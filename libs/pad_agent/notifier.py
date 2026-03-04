"""FIFO writer to in.pad channel."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .constants import PIPE_BUF

log = logging.getLogger(__name__)


class PadNotifier:
    """Sends structured JSON messages to the pad FIFO channel."""

    def __init__(self, runtime_dir: Path) -> None:
        self._fifo_path = runtime_dir / "in.pad"

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------

    def notify(
        self,
        event: str,
        content: str,
        child_name: str,
        meta: dict | None = None,
    ) -> bool:
        """Write a JSON message to the FIFO.

        Returns True on success, False if FIFO is unavailable.
        """
        payload: dict = {
            "channel": "pad",
            "event": event,
            "content": content,
            "child": child_name,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if meta:
            payload.update(meta)

        raw = json.dumps(payload)

        # Ensure atomic write: truncate content if payload exceeds PIPE_BUF.
        if len(raw.encode("utf-8")) >= PIPE_BUF:
            # Shrink content until it fits.
            overhead = len(json.dumps({**payload, "content": ""}).encode("utf-8"))
            max_content_bytes = PIPE_BUF - overhead - 1  # leave 1 byte margin
            truncated = content.encode("utf-8")[:max_content_bytes].decode(
                "utf-8", errors="ignore"
            )
            payload["content"] = truncated
            raw = json.dumps(payload)

        try:
            fd = os.open(str(self._fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            log.warning("FIFO not found: %s", self._fifo_path)
            return False
        except OSError as exc:
            log.warning("Cannot open FIFO %s: %s", self._fifo_path, exc)
            return False

        try:
            os.write(fd, raw.encode("utf-8"))
            return True
        except OSError as exc:
            log.warning("Failed to write to FIFO %s: %s", self._fifo_path, exc)
            return False
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def notify_screen_time_update(
        self,
        child_name: str,
        active_min: float,
        daily_limit: int,
        session_min: float,
        session_limit: int,
    ) -> bool:
        """Send a screen_time_update event."""
        return self.notify(
            event="screen_time_update",
            content=(
                f"Active: {active_min:.1f}/{daily_limit} min, "
                f"Session: {session_min:.1f}/{session_limit} min"
            ),
            child_name=child_name,
            meta={
                "active_min": active_min,
                "daily_limit": daily_limit,
                "session_min": session_min,
                "session_limit": session_limit,
            },
        )

    def notify_lock_change(
        self,
        child_name: str,
        action: str,
        reason: str,
        active_reasons: list[str],
    ) -> bool:
        """Send a lock_change event."""
        return self.notify(
            event="lock_change",
            content=f"{action}: {reason}",
            child_name=child_name,
            meta={
                "action": action,
                "reason": reason,
                "active_reasons": active_reasons,
            },
        )

    def notify_adb_status(
        self,
        child_name: str,
        status: str,
        reconnect_attempts: int = 0,
    ) -> bool:
        """Send an adb_status event."""
        return self.notify(
            event="adb_status",
            content=f"ADB {status}",
            child_name=child_name,
            meta={
                "status": status,
                "reconnect_attempts": reconnect_attempts,
            },
        )
