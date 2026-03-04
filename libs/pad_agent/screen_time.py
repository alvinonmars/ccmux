"""Active screen time tracker, elapsed-time based, midnight rollover."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Maximum elapsed seconds to accumulate in a single cycle.
# Anything larger (e.g. server suspend) is clamped to this value.
_MAX_ELAPSED_SECONDS = 120.0


class ScreenTimeTracker:
    """Tracks active screen time using elapsed wall-clock deltas.

    Only counts time when the screen is **on** AND the device is **not locked**.
    Session lifecycle (start/end) is tracked even while locked, but accumulated
    time does not grow while the screen is off or the device is locked.
    """

    def __init__(self, timezone: str = "Asia/Hong_Kong") -> None:
        self._tz = ZoneInfo(timezone)
        self._active_seconds_today: float = 0.0
        self._current_session_seconds: float = 0.0
        self._current_session_start: datetime | None = None
        # Active-seconds reading at the moment of the last eye break.
        self._last_eye_break_active_seconds: float = 0.0
        self._last_update_time: datetime | None = None
        self._prev_screen_on: bool = False
        self._in_session: bool = False
        self._date: str = ""  # current tracking date as YYYY-MM-DD

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    def update(
        self,
        screen_on: bool,
        is_locked: bool,
        now: datetime | None = None,
    ) -> list[dict]:
        """Called every cycle (~30 s).  Returns a list of event dicts."""
        if now is None:
            now = datetime.now(self._tz)

        events: list[dict] = []

        # 1. First call — initialise bookkeeping, nothing to compute yet.
        if self._last_update_time is None:
            self._last_update_time = now
            self._date = now.date().isoformat()
            self._prev_screen_on = screen_on
            return events

        # 2. Elapsed time since previous cycle (clamped).
        elapsed = max(0.0, (now - self._last_update_time).total_seconds())
        elapsed = min(elapsed, _MAX_ELAPSED_SECONDS)

        # 3. Session transitions (before accumulation).
        if not self._prev_screen_on and screen_on:
            # Screen turned on → start new session.
            self._in_session = True
            self._current_session_start = now
            self._current_session_seconds = 0.0
            events.append({"ts": now.isoformat(), "event": "session_start"})

        elif self._prev_screen_on and not screen_on:
            # Screen turned off → end session.
            events.append({
                "ts": now.isoformat(),
                "event": "session_end",
                "duration_min": round(self._current_session_seconds / 60, 1),
                "reason": "screen_off",
            })
            self._in_session = False
            self._current_session_start = None

        # 4. Accumulate active time (screen on AND not locked).
        if screen_on and not is_locked:
            self._active_seconds_today += elapsed
            self._current_session_seconds += elapsed

        # 5. Bookkeeping.
        self._last_update_time = now
        self._prev_screen_on = screen_on

        return events

    # ------------------------------------------------------------------
    # Midnight rollover
    # ------------------------------------------------------------------

    def check_midnight_rollover(
        self, now: datetime | None = None
    ) -> list[dict]:
        """Check for date change and reset counters if needed.

        Returns a list of events (empty when no rollover occurred).
        """
        if now is None:
            now = datetime.now(self._tz)

        if not self._date or now.date().isoformat() == self._date:
            return []

        events: list[dict] = []

        # End any active session first.
        if self._in_session:
            events.append({
                "ts": now.isoformat(),
                "event": "session_end",
                "duration_min": round(
                    self._current_session_seconds / 60, 1
                ),
                "reason": "midnight_rollover",
            })
            self._in_session = False
            self._current_session_start = None

        events.append({
            "ts": now.isoformat(),
            "event": "midnight_rollover",
            "prev_total_min": round(self._active_seconds_today / 60, 1),
        })

        # Reset counters for the new day.
        self._active_seconds_today = 0.0
        self._current_session_seconds = 0.0
        self._last_eye_break_active_seconds = 0.0
        self._date = now.date().isoformat()

        return events

    # ------------------------------------------------------------------
    # Eye break helpers
    # ------------------------------------------------------------------

    def record_eye_break(self, now: datetime | None = None) -> None:
        """Record that an eye break is starting now."""
        self._last_eye_break_active_seconds = self._active_seconds_today

    def reset_session(self) -> None:
        """Reset current session counters AND eye break timer.

        Intended for use on non-eye-break unlock.
        """
        self._current_session_seconds = 0.0
        self._last_eye_break_active_seconds = self._active_seconds_today

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_minutes_today(self) -> float:
        return self._active_seconds_today / 60

    @property
    def current_session_minutes(self) -> float:
        return self._current_session_seconds / 60

    @property
    def minutes_since_last_eye_break(self) -> float:
        """Minutes of *active* screen time since the last eye break."""
        return (
            self._active_seconds_today - self._last_eye_break_active_seconds
        ) / 60

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def get_state_snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of all internal state."""
        return {
            "date": self._date,
            "active_seconds_today": self._active_seconds_today,
            "current_session_start": (
                self._current_session_start.isoformat()
                if self._current_session_start
                else None
            ),
            "current_session_seconds": self._current_session_seconds,
            "last_eye_break_active_seconds": self._last_eye_break_active_seconds,
            "last_update_time": (
                self._last_update_time.isoformat()
                if self._last_update_time
                else None
            ),
            "screen_on": self._prev_screen_on,
            "in_session": self._in_session,
        }

    def restore_from_state(self, state: dict) -> None:
        """Restore internal state from a previously saved snapshot."""
        self._date = state.get("date", "")
        self._active_seconds_today = float(
            state.get("active_seconds_today", 0.0)
        )
        self._current_session_seconds = float(
            state.get("current_session_seconds", 0.0)
        )
        self._last_eye_break_active_seconds = float(
            state.get("last_eye_break_active_seconds", 0.0)
        )
        self._prev_screen_on = bool(state.get("screen_on", False))
        self._in_session = bool(state.get("in_session", False))

        # Parse ISO datetime strings back into aware datetime objects.
        cs_raw = state.get("current_session_start")
        self._current_session_start = (
            datetime.fromisoformat(cs_raw) if cs_raw else None
        )

        lu_raw = state.get("last_update_time")
        self._last_update_time = (
            datetime.fromisoformat(lu_raw) if lu_raw else None
        )
