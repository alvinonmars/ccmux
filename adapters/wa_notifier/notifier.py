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

from adapters.wa_notifier.config import WANotifierConfig

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
                    summaries = self._query_new_messages()
                    if summaries:
                        self._write_notification(summaries)
                except sqlite3.Error as exc:
                    log.warning("SQLite query failed: %s", exc)
                except OSError as exc:
                    log.warning("FIFO write failed: %s", exc)
                await asyncio.sleep(self.cfg.poll_interval)
        finally:
            self._cleanup_fifo()

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

    def _query_new_messages(self) -> list[dict]:
        """Query SQLite for messages newer than last_seen_ts.

        Returns a list of summary dicts: {chat_id, sender, count, preview}.
        """
        conn = sqlite3.connect(
            f"file:{self.cfg.db_path}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute(
                """
                SELECT chat_jid, sender, content, timestamp
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
            return []

        # Update last_seen to the newest message timestamp
        self.last_seen_ts = max(row["timestamp"] for row in rows)

        # Filter by allowed_chats
        if self.cfg.allowed_chats:
            allowed = set(self.cfg.allowed_chats)
            rows = [r for r in rows if r["chat_jid"] in allowed]

        # Filter out group messages if configured
        if self.cfg.ignore_groups:
            rows = [r for r in rows if not self._is_group_jid(r["chat_jid"])]

        if not rows:
            return []

        # Aggregate by chat_jid
        chats: dict[str, dict] = {}
        for row in rows:
            chat_id = row["chat_jid"]
            if chat_id not in chats:
                chats[chat_id] = {
                    "chat_id": chat_id,
                    "sender": row["sender"] or chat_id,
                    "count": 0,
                    "preview": "",
                }
            chats[chat_id]["count"] += 1
            # Keep last message as preview (truncated)
            content = row["content"] or ""
            chats[chat_id]["preview"] = content[:80]

        return list(chats.values())

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

    @staticmethod
    def _is_group_jid(jid: str) -> bool:
        """WhatsApp group JIDs end with @g.us; 1:1 chats end with @s.whatsapp.net."""
        return "@g.us" in jid
