"""WhatsApp notifier: polls SQLite for new messages, writes to FIFO.

whatsapp-mcp's Go bridge stores timestamps as Go time.Time strings via
go-sqlite3 (e.g. "2026-02-21 16:14:59+08:00").  To avoid format mismatches
the notifier reads max(timestamp) from the DB on startup, so all subsequent
comparisons use the exact same format the bridge produces.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from adapters.wa_notifier.config import BOT_PREFIXES, REPLY_PREFIX, WANotifierConfig

log = logging.getLogger(__name__)

FIFO_NAME = "in.whatsapp"


class WhatsAppNotifier:
    """Poll whatsapp-mcp SQLite database and notify ccmux via FIFO."""

    def __init__(self, cfg: WANotifierConfig) -> None:
        self.cfg = cfg
        self.last_seen_ts: str = ""  # initialized from DB in run()
        self._fifo_path: Path = cfg.runtime_dir / FIFO_NAME
        self._running = False

        # Intent classification (local heuristics only, no API calls)
        self._classifier = None
        self._smart_classify_chats: set[str] = set()

        # Load S3 whitelist from contacts.json for permission gating
        from ccmux.paths import load_s3_whitelist
        self._s3_whitelist: frozenset[str] = load_s3_whitelist()
        if self._s3_whitelist:
            log.info("S3 whitelist loaded: %d JID(s)", len(self._s3_whitelist))

        if cfg.classify_enabled and cfg.smart_classify_chats:
            self._smart_classify_chats = set(cfg.smart_classify_chats)
            from adapters.wa_notifier.classifier import IntentClassifier
            self._classifier = IntentClassifier(s3_whitelist=self._s3_whitelist)
            log.info(
                "Intent classifier enabled for %d chat(s)",
                len(self._smart_classify_chats),
            )

    async def run(self) -> None:
        """Main loop: create FIFO, poll SQLite, write notifications."""
        self._ensure_fifo()
        self._init_last_seen()
        self._running = True
        log.info(
            "WhatsApp notifier started: db=%s poll=%ds fifo=%s last_seen=%s classify=%s",
            self.cfg.db_path, self.cfg.poll_interval, self._fifo_path,
            self.last_seen_ts, bool(self._classifier),
        )
        try:
            while self._running:
                try:
                    summaries, admin_msgs, classified_msgs, new_ts = (
                        self._query_new_messages()
                    )
                    if admin_msgs:
                        self._write_admin_notification(admin_msgs)
                    if classified_msgs:
                        self._classify_and_write(classified_msgs)
                    if summaries:
                        self._write_notification(summaries)
                    # Advance high-water mark only after successful delivery
                    if new_ts:
                        self.last_seen_ts = new_ts
                except sqlite3.Error as exc:
                    log.warning("SQLite query failed: %s", exc)
                except OSError as exc:
                    log.warning("FIFO write failed: %s", exc)
                await asyncio.sleep(self.cfg.poll_interval)
        finally:
            # Do NOT call _cleanup_fifo() here. The FIFO is a reusable
            # filesystem object in /tmp and ccmux keeps a reader fd on it.
            # Deleting + recreating triggers a DirectoryWatcher race and
            # leaves a window where ccmux has no reader registered.
            pass

    def stop(self) -> None:
        """Signal the run loop to exit."""
        self._running = False

    def _init_last_seen(self) -> None:
        """Read max(timestamp) from the DB so we use the bridge's own format.

        If the DB is empty or unreadable, fall back to a sentinel that is
        lexicographically smaller than any real timestamp ("").
        """
        if self.last_seen_ts:
            return  # already set (e.g. by test)
        try:
            conn = sqlite3.connect(
                f"file:{self.cfg.db_path}?mode=ro", uri=True, timeout=5.0,
            )
            try:
                row = conn.execute(
                    "SELECT max(timestamp) FROM messages"
                ).fetchone()
                if row and row[0]:
                    self.last_seen_ts = row[0]
                    return
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("Could not read initial timestamp: %s", exc)
        # DB empty or unreadable — empty string is < any real timestamp,
        # but _query_new_messages will just return everything (which is
        # then ignored since there's nothing truly "new").
        self.last_seen_ts = ""

    def _ensure_fifo(self) -> None:
        """Create the input FIFO if it does not exist."""
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        if not self._fifo_path.exists():
            os.mkfifo(str(self._fifo_path))
            log.info("Created FIFO: %s", self._fifo_path)

    def _cleanup_fifo(self) -> None:
        """Remove the FIFO on shutdown."""
        try:
            if self._fifo_path.exists():
                self._fifo_path.unlink()
                log.info("Removed FIFO: %s", self._fifo_path)
        except OSError as exc:
            log.warning("Failed to remove FIFO: %s", exc)

    def _query_new_messages(
        self,
    ) -> tuple[list[dict], list[dict], list[dict], str]:
        """Query SQLite for messages newer than last_seen_ts.

        Returns (regular_summaries, admin_messages, classified_messages, new_ts):
        - regular_summaries: list of {chat_id, sender, count, preview}
        - admin_messages: list of {content, timestamp} from admin self-chat
        - classified_messages: individual rows from smart_classify_chats
        - new_ts: new high-water mark timestamp (or "" if no messages)
        """
        admin_jid = self.cfg.admin_jid
        conn = sqlite3.connect(
            f"file:{self.cfg.db_path}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Include is_from_me=1 for admin self-chat AND allowed group chats
            # (so admin's messages in monitored groups are picked up too)
            allowed_set = set(self.cfg.allowed_chats) if self.cfg.allowed_chats else set()
            include_from_me = {admin_jid} if admin_jid else set()
            include_from_me |= allowed_set
            # Also include smart_classify_chats so their is_from_me messages
            # are picked up (for echo filtering in Claude)
            include_from_me |= self._smart_classify_chats

            # Fetch ALL messages (including is_from_me) so admin's messages
            # from any chat can be forwarded.  Bot echo filtering is done in
            # Python below using BOT_PREFIXES.
            cur.execute(
                """
                SELECT chat_jid, sender, content, timestamp, is_from_me,
                       media_type
                FROM messages
                WHERE timestamp > ?
                  AND (content != '' OR media_type IS NOT NULL)
                ORDER BY timestamp ASC
                """,
                (self.last_seen_ts,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return [], [], [], ""

        # Compute new high-water mark but do NOT advance yet —
        # caller must commit only after successful delivery.
        new_ts = max(row["timestamp"] for row in rows)

        # Separate admin self-chat messages from regular messages
        admin_msgs: list[dict] = []
        regular_rows: list[sqlite3.Row] = []

        allowed_group_set = set(self.cfg.allowed_chats) if self.cfg.allowed_chats else set()

        for row in rows:
            chat_jid = row["chat_jid"]
            is_from_me = row["is_from_me"] == 1
            content = row["content"] or ""

            if is_from_me:
                if admin_jid and chat_jid == admin_jid:
                    # Admin self-chat: skip bot echoes, forward human messages
                    if any(content.startswith(p) for p in BOT_PREFIXES):
                        continue
                    media_type = (
                        row["media_type"] if "media_type" in row.keys() else None
                    )
                    fwd_content = content
                    if not fwd_content and media_type:
                        fwd_content = f"[{media_type}]"
                    admin_msgs.append({
                        "content": fwd_content,
                        "timestamp": row["timestamp"],
                        "media_type": media_type,
                    })
                elif chat_jid in allowed_group_set or chat_jid in self._smart_classify_chats:
                    # Admin typing in monitored groups: include as regular
                    regular_rows.append(row)
                else:
                    # Admin typing in other chats (e.g. Joy's chat):
                    # forward as admin message so instructions reach the bot
                    media_type = (
                        row["media_type"] if "media_type" in row.keys() else None
                    )
                    fwd_content = content
                    if not fwd_content and media_type:
                        fwd_content = f"[{media_type}]"
                    admin_msgs.append({
                        "content": fwd_content,
                        "timestamp": row["timestamp"],
                        "media_type": media_type,
                    })
            elif chat_jid in allowed_group_set or chat_jid in self._smart_classify_chats:
                # Monitored groups: include ALL messages without filtering.
                # Echo handling is done by Claude (it knows what it sent).
                regular_rows.append(row)
            else:
                regular_rows.append(row)

        # Filter regular messages by allowed_chats
        if self.cfg.allowed_chats:
            allowed = set(self.cfg.allowed_chats) | self._smart_classify_chats
            regular_rows = [r for r in regular_rows if r["chat_jid"] in allowed]

        # Filter out group messages if configured
        if self.cfg.ignore_groups:
            # Never filter out smart_classify_chats — they're groups we monitor
            regular_rows = [
                r for r in regular_rows
                if not self._is_group_jid(r["chat_jid"])
                or r["chat_jid"] in self._smart_classify_chats
            ]

        # Separate messages for smart classification from regular aggregation
        classified_msgs: list[dict] = []
        if self._smart_classify_chats:
            remaining: list[sqlite3.Row] = []
            for r in regular_rows:
                if r["chat_jid"] in self._smart_classify_chats:
                    media_type = (
                        r["media_type"] if "media_type" in r.keys() else None
                    )
                    classified_msgs.append({
                        "chat_jid": r["chat_jid"],
                        "sender": r["sender"] or r["chat_jid"],
                        "content": r["content"] or "",
                        "timestamp": r["timestamp"],
                        "media_type": media_type,
                    })
                else:
                    remaining.append(r)
            regular_rows = remaining

        # Aggregate regular messages by chat_jid
        chats: dict[str, dict] = {}
        for row in regular_rows:
            chat_id = row["chat_jid"]
            if chat_id not in chats:
                chats[chat_id] = {
                    "chat_id": chat_id,
                    "sender": row["sender"] or chat_id,
                    "count": 0,
                    "preview": "",
                }
            chats[chat_id]["count"] += 1
            content = row["content"] or ""
            media_type = row["media_type"] if "media_type" in row.keys() else None
            if not content and media_type:
                content = f"[{media_type}]"
            chats[chat_id]["preview"] = content[:80]

        return list(chats.values()), admin_msgs, classified_msgs, new_ts

    def _classify_and_write(self, messages: list[dict]) -> None:
        """Classify each message and write non-silent ones to FIFO."""
        from adapters.wa_notifier.classifier import SILENT_INTENTS

        for msg in messages:
            text = msg.get("content", "")
            sender = msg.get("sender", "")
            media_type = msg.get("media_type")
            chat_jid = msg.get("chat_jid", "")
            has_media = bool(media_type)

            if self._classifier:
                result = self._classifier.classify(
                    text, sender, has_media, media_type, chat_jid,
                )
            else:
                # Classifier not available — pass everything through as unknown
                from adapters.wa_notifier.classifier import (
                    ClassificationResult,
                    Intent,
                )
                result = ClassificationResult(
                    Intent.UNKNOWN, 0.0, "Classifier unavailable", "respond",
                )

            if result.intent in SILENT_INTENTS:
                log.info(
                    "Classified as silent: %s (sender=%s)",
                    result.intent.value, sender,
                )
                continue

            self._write_classified_notification(msg, result)

    def _write_classified_notification(
        self, msg: dict, result: "ClassificationResult",
    ) -> None:
        """Write a single classified message to FIFO with intent metadata."""
        from adapters.wa_notifier.classifier import ClassificationResult

        content = msg.get("content", "")
        media_type = msg.get("media_type")
        sender = msg.get("sender", "")

        if not content and media_type:
            content = f"[{media_type}]"

        # Build human-readable description for Claude
        description = (
            f"Household group message [intent: {result.intent.value}] "
            f"from {sender}: {content[:80]}"
        )
        if result.intent.value != "s3_command":
            description += "\nUse list_messages to read full details."

        payload = json.dumps({
            "channel": "whatsapp",
            "content": description,
            "ts": int(time.time()),
            "intent": result.intent.value,
            "intent_meta": {
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "action": result.action,
                "chat_jid": msg.get("chat_jid", ""),
                "sender": sender,
                "original_content": msg.get("content", ""),
                "media_type": media_type,
            },
        })

        # O_WRONLY | O_NONBLOCK: don't block if no reader yet
        fd = os.open(str(self._fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (payload + "\n").encode())
            log.info(
                "Classified notification: intent=%s sender=%s",
                result.intent.value, sender,
            )
        finally:
            os.close(fd)

    def _write_notification(self, summaries: list[dict]) -> None:
        """Format and write notification to FIFO."""
        lines = []
        for s in summaries:
            preview = s["preview"]
            if len(preview) > 60:
                preview = preview[:57] + "..."
            lines.append(
                f'- {s["sender"]} ({s["count"]} msg{"s" if s["count"] > 1 else ""}): '
                f'"{preview}"'
            )

        content = "New WhatsApp messages:\n" + "\n".join(lines)
        content += "\nUse list_messages tool to read full messages."

        payload = json.dumps({
            "channel": "whatsapp",
            "content": content,
            "ts": int(time.time()),
        })

        # O_WRONLY | O_NONBLOCK: don't block if no reader yet
        fd = os.open(str(self._fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (payload + "\n").encode())
            log.info(
                "Notified: %d chat(s), %d total message(s)",
                len(summaries),
                sum(s["count"] for s in summaries),
            )
        finally:
            os.close(fd)

    def _write_admin_notification(self, messages: list[dict]) -> None:
        """Write admin self-chat messages directly to FIFO (full content, no summary)."""
        for msg in messages:
            payload = json.dumps({
                "channel": "whatsapp",
                "content": msg["content"],
                "ts": int(time.time()),
            })
            fd = os.open(str(self._fifo_path), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, (payload + "\n").encode())
            finally:
                os.close(fd)
        log.info("Admin chat: forwarded %d message(s)", len(messages))

    @staticmethod
    def _is_group_jid(jid: str) -> bool:
        """WhatsApp group JIDs end with @g.us; 1:1 chats end with @s.whatsapp.net."""
        return "@g.us" in jid
