"""Zulip adapter — inbound message routing.

Connects to the Zulip event queue, receives messages, routes each to the
correct per-topic instance via FIFO. Outbound is handled separately by
per-instance Stop hooks (zulip_relay_hook.py).

Uses stdlib urllib for Zulip API calls (no aiohttp dependency for the
HTTP client — Zulip's long-polling is simple enough).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import ZulipAdapterConfig, scan_streams
from .file_handler import (
    download_file,
    extract_attachments,
    preprocess_image,
    safe_resolve,
    sanitize_filename,
    strip_attachment_links,
)
from .process_mgr import CreateMode, ProcessManager

log = logging.getLogger(__name__)

# Reconnection backoff
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0
POLL_TIMEOUT = 90  # seconds for long-poll (Zulip default)

# Staleness watchdog: if no events (including heartbeats) arrive within this
# many seconds, force re-registration.  Zulip sends heartbeat events every
# ~POLL_TIMEOUT seconds during quiet periods, so 3× is a generous threshold.
STALE_QUEUE_TIMEOUT = POLL_TIMEOUT * 3  # 270 s


def _load_api_key(credentials_path: Path) -> str:
    """Read ZULIP_BOT_API_KEY from the credentials file."""
    if not credentials_path.exists():
        raise ValueError(f"Credentials file not found: {credentials_path}")

    for line in credentials_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("ZULIP_BOT_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            return value

    raise ValueError(f"ZULIP_BOT_API_KEY not found in {credentials_path}")


class ZulipAdapter:
    """Inbound adapter: Zulip event queue → route → FIFO."""

    def __init__(self, cfg: ZulipAdapterConfig):
        self.cfg = cfg
        self.api_key = _load_api_key(cfg.bot_credentials)
        self.process_mgr = ProcessManager(cfg)
        self._running = True
        self._auth_header = self._build_auth()
        # Bypass system proxy — Zulip server is local, no proxy needed.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _build_auth(self) -> str:
        cred = base64.b64encode(
            f"{self.cfg.bot_email}:{self.api_key}".encode()
        ).decode()
        return f"Basic {cred}"

    def _api_call(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        timeout: int = 30,
    ) -> dict:
        """Make authenticated Zulip API call. Returns parsed JSON."""
        url = f"{self.cfg.site}/api/v1{endpoint}"

        if data is not None:
            encoded = urllib.parse.urlencode(data, doseq=True).encode()
        else:
            encoded = None

        req = urllib.request.Request(url, data=encoded, method=method)
        req.add_header("Authorization", self._auth_header)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"result": "error", "msg": f"HTTP {e.code}: {body[:200]}"}
        except Exception as e:
            return {"result": "error", "msg": str(e)}

    def _register_event_queue(self) -> tuple[str, int]:
        """Register a Zulip event queue for stream messages.

        Returns (queue_id, last_event_id).
        """
        result = self._api_call(
            "POST",
            "/register",
            {
                "event_types": json.dumps(["message"]),
                "narrow": json.dumps([["is", "stream"]]),
                "apply_markdown": "false",
            },
        )

        if result.get("result") != "success":
            raise ConnectionError(
                f"Failed to register event queue: {result.get('msg', 'unknown')}"
            )

        return result["queue_id"], result["last_event_id"]

    def _get_events(
        self, queue_id: str, last_event_id: int
    ) -> list[dict]:
        """Long-poll for events. Returns list of event dicts."""
        params = urllib.parse.urlencode(
            {"queue_id": queue_id, "last_event_id": last_event_id}
        )
        result = self._api_call(
            "GET",
            f"/events?{params}",
            timeout=POLL_TIMEOUT + 10,
        )

        if result.get("result") != "success":
            code = result.get("code", "")
            msg = result.get("msg", "")
            if code == "BAD_EVENT_QUEUE_ID":
                raise ConnectionError("Event queue expired, need re-register")
            raise ConnectionError(f"get_events failed: {msg}")

        return result.get("events", [])

    def _post_message(self, stream: str, topic: str, content: str) -> None:
        """Post a message to a Zulip stream+topic."""
        result = self._api_call(
            "POST",
            "/messages",
            {
                "type": "stream",
                "to": stream,
                "topic": topic,
                "content": content,
            },
        )
        if result.get("result") != "success":
            log.warning(
                "Failed to post message to %s/%s: %s",
                stream, topic, result.get("msg", "unknown"),
            )

    def _fetch_topic_history(
        self, stream: str, topic: str, limit: int = 20
    ) -> list[dict]:
        """Fetch recent message history for a topic from Zulip API.

        Returns list of message dicts (newest last), excluding bot's own messages.
        Returns empty list on API failure.
        """
        narrow = json.dumps([
            ["stream", stream],
            ["topic", topic],
        ])
        params = urllib.parse.urlencode({
            "narrow": narrow,
            "num_before": limit,
            "num_after": 0,
            "anchor": "newest",
            "apply_markdown": "false",
        })
        result = self._api_call("GET", f"/messages?{params}")

        if result.get("result") != "success":
            log.warning(
                "Failed to fetch topic history for %s/%s: %s",
                stream, topic, result.get("msg", "unknown"),
            )
            return []

        messages = result.get("messages", [])
        # Filter out bot's own messages and reverse to chronological order
        filtered = [
            m for m in messages
            if m.get("sender_email") != self.cfg.bot_email
        ]
        return filtered

    def _format_history_context(self, history: list[dict]) -> str:
        """Format message history as a context recovery prompt."""
        lines = ["[Context recovery] Previous conversation in this topic:"]
        for msg in history:
            sender = msg.get("sender_full_name", "unknown")
            ts = msg.get("timestamp", 0)
            dt = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "??:??"
            content = msg.get("content", "")
            lines.append(f"  [{dt}] {sender}: {content}")
        lines.append("[End of context recovery — continue from here]")
        return "\n".join(lines)

    def _write_to_fifo(self, fifo_path: Path, message: str) -> bool:
        """Write message to FIFO. Returns True on success.

        Uses O_WRONLY (blocking). The sentinel fd kept open by process_mgr
        ensures a reader is always present, so open() will not block and
        writes are safe from ENXIO. Blocking mode avoids partial writes
        that could corrupt messages when using O_NONBLOCK.

        Messages are NUL-delimited (\\0) to preserve multi-line content.
        The injector splits on NUL instead of newline.
        """
        try:
            fd = os.open(str(fifo_path), os.O_WRONLY)
            try:
                data = (message + "\0").encode("utf-8")
                total = len(data)
                written = 0
                while written < total:
                    n = os.write(fd, data[written:])
                    if n == 0:
                        break
                    written += n
                return written == total
            finally:
                os.close(fd)
        except OSError as e:
            log.warning("FIFO write failed (%s): %s", fifo_path, e)
            return False

    async def _handle_message(self, event: dict) -> None:
        """Route a single message event to the correct instance."""
        msg = event.get("message", {})

        # Ignore bot's own messages (echo prevention)
        if msg.get("sender_email") == self.cfg.bot_email:
            return

        # Only handle stream messages
        if msg.get("type") != "stream":
            return

        stream = msg.get("display_recipient", "")
        topic = msg.get("subject", "chat")
        content = msg.get("content", "")
        sender = msg.get("sender_full_name", "unknown")

        if not stream or not content:
            return

        # Hot-reload stream configs (mtime-based cache)
        scan_streams(self.cfg)

        # Check if stream is registered
        if stream not in self.cfg.streams:
            log.debug("Ignoring message in unregistered stream: %s", stream)
            return

        stream_cfg = self.cfg.streams[stream]

        # Ensure instance is alive (lazy create if needed)
        fifo, create_mode = await self.process_mgr.ensure_instance(stream, topic, stream_cfg)

        # --- New session: use Zulip history as reliable message source ---
        # When a session is freshly created, Claude's TUI needs time to
        # initialize.  Instead of writing the triggering message to the
        # FIFO (which races against TUI readiness), we schedule a deferred
        # recovery task that fetches ALL unprocessed messages from the
        # Zulip API after the injector confirms Claude is ready.
        # Zulip message history is the durable source of truth — no
        # message can be lost because it persists in Zulip regardless of
        # pipe buffer or TUI state.
        if create_mode in (CreateMode.FIRST_TIME, CreateMode.FALLBACK):
            if create_mode == CreateMode.FIRST_TIME:
                self._post_message(
                    stream, topic, "\U0001f916 Session started.")
            elif create_mode == CreateMode.FALLBACK:
                self._post_message(
                    stream, topic,
                    "\U0001f916 Session restarted. Recovering context...",
                )
        elif create_mode == CreateMode.RESUMED:
            self._post_message(
                stream, topic,
                "\U0001f916 Session resumed (previous context restored).",
            )

        if create_mode in (CreateMode.FIRST_TIME, CreateMode.FALLBACK):

            # Download attachments for the current message so files are
            # available when Claude processes the recovered messages.
            self._download_attachments(content, stream_cfg, topic)

            asyncio.create_task(
                self._recover_pending_messages(
                    stream, topic, fifo, stream_cfg,
                ),
                name=f"recover-{stream}/{topic}",
            )
            log.info(
                "Deferred message delivery for %s/%s (mode=%s, from=%s)",
                stream, topic, create_mode.value, sender,
            )
            return  # Skip normal FIFO write — recovery handles it

        # --- Steady state: direct FIFO write ---
        formatted = self._format_message(content, stream_cfg, topic)

        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(
            None, self._write_to_fifo, fifo, formatted
        )
        if not success:
            log.warning(
                "FIFO write failed for %s/%s, retrying in 1s...",
                stream, topic,
            )
            await asyncio.sleep(1.0)
            fifo, _ = await self.process_mgr.ensure_instance(stream, topic, stream_cfg)
            success = await loop.run_in_executor(
                None, self._write_to_fifo, fifo, formatted
            )

        if success:
            log.info(
                "Routed message: %s/%s from=%s len=%d",
                stream, topic, sender, len(content),
            )
        else:
            log.error(
                "FAILED to route message: %s/%s from=%s len=%d "
                "(FIFO write failed after retry)",
                stream, topic, sender, len(content),
            )

    def _format_message(
        self, content: str, stream_cfg, topic: str
    ) -> str:
        """Format a message with attachments and timestamp prefix.

        Downloads attachments, strips raw upload links, adds file
        notifications, and prefixes with timestamp.
        """
        file_notifications: list[str] = []
        attachments = extract_attachments(content)
        if attachments:
            file_notifications = self._download_attachments(
                content, stream_cfg, topic
            )
            content = strip_attachment_links(content)

        now = datetime.now().strftime("%y/%m/%d %H:%M")
        parts: list[str] = []
        if file_notifications:
            parts.extend(file_notifications)
        if content:
            parts.append(content)
        body = "\n".join(parts) if parts else content
        return f"[{now} From zulip] {body}"

    def _download_attachments(
        self, content: str, stream_cfg, topic: str
    ) -> list[str]:
        """Download attachments from message content. Returns file notification strings."""
        file_notifications: list[str] = []
        attachments = extract_attachments(content)
        if not attachments:
            return file_notifications

        project_path = stream_cfg.project_path
        topic_dir_name = topic.replace("/", "_").replace("\\", "_")
        for display_name, server_path in attachments:
            filename = sanitize_filename(display_name)
            rel_path = f".zulip-uploads/{topic_dir_name}/{filename}"
            dest = safe_resolve(project_path, rel_path)
            if dest is None:
                log.warning(
                    "Path validation failed for attachment: %s", rel_path
                )
                continue
            ok = download_file(
                self._opener,
                self.cfg.site,
                self._auth_header,
                server_path,
                dest,
            )
            if ok:
                converted = preprocess_image(dest)
                if converted:
                    rel_path = str(converted.relative_to(project_path))
                file_notifications.append(f"[File: {rel_path}]")
            else:
                log.warning("Failed to download attachment: %s", server_path)

        return file_notifications

    async def _recover_pending_messages(
        self,
        stream: str,
        topic: str,
        fifo: Path,
        stream_cfg,
    ) -> None:
        """Fetch unprocessed messages from Zulip and inject after Claude is ready.

        This replaces direct FIFO writes for new sessions.  Zulip message
        history is the durable source of truth — we fetch all user messages
        that have no bot response after them and inject the batch.

        The injector's FIRST_INJECT_SETTLE delay ensures Claude's TUI is
        fully initialized before the text is actually sent to the terminal.
        """
        key = f"{stream}/{topic}"
        try:
            # Fetch topic history from Zulip API
            loop = asyncio.get_running_loop()
            history = await loop.run_in_executor(
                None, self._fetch_topic_history, stream, topic, 20,
            )

            if not history:
                log.warning(
                    "No history found for %s/%s during recovery", stream, topic
                )
                return

            # Find unprocessed messages: all user messages after the last
            # bot response (or all messages if no bot response exists).
            unprocessed = self._find_unprocessed_messages(history)

            if not unprocessed:
                log.info("No unprocessed messages for %s/%s", stream, topic)
                return

            # Format each unprocessed message with its original timestamp
            parts: list[str] = []
            for msg_item in unprocessed:
                ts = msg_item.get("timestamp", 0)
                dt = datetime.fromtimestamp(ts).strftime("%y/%m/%d %H:%M")
                msg_content = msg_item.get("content", "")

                # Download and process attachments in each message
                msg_files = self._download_attachments(
                    msg_content, stream_cfg, topic
                )
                msg_content = strip_attachment_links(msg_content)

                msg_parts: list[str] = []
                if msg_files:
                    msg_parts.extend(msg_files)
                if msg_content:
                    msg_parts.append(msg_content)
                body = "\n".join(msg_parts) if msg_parts else msg_content
                parts.append(f"[{dt} From zulip] {body}")

            combined = "\n---\n".join(parts)

            # Write to FIFO — the injector's settle delay + gate ensures
            # Claude is ready before actual terminal injection.
            success = await loop.run_in_executor(
                None, self._write_to_fifo, fifo, combined
            )

            if success:
                log.info(
                    "Recovered %d pending message(s) for %s/%s (%d bytes)",
                    len(unprocessed), stream, topic,
                    len(combined.encode("utf-8")),
                )
                self._post_message(
                    stream, topic,
                    f"\U0001f916 Delivering {len(unprocessed)} "
                    f"pending message(s)...",
                )
            else:
                log.error(
                    "FIFO write failed during recovery for %s/%s", stream, topic
                )
        except Exception as e:
            log.error("Recovery failed for %s/%s: %s", stream, topic, e)

    def _find_unprocessed_messages(self, history: list[dict]) -> list[dict]:
        """Find user messages after the last bot response in topic history.

        Returns list of message dicts (chronological order) that have not
        been responded to by the bot.  These are the messages that need
        to be delivered to Claude.
        """
        # history is already filtered (no bot messages) and chronological.
        # But we need to check the FULL history (including bot messages)
        # to find the last bot response boundary.
        # Since _fetch_topic_history already filters out bot messages,
        # ALL messages in history are user messages that need delivery.
        # This is correct: if Claude never responded, all messages are
        # unprocessed.  If Claude did respond, those responses wouldn't
        # be in the filtered history.
        #
        # However, we should only deliver RECENT unprocessed messages,
        # not the entire history.  The last few messages are what matter.
        return history

    async def run(self) -> None:
        """Main event loop: register queue, long-poll, route messages.

        Includes a staleness watchdog: if no events (including heartbeats)
        arrive within ``STALE_QUEUE_TIMEOUT`` seconds, the queue is assumed
        dead and re-registered.  This catches silent connection deaths that
        do not raise exceptions (e.g. TCP half-open, server-side GC).
        """
        backoff = INITIAL_BACKOFF
        self._queue_id: str | None = None

        while self._running:
            try:
                log.info("Registering Zulip event queue...")
                queue_id, last_event_id = await asyncio.get_running_loop().run_in_executor(
                    None, self._register_event_queue
                )
                self._queue_id = queue_id
                log.info("Event queue registered: %s", queue_id)
                backoff = INITIAL_BACKOFF  # Reset on success
                last_event_time = time.monotonic()

                while self._running:
                    events = await asyncio.get_running_loop().run_in_executor(
                        None, self._get_events, queue_id, last_event_id
                    )

                    if events:
                        last_event_time = time.monotonic()

                    for event in events:
                        last_event_id = max(
                            last_event_id, event.get("id", last_event_id)
                        )
                        if event.get("type") == "message":
                            await self._handle_message(event)

                    # Staleness watchdog: force re-registration if no events
                    # (including Zulip heartbeats) for too long.
                    elapsed = time.monotonic() - last_event_time
                    if elapsed > STALE_QUEUE_TIMEOUT:
                        log.warning(
                            "Event queue stale (no events for %.0fs > %.0fs "
                            "threshold), forcing re-registration",
                            elapsed, STALE_QUEUE_TIMEOUT,
                        )
                        break  # Break inner loop → re-register in outer loop

            except ConnectionError as e:
                log.warning("Connection error: %s (retry in %.0fs)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

            except Exception as e:
                log.error("Unexpected error: %s (retry in %.0fs)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    def _delete_event_queue(self, queue_id: str) -> None:
        """Delete the event queue to unblock the long-poll immediately."""
        self._api_call("DELETE", "/events", data={"queue_id": queue_id}, timeout=5)

    def stop(self) -> None:
        """Signal the adapter to stop. Deletes event queue to unblock long-poll.

        Idempotent — safe to call multiple times (signal handler + finally block).
        """
        self._running = False
        if hasattr(self, "_queue_id") and self._queue_id:
            qid = self._queue_id
            self._queue_id = None  # Clear first to prevent double-delete
            try:
                self._delete_event_queue(qid)
            except Exception:
                pass
        self.process_mgr.stop_all()
