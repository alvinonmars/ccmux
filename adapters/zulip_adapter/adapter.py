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
            return line.split("=", 1)[1].strip()

    raise ValueError(f"ZULIP_BOT_API_KEY not found in {credentials_path}")


class ZulipAdapter:
    """Inbound adapter: Zulip event queue → route → FIFO."""

    def __init__(self, cfg: ZulipAdapterConfig):
        self.cfg = cfg
        self.api_key = _load_api_key(cfg.bot_credentials)
        self.process_mgr = ProcessManager(cfg)
        self._running = True
        self._auth_header = self._build_auth()

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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        result = self._api_call(
            "GET",
            f"/events?queue_id={queue_id}&last_event_id={last_event_id}",
            timeout=POLL_TIMEOUT + 10,
        )

        if result.get("result") != "success":
            msg = result.get("msg", "")
            if "BAD_EVENT_QUEUE_ID" in msg:
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
        """Write message to FIFO (non-blocking). Returns True on success.

        Uses O_WRONLY | O_NONBLOCK. Requires a reader to be present —
        process_mgr keeps a sentinel fd open on each FIFO to ensure this.
        """
        try:
            fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)
            try:
                data = (message + "\n").encode("utf-8")
                os.write(fd, data)
                return True
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
        was_alive = self.process_mgr.is_alive(stream, topic)
        fifo = await self.process_mgr.ensure_instance(stream, topic, stream_cfg)

        if not was_alive:
            # Notify in Zulip that a new session started
            self._post_message(stream, topic, "\U0001f916 Session started.")

        # Format message with timestamp and source
        now = datetime.now().strftime("%H:%M")
        formatted = f"[{now} zulip] {content}"

        # Write to FIFO
        if not self._write_to_fifo(fifo, formatted):
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

        while self._running:
            try:
                log.info("Registering Zulip event queue...")
                queue_id, last_event_id = await asyncio.get_event_loop().run_in_executor(
                    None, self._register_event_queue
                )
                log.info("Event queue registered: %s", queue_id)
                backoff = INITIAL_BACKOFF  # Reset on success

                while self._running:
                    events = await asyncio.get_event_loop().run_in_executor(
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

    def stop(self) -> None:
        """Signal the adapter to stop."""
        self._running = False
        self.process_mgr.stop_all()
