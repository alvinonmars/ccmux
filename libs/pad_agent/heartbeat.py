"""Server heartbeat mechanism."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class HeartbeatManager:
    """Track the server-side heartbeat cycle for a monitored device."""

    def __init__(self, timeout_seconds: int = 300) -> None:
        self._timeout_seconds = timeout_seconds
        self._seq = 0
        self._last_cycle_mono = time.monotonic()

    # -- public API --------------------------------------------------------

    def record_successful_cycle(self) -> None:
        """Mark a successful monitoring cycle (device reachable)."""
        self._seq += 1
        self._last_cycle_mono = time.monotonic()
        log.debug("Heartbeat seq=%d recorded", self._seq)

    def is_timed_out(self) -> bool:
        """Return ``True`` if the device has not been seen within the timeout."""
        return (time.monotonic() - self._last_cycle_mono) > self._timeout_seconds

    @property
    def last_seen_seconds_ago(self) -> float:
        """Seconds since the last successful cycle (monotonic)."""
        return time.monotonic() - self._last_cycle_mono

    @property
    def seq(self) -> int:
        """Current sequence number."""
        return self._seq

    # -- state persistence -------------------------------------------------

    def get_state_snapshot(self) -> dict:
        """Return serialisable state for persistence.

        Note: monotonic clock is **not** persisted -- only wall-clock and seq.
        """
        return {
            "last_device_seen": datetime.now(timezone.utc).isoformat(),
            "seq": self._seq,
        }

    def restore_from_state(self, state: dict) -> None:
        """Restore from a previously saved snapshot.

        Resets ``_last_cycle_mono`` to *now* so the manager does not start
        in a timed-out state after a restart.
        """
        self._seq = state.get("seq", 0)
        self._last_cycle_mono = time.monotonic()
        log.info(
            "Heartbeat restored: seq=%d, monotonic reset", self._seq,
        )
