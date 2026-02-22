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

from adapters.wa_notifier.config import REPLY_PREFIX, WANotifierConfig

log = logging.getLogger(__name__)

FIFO_NAME = "in.whatsapp"


class WhatsAppNotifier:
    """Poll whatsapp-mcp SQLite database and notify ccmux via FIFO."""

    def __init__(self, cfg: WANotifierConfig) -> None:
        self.cfg = cfg
        self.last_seen_ts: str = ""  # initialized from DB in run()
        self._fifo_path: Path = cfg.runtime_dir / FIFO_NAME
        self._running = False

    async def run(self) -> None:
        """Main loop: create FIFO, poll SQLite, write notifications."""
        self._ensure_fifo()
        self._init_last_seen()
        self._running = True
        log.info(
            "WhatsApp notifier started: db=%s poll=%ds fifo=%s last_seen=%s",
            self.cfg.db_path, self.cfg.poll_interval, self._fifo_path,
            self.last_seen_ts,
        )
        try:
            while self._running:
                try:
                    summaries, admin_msgs, new_ts = self._query_new_messages()
                    if admin_msgs:
                        self._write_admin_notification(admin_msgs)
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

    def _query_new_messages(self) -> tuple[list[dict], list[dict], str]:
        """Query SQLite for messages newer than last_seen_ts.

        Returns (regular_summaries, admin_messages) where:
        - regular_summaries: list of {chat_id, sender, count, preview}
        - admin_messages: list of {content, timestamp} from admin self-chat
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

            # Include is_from_me=1 for admin self-chat
            if admin_jid:
                cur.execute(
                    """
                    SELECT chat_jid, sender, content, timestamp, is_from_me
                    FROM messages
                    WHERE timestamp > ?
                      AND content != ''
                      AND (is_from_me = 0 OR chat_jid = ?)
                    ORDER BY timestamp ASC
                    """,
                    (self.last_seen_ts, admin_jid),
                )
            else:
                cur.execute(
                    """
                    SELECT chat_jid, sender, content, timestamp, is_from_me
                    FROM messages
                    WHERE timestamp > ?
                      AND is_from_me = 0
                      AND content != ''
                    ORDER BY timestamp ASC
                    """,
                    (self.last_seen_ts,),
                )
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return [], [], ""

        # Compute new high-water mark but do NOT advance yet —
        # caller must commit only after successful delivery.
        new_ts = max(row["timestamp"] for row in rows)

        # Separate admin self-chat messages from regular messages
        admin_msgs: list[dict] = []
        regular_rows: list[sqlite3.Row] = []

        for row in rows:
            if admin_jid and row["chat_jid"] == admin_jid and row["is_from_me"] == 1:
                content = row["content"] or ""
                # Anti-echo: skip messages prefixed with reply marker (Claude's own replies)
                if content.startswith(REPLY_PREFIX):
                    continue
                admin_msgs.append({
                    "content": content,
                    "timestamp": row["timestamp"],
                })
            elif row["is_from_me"] == 0:
                regular_rows.append(row)

        # Filter regular messages by allowed_chats
        if self.cfg.allowed_chats:
            allowed = set(self.cfg.allowed_chats)
            regular_rows = [r for r in regular_rows if r["chat_jid"] in allowed]

        # Filter out group messages if configured
        if self.cfg.ignore_groups:
            regular_rows = [r for r in regular_rows if not self._is_group_jid(r["chat_jid"])]

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
            chats[chat_id]["preview"] = content[:80]

        return list(chats.values()), admin_msgs, new_ts

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
