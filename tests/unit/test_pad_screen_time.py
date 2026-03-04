"""Tests for libs.pad_agent.screen_time.ScreenTimeTracker."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from libs.pad_agent.screen_time import ScreenTimeTracker

HKT = ZoneInfo("Asia/Hong_Kong")


def hkt(h: int, m: int, s: int = 0, date: str = "2026-03-04") -> datetime:
    y, mo, d = map(int, date.split("-"))
    return datetime(y, mo, d, h, m, s, tzinfo=HKT)


class TestScreenTimeTracker:
    """Unit tests for ScreenTimeTracker."""

    # 1. First update initialises state, returns no events.
    def test_first_update_initializes(self) -> None:
        t = ScreenTimeTracker()
        events = t.update(screen_on=True, is_locked=False, now=hkt(10, 0))
        assert events == []
        assert t._last_update_time == hkt(10, 0)
        assert t._date == "2026-03-04"

    # 2. Two updates 30 s apart with screen on, not locked -> 30 s accumulated.
    def test_elapsed_time_accumulation(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 0))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 30))
        assert t._active_seconds_today == pytest.approx(30.0)
        assert t.active_minutes_today == pytest.approx(0.5)

    # 3. Screen off -> no time added.
    def test_no_accumulation_screen_off(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=False, is_locked=False, now=hkt(10, 0, 0))
        t.update(screen_on=False, is_locked=False, now=hkt(10, 0, 30))
        assert t._active_seconds_today == 0.0

    # 4. Screen on but locked -> no time added.
    def test_no_accumulation_when_locked(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=True, is_locked=True, now=hkt(10, 0, 0))
        t.update(screen_on=True, is_locked=True, now=hkt(10, 0, 30))
        assert t._active_seconds_today == 0.0

    # 5. Screen off -> on transition emits session_start.
    def test_session_start_on_screen_on(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=False, is_locked=False, now=hkt(10, 0, 0))
        events = t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 30))
        assert len(events) == 1
        assert events[0]["event"] == "session_start"
        assert t._in_session is True

    # 6. Screen on -> off transition emits session_end with duration.
    def test_session_end_on_screen_off(self) -> None:
        t = ScreenTimeTracker()
        # Init with screen on.
        t.update(screen_on=False, is_locked=False, now=hkt(10, 0, 0))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 0))
        # Accumulate 60 s of active time.
        t.update(screen_on=True, is_locked=False, now=hkt(10, 1, 0))
        # Screen off.
        events = t.update(
            screen_on=False, is_locked=False, now=hkt(10, 1, 30)
        )
        assert len(events) == 1
        assert events[0]["event"] == "session_end"
        assert events[0]["reason"] == "screen_off"
        assert events[0]["duration_min"] == pytest.approx(1.0, abs=0.1)
        assert t._in_session is False

    # 7. Date change resets counters and emits rollover event.
    def test_midnight_rollover_resets(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=True, is_locked=False, now=hkt(23, 59, 0))
        t.update(screen_on=True, is_locked=False, now=hkt(23, 59, 30))
        assert t._active_seconds_today == pytest.approx(30.0)

        # Cross midnight (screen off, no active session).
        t.update(screen_on=False, is_locked=False, now=hkt(23, 59, 50))
        next_day = hkt(0, 0, 10, date="2026-03-05")
        events = t.check_midnight_rollover(now=next_day)

        rollover = [e for e in events if e["event"] == "midnight_rollover"]
        assert len(rollover) == 1
        assert rollover[0]["prev_total_min"] == pytest.approx(30.0 / 60, abs=0.1)
        assert t._active_seconds_today == 0.0
        assert t._date == "2026-03-05"

    # 8. Active session at midnight is ended before rollover.
    def test_midnight_rollover_ends_active_session(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=False, is_locked=False, now=hkt(23, 58, 0))
        t.update(screen_on=True, is_locked=False, now=hkt(23, 59, 0))
        t.update(screen_on=True, is_locked=False, now=hkt(23, 59, 30))
        assert t._in_session is True

        next_day = hkt(0, 0, 5, date="2026-03-05")
        events = t.check_midnight_rollover(now=next_day)

        event_types = [e["event"] for e in events]
        assert event_types == ["session_end", "midnight_rollover"]
        session_end = events[0]
        assert session_end["reason"] == "midnight_rollover"
        assert t._in_session is False

    # 9. record_eye_break resets minutes_since_last_eye_break.
    def test_eye_break_recording(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 0))
        # Use 30s steps to stay under the 120s cap
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 30))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 1, 0))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 1, 30))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 2, 0))
        assert t.minutes_since_last_eye_break == pytest.approx(2.0)

        t.record_eye_break()
        assert t.minutes_since_last_eye_break == pytest.approx(0.0)

    # 10. minutes_since_last_eye_break counts only active time.
    def test_minutes_since_eye_break_active_only(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 0))
        # 60 s active.
        t.update(screen_on=True, is_locked=False, now=hkt(10, 1, 0))
        t.record_eye_break()

        # 30 s active.
        t.update(screen_on=True, is_locked=False, now=hkt(10, 1, 30))
        # 60 s locked — should NOT count.
        t.update(screen_on=True, is_locked=True, now=hkt(10, 2, 30))
        # 30 s active again.
        t.update(screen_on=True, is_locked=False, now=hkt(10, 3, 0))

        # Only 60 s of active time since the eye break (30 + 30).
        assert t.minutes_since_last_eye_break == pytest.approx(1.0)

    # 11. reset_session clears session seconds and eye break timer.
    def test_reset_session(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 0))
        # Use 30s steps to stay under the 120s cap
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 30))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 1, 0))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 1, 30))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 2, 0))
        assert t.current_session_minutes == pytest.approx(2.0)
        assert t.minutes_since_last_eye_break == pytest.approx(2.0)

        t.reset_session()
        assert t.current_session_minutes == 0.0
        assert t.minutes_since_last_eye_break == 0.0

    # 12. State snapshot roundtrip preserves all fields.
    def test_state_snapshot_restore(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=False, is_locked=False, now=hkt(10, 0, 0))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 30))
        t.update(screen_on=True, is_locked=False, now=hkt(10, 1, 0))

        snap = t.get_state_snapshot()

        t2 = ScreenTimeTracker()
        t2.restore_from_state(snap)

        assert t2._date == t._date
        assert t2._active_seconds_today == pytest.approx(
            t._active_seconds_today
        )
        assert t2._current_session_seconds == pytest.approx(
            t._current_session_seconds
        )
        assert t2._last_eye_break_active_seconds == pytest.approx(
            t._last_eye_break_active_seconds
        )
        assert t2._prev_screen_on == t._prev_screen_on
        assert t2._in_session == t._in_session
        assert t2._last_update_time == t._last_update_time
        assert t2._current_session_start == t._current_session_start

    # 13. Large gap (server suspend) capped at 120 s.
    def test_elapsed_capped_at_120s(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=True, is_locked=False, now=hkt(10, 0, 0))
        # 10-minute gap — should be capped to 120 s.
        t.update(screen_on=True, is_locked=False, now=hkt(10, 10, 0))
        assert t._active_seconds_today == pytest.approx(120.0)

    # 14. Explicit Asia/Hong_Kong timezone is used.
    def test_timezone_explicit_hkt(self) -> None:
        t = ScreenTimeTracker(timezone="Asia/Hong_Kong")
        assert t._tz == HKT

    # 15. active_minutes_today returns seconds / 60.
    def test_active_minutes_property(self) -> None:
        t = ScreenTimeTracker()
        t._active_seconds_today = 180.0
        assert t.active_minutes_today == pytest.approx(3.0)

    # 16. Screen on/off while locked still tracks sessions but time
    #     does not accumulate.
    def test_session_transitions_while_locked(self) -> None:
        t = ScreenTimeTracker()
        t.update(screen_on=False, is_locked=True, now=hkt(10, 0, 0))
        # Screen turns on while locked.
        events_on = t.update(
            screen_on=True, is_locked=True, now=hkt(10, 0, 30)
        )
        assert any(e["event"] == "session_start" for e in events_on)
        assert t._in_session is True

        # 30 s pass while locked — no active time.
        t.update(screen_on=True, is_locked=True, now=hkt(10, 1, 0))
        assert t._active_seconds_today == 0.0

        # Screen turns off while still locked.
        events_off = t.update(
            screen_on=False, is_locked=True, now=hkt(10, 1, 30)
        )
        assert any(e["event"] == "session_end" for e in events_off)
        assert t._in_session is False
        # Still no active time accumulated.
        assert t._active_seconds_today == 0.0
