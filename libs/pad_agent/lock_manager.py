"""Lock-reason state machine, FK REST API control."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .adb_manager import ADBManager
from .constants import (
    FK_CMD_CLEAR_CACHE,
    FK_CMD_DEVICE_INFO,
    FK_CMD_LOAD_URL,
    FK_CMD_SET_STRING,
    FK_REST_MAX_RETRIES,
    FK_REST_TIMEOUT_SECONDS,
)
from .policy import LockReason, PolicyEvaluation

log = logging.getLogger(__name__)

_MAX_REASON_HISTORY = 100

# Priority order: highest → lowest.
_REASON_PRIORITY: list[LockReason] = [
    LockReason.BEDTIME,
    LockReason.DAILY_LIMIT,
    LockReason.SESSION_LIMIT,
    LockReason.EYE_BREAK,
    LockReason.HEARTBEAT_TIMEOUT,
    LockReason.MANUAL,
]


class LockManager:
    """Manages device lock/unlock state via FK REST API."""

    def __init__(
        self,
        adb_mgr: ADBManager,
        fk_password: str,
        fk_base_url: str,
        lock_url: str,
        dashboard_url: str,
    ) -> None:
        self._adb_mgr = adb_mgr
        self._fk_password = fk_password
        self._fk_base_url = fk_base_url  # "http://<tailscale_ip>:2323"
        self._lock_url = lock_url  # "file:///sdcard/kidpad/lock.html"
        self._dashboard_url = dashboard_url
        self._active_reasons: set[LockReason] = set()
        self._eye_break_started_at: datetime | None = None
        self._reason_history: list[dict] = []  # capped at 100

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_locked(self) -> bool:
        return len(self._active_reasons) > 0

    @property
    def active_reasons(self) -> list[str]:
        """Public accessor for active lock reasons (as string values)."""
        return [r.value for r in self._active_reasons]

    @property
    def eye_break_started_at(self) -> datetime | None:
        return self._eye_break_started_at

    # ------------------------------------------------------------------
    # Reason management
    # ------------------------------------------------------------------

    def add_reason(self, reason: LockReason, now: datetime | None = None) -> bool:
        """Add a lock reason.

        Returns True if this caused a state transition (UNLOCKED -> LOCKED).
        """
        if reason in self._active_reasons:
            return False

        was_empty = len(self._active_reasons) == 0
        self._active_reasons.add(reason)

        if reason == LockReason.EYE_BREAK:
            self._eye_break_started_at = now

        self._record_history("add", reason, now)

        if was_empty:
            self._lock_device(self._get_primary_reason())
            return True
        return False

    def remove_reason(self, reason: LockReason, now: datetime | None = None) -> bool:
        """Remove a lock reason.

        Returns True if this caused a state transition (LOCKED -> UNLOCKED).
        """
        if reason not in self._active_reasons:
            return False

        self._active_reasons.discard(reason)

        if reason == LockReason.EYE_BREAK:
            self._eye_break_started_at = None

        self._record_history("remove", reason, now)

        if len(self._active_reasons) == 0:
            self._unlock_device()
            return True
        return False

    def apply_evaluation(
        self, evaluation: PolicyEvaluation
    ) -> list[tuple[str, LockReason]]:
        """Apply a policy evaluation result.

        Skips EYE_BREAK (handled separately by the monitor loop).
        Returns list of (action, reason) tuples for each add/remove.
        """
        transitions: list[tuple[str, LockReason]] = []

        for reason in evaluation.lock_reasons:
            if reason == LockReason.EYE_BREAK:
                continue
            if reason not in self._active_reasons:
                self.add_reason(reason)
                transitions.append(("lock", reason))

        for reason in evaluation.unlock_reasons:
            if reason == LockReason.EYE_BREAK:
                continue
            if reason in self._active_reasons:
                self.remove_reason(reason)
                transitions.append(("unlock", reason))

        return transitions

    # ------------------------------------------------------------------
    # Device state enforcement
    # ------------------------------------------------------------------

    def assert_device_state(self) -> None:
        """Force device into the correct state based on active reasons."""
        self.check_fk_alive()
        if self.is_locked:
            self._lock_device(self._get_primary_reason())
        else:
            self._unlock_device()

    def check_fk_alive(self) -> bool:
        """Check if Fully Kiosk process is running on the device."""
        try:
            output = self._adb_mgr.shell("pidof de.ozerov.fully")
            return len(output.strip()) > 0
        except Exception:
            return False

    def verify_fk_page(self) -> str | None:
        """Query FK REST API for the current page URL."""
        result = self._fk_request({
            "cmd": FK_CMD_DEVICE_INFO,
            "type": "json",
            "password": self._fk_password,
        })
        if result is None:
            return None
        return result.get("currentPage")

    # ------------------------------------------------------------------
    # FK REST API interaction
    # ------------------------------------------------------------------

    def _lock_device(self, primary_reason: str = "manual") -> None:
        """Navigate FK to lock.html with the primary reason."""
        url = f"{self._lock_url}?reason={primary_reason}"
        log.info("Locking device: %s", url)
        self._fk_request({
            "cmd": FK_CMD_LOAD_URL,
            "url": url,
            "password": self._fk_password,
            "type": "json",
        })

    def _unlock_device(self) -> None:
        """Navigate FK to the dashboard URL."""
        log.info("Unlocking device: %s", self._dashboard_url)
        self._fk_request({
            "cmd": FK_CMD_LOAD_URL,
            "url": self._dashboard_url,
            "password": self._fk_password,
            "type": "json",
        })

    def _get_primary_reason(self) -> str:
        """Return the highest-priority active reason."""
        for reason in _REASON_PRIORITY:
            if reason in self._active_reasons:
                return reason.value
        return "manual"

    def _fk_request(self, params: dict) -> dict | None:
        """Send a GET request to the FK REST API with retry."""
        for attempt in range(1 + FK_REST_MAX_RETRIES):
            try:
                resp = requests.get(
                    self._fk_base_url,
                    params=params,
                    timeout=FK_REST_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning(
                    "FK request failed (attempt %d/%d): %s",
                    attempt + 1,
                    1 + FK_REST_MAX_RETRIES,
                    exc,
                )
        return None

    # ------------------------------------------------------------------
    # History helpers
    # ------------------------------------------------------------------

    def _record_history(
        self, action: str, reason: LockReason, now: datetime | None
    ) -> None:
        """Append to reason history, capping at 100 entries."""
        ts = (now or datetime.now(timezone.utc)).isoformat()
        self._reason_history.append({
            "ts": ts,
            "action": action,
            "reason": reason.value,
        })
        if len(self._reason_history) > _MAX_REASON_HISTORY:
            self._reason_history = self._reason_history[-_MAX_REASON_HISTORY:]

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def get_state_snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot."""
        return {
            "is_locked": self.is_locked,
            "active_reasons": [r.value for r in self._active_reasons],
            "eye_break_started_at": (
                self._eye_break_started_at.isoformat()
                if self._eye_break_started_at
                else None
            ),
            "reason_history": self._reason_history,
        }

    def restore_from_state(self, state: dict) -> None:
        """Restore state from a saved snapshot."""
        self._active_reasons = set()
        for rv in state.get("active_reasons", []):
            try:
                self._active_reasons.add(LockReason(rv))
            except ValueError:
                log.warning("Unknown lock reason in state: %s", rv)

        eb_raw = state.get("eye_break_started_at")
        self._eye_break_started_at = (
            datetime.fromisoformat(eb_raw) if eb_raw else None
        )

        self._reason_history = state.get("reason_history", [])
