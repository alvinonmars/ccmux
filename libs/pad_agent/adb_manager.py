"""ADB connection manager with exponential backoff reconnection.

All methods are SYNCHRONOUS (no asyncio).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from enum import Enum

from .adb import ADB, ADBError

log = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


_RECONNECT_DISCONNECT_THRESHOLD = 3   # targeted disconnect after N failures
_RECONNECT_KILL_SERVER_THRESHOLD = 10  # kill-server as last resort


class ADBManager:
    """Manages ADB connection lifecycle with automatic reconnection."""

    def __init__(
        self,
        host: str,
        port: int = 5555,
        backoff_initial: float = 1.0,
        backoff_cap: float = 120.0,
    ) -> None:
        self._host = host
        self._port = port
        self._adb = ADB()  # no serial initially
        self._state = ConnectionState.DISCONNECTED
        self._backoff = backoff_initial
        self._backoff_initial = backoff_initial
        self._backoff_cap = backoff_cap
        self._reconnect_attempts = 0
        self._last_connected: datetime | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def adb(self) -> ADB:
        return self._adb

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Initial connection to device. Returns True on success."""
        try:
            self._adb.connect_wireless(self._host, self._port)
            self._state = ConnectionState.CONNECTED
            self._last_connected = datetime.now(timezone.utc)
            log.info("Connected to %s:%d", self._host, self._port)
            return True
        except ADBError as exc:
            self._state = ConnectionState.DISCONNECTED
            log.warning("Connection failed: %s", exc)
            return False

    def ensure_connected(self) -> bool:
        """Ensure device is reachable. Reconnects if needed."""
        if self._state == ConnectionState.CONNECTED:
            if self.is_device_responsive():
                return True
            log.warning("Device unresponsive, attempting reconnect")
            return self.reconnect()
        return self.reconnect()

    def reconnect(self) -> bool:
        """Attempt reconnection with exponential backoff."""
        self._state = ConnectionState.RECONNECTING

        try:
            self._adb.connect_wireless(self._host, self._port)
            self._state = ConnectionState.CONNECTED
            self._last_connected = datetime.now(timezone.utc)
            self.reset_backoff()
            log.info("Reconnected to %s:%d", self._host, self._port)
            return True
        except ADBError as exc:
            self._reconnect_attempts += 1
            log.warning(
                "Reconnect attempt %d failed: %s", self._reconnect_attempts, exc
            )

            # Targeted disconnect after repeated failures
            if self._reconnect_attempts >= _RECONNECT_DISCONNECT_THRESHOLD:
                target = f"{self._host}:{self._port}"
                log.info("Targeted disconnect of %s after %d failures",
                         target, self._reconnect_attempts)
                self._adb.run(["disconnect", target], check=False)

            # Kill server as last resort
            if self._reconnect_attempts >= _RECONNECT_KILL_SERVER_THRESHOLD:
                log.warning("Killing ADB server after %d failures",
                            self._reconnect_attempts)
                self._adb.run(["kill-server"], check=False)
                time.sleep(2)

            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, self._backoff_cap)
            self._state = ConnectionState.DISCONNECTED
            return False

    def is_device_responsive(self) -> bool:
        """Quick liveness check via shell echo."""
        try:
            output = self._adb.shell("echo ok", timeout=3)
            return "ok" in output
        except ADBError:
            return False

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def reset_backoff(self) -> None:
        """Reset backoff to initial value."""
        self._backoff = self._backoff_initial
        self._reconnect_attempts = 0

    def mark_disconnected(self) -> None:
        """Externally mark device as disconnected."""
        self._state = ConnectionState.DISCONNECTED

    # ------------------------------------------------------------------
    # Device operations
    # ------------------------------------------------------------------

    def push_file(self, local_path: str, remote_path: str) -> None:
        """Push a file to the device."""
        self._adb.run(["push", local_path, remote_path], timeout=30)

    def shell(self, cmd: str, timeout: int = 10) -> str:
        """Run a shell command on the device. Requires CONNECTED state."""
        if self._state != ConnectionState.CONNECTED:
            raise ADBError(
                f"Cannot run shell command: device is {self._state.value}"
            )
        return self._adb.shell(cmd, timeout=timeout)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def get_state_snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of connection state."""
        return {
            "status": self._state.value,
            "device_serial": f"{self._host}:{self._port}",
            "last_connected": (
                self._last_connected.isoformat() if self._last_connected else None
            ),
            "reconnect_attempts": self._reconnect_attempts,
            "backoff_seconds": self._backoff,
        }

    def restore_from_state(self, state: dict) -> None:
        """Restore reconnect state from a saved snapshot. Does NOT auto-connect."""
        self._reconnect_attempts = int(state.get("reconnect_attempts", 0))
        self._backoff = float(state.get("backoff_seconds", self._backoff_initial))
