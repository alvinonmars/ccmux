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
    safe_resolve,
    sanitize_filename,
    strip_attachment_links,
)
from .process_mgr import ProcessManager

log = logging.getLogger(__name__)

# Reconnection backoff
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0
POLL_TIMEOUT = 90  # seconds for long-poll (Zulip default)


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
        fifo, created = await self.process_mgr.ensure_instance(stream, topic, stream_cfg)

        if created:
            # Notify in Zulip that a new session started
            self._post_message(stream, topic, "\U0001f916 Session started.")

        # Handle file attachments: download to project dir, build notifications
        file_notifications: list[str] = []
        attachments = extract_attachments(content)
        if attachments:
            project_path = stream_cfg.project_path
            topic_dir_name = content_topic = topic.replace("/", "_").replace("\\", "_")
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
                    file_notifications.append(f"[File: {rel_path}]")
                else:
                    log.warning("Failed to download attachment: %s", server_path)

            # Strip raw /user_uploads/ links from the text
            content = strip_attachment_links(content)

        # Prefix with timestamp and channel so Claude Code knows when
        # and where the message came from. Each topic is an isolated
        # single-user instance, so sender name is omitted.
        # Format: [yy/mm/dd hh:mm From zulip]
        now = datetime.now().strftime("%y/%m/%d %H:%M")
        parts: list[str] = []
        if file_notifications:
            parts.extend(file_notifications)
        if content:
            parts.append(content)
        body = "\n".join(parts) if parts else content
        formatted = f"[{now} From zulip] {body}"

        # Write to FIFO in executor to avoid blocking the event loop
        # (prevents deadlock if pipe buffer fills during message burst)
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(
            None, self._write_to_fifo, fifo, formatted
        )
        if not success:
            log.error(
                "Failed to write to FIFO for %s/%s, will retry on next message",
                stream, topic,
            )

        log.info(
            "Routed message: %s/%s from=%s len=%d",
            stream, topic, sender, len(content),
        )

    async def run(self) -> None:
        """Main event loop: register queue, long-poll, route messages."""
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

                while self._running:
                    events = await asyncio.get_running_loop().run_in_executor(
                        None, self._get_events, queue_id, last_event_id
                    )

                    for event in events:
                        last_event_id = max(
                            last_event_id, event.get("id", last_event_id)
                        )
                        if event.get("type") == "message":
                            await self._handle_message(event)

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
