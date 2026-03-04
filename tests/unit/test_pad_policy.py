"""Unit tests for pad_agent policy engine."""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

import pytest

from libs.pad_agent.policy import (
    LockReason,
    PolicyConfig,
    PolicyEvaluation,
    evaluate,
    load_policy,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_policy(**overrides: object) -> PolicyConfig:
    """Build a PolicyConfig with sensible defaults; override any field."""
    defaults: dict = dict(
        daily_limit_minutes=60,
        session_limit_minutes=30,
        eye_break_interval_minutes=20,
        eye_break_duration_seconds=20,
        bedtime_start=datetime.time(21, 0),
        bedtime_end=datetime.time(7, 0),
        heartbeat_timeout_seconds=300,
        timezone="Asia/Hong_Kong",
    )
    defaults.update(overrides)
    return PolicyConfig(**defaults)


_VALID_POLICY_JSON: dict = {
    "version": 1,
    "child_name": "TestChild",
    "timezone": "Asia/Hong_Kong",
    "screen_time": {
        "daily_limit_minutes": 60,
        "session_limit_minutes": 30,
        "eye_break_interval_minutes": 20,
        "eye_break_duration_seconds": 20,
    },
    "bedtime": {"start": "21:00", "end": "07:00"},
    "heartbeat": {"timeout_seconds": 300},
    "allowed_apps": {},
    "fully_kiosk_package": "de.ozerov.fully",
}


def _write_policy(tmp_path: Path, data: dict | None = None) -> Path:
    """Write policy JSON to a temp file and return the path."""
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(data or _VALID_POLICY_JSON, indent=2))
    return p


# ---------------------------------------------------------------------------
# TestPolicyConfig
# ---------------------------------------------------------------------------


class TestPolicyConfig:
    def test_frozen(self) -> None:
        cfg = _default_policy()
        with pytest.raises(AttributeError):
            cfg.daily_limit_minutes = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestLoadPolicy
# ---------------------------------------------------------------------------


class TestLoadPolicy:
    def test_load_valid(self, tmp_path: Path) -> None:
        path = _write_policy(tmp_path)
        cfg = load_policy(path)
        assert cfg.daily_limit_minutes == 60
        assert cfg.session_limit_minutes == 30
        assert cfg.eye_break_interval_minutes == 20
        assert cfg.eye_break_duration_seconds == 20
        assert cfg.bedtime_start == datetime.time(21, 0)
        assert cfg.bedtime_end == datetime.time(7, 0)
        assert cfg.heartbeat_timeout_seconds == 300
        assert cfg.timezone == "Asia/Hong_Kong"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_policy(tmp_path / "nonexistent.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "policy.json"
        p.write_text("{bad json")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_policy(p)

    def test_missing_key_raises(self, tmp_path: Path) -> None:
        bad = {**_VALID_POLICY_JSON}
        del bad["screen_time"]
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match="Invalid policy structure"):
            load_policy(p)


# ---------------------------------------------------------------------------
# TestEvaluate
# ---------------------------------------------------------------------------


class TestEvaluate:
    """Tests for the pure evaluate() function."""

    # -- bedtime -----------------------------------------------------------

    def test_bedtime_inside_22h(self) -> None:
        """22:00 is inside 21:00-07:00 -> BEDTIME lock."""
        policy = _default_policy()
        now = datetime.datetime(2026, 3, 4, 22, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=0,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.BEDTIME in result.lock_reasons

    def test_bedtime_outside_08h(self) -> None:
        """08:00 is outside 21:00-07:00 -> BEDTIME unlock."""
        policy = _default_policy()
        now = datetime.datetime(2026, 3, 4, 8, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=0,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.BEDTIME in result.unlock_reasons
        assert LockReason.BEDTIME not in result.lock_reasons

    def test_bedtime_inside_06h(self) -> None:
        """06:00 is inside 21:00-07:00 (before end) -> BEDTIME lock."""
        policy = _default_policy()
        now = datetime.datetime(2026, 3, 4, 6, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=0,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.BEDTIME in result.lock_reasons

    def test_bedtime_inside_midnight(self) -> None:
        """00:00 is inside 21:00-07:00 -> BEDTIME lock."""
        policy = _default_policy()
        now = datetime.datetime(2026, 3, 4, 0, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=0,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.BEDTIME in result.lock_reasons

    # -- daily limit -------------------------------------------------------

    def test_daily_limit_reached(self) -> None:
        policy = _default_policy(daily_limit_minutes=60)
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=60,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.DAILY_LIMIT in result.lock_reasons
        assert result.daily_limit_reached is True

    def test_daily_limit_not_reached(self) -> None:
        policy = _default_policy(daily_limit_minutes=60)
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=30,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.DAILY_LIMIT in result.unlock_reasons
        assert LockReason.DAILY_LIMIT not in result.lock_reasons
        assert result.daily_limit_reached is False

    # -- session limit -----------------------------------------------------

    def test_session_limit_reached(self) -> None:
        policy = _default_policy(session_limit_minutes=30)
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=10,
            current_session_minutes=30,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.SESSION_LIMIT in result.lock_reasons
        assert result.session_limit_reached is True

    # -- eye break ---------------------------------------------------------

    def test_eye_break_due(self) -> None:
        """Interval elapsed + not locked -> eye_break_due."""
        policy = _default_policy(eye_break_interval_minutes=20)
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=25,
            current_session_minutes=25,
            minutes_since_last_eye_break=20,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
            is_locked=False,
        )
        assert result.eye_break_due is True

    def test_eye_break_not_due_when_locked(self) -> None:
        """Interval elapsed but device is locked -> NOT due."""
        policy = _default_policy(eye_break_interval_minutes=20)
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=25,
            current_session_minutes=25,
            minutes_since_last_eye_break=20,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
            is_locked=True,
        )
        assert result.eye_break_due is False

    def test_eye_break_expired(self) -> None:
        """Break started long enough ago -> expired."""
        policy = _default_policy(eye_break_duration_seconds=20)
        now = datetime.datetime(2026, 3, 4, 12, 0, 30, tzinfo=datetime.timezone.utc)
        started = datetime.datetime(2026, 3, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=0,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=started,
            now=now,
        )
        assert result.eye_break_expired is True

    # -- heartbeat ---------------------------------------------------------

    def test_heartbeat_timeout(self) -> None:
        policy = _default_policy(heartbeat_timeout_seconds=300)
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=0,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=301,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.HEARTBEAT_TIMEOUT in result.lock_reasons

    # -- contract: EYE_BREAK never in lock/unlock reasons ------------------

    def test_eye_break_never_in_lock_or_unlock_reasons(self) -> None:
        """CONTRACT: EYE_BREAK must never appear in lock_reasons or unlock_reasons."""
        policy = _default_policy(eye_break_interval_minutes=1)
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
        started = datetime.datetime(2026, 3, 4, 11, 59, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=100,
            current_session_minutes=100,
            minutes_since_last_eye_break=100,
            last_heartbeat_age_seconds=9999,
            eye_break_started_at=started,
            now=now,
            is_locked=False,
        )
        assert LockReason.EYE_BREAK not in result.lock_reasons
        assert LockReason.EYE_BREAK not in result.unlock_reasons

    # -- multiple concurrent reasons ---------------------------------------

    def test_multiple_concurrent_reasons(self) -> None:
        """Both BEDTIME and DAILY_LIMIT can fire at the same time."""
        policy = _default_policy(daily_limit_minutes=60)
        # 22:00 = inside bedtime, and 60 minutes = at daily limit
        now = datetime.datetime(2026, 3, 4, 22, 0, tzinfo=datetime.timezone.utc)
        result = evaluate(
            policy,
            active_minutes_today=60,
            current_session_minutes=0,
            minutes_since_last_eye_break=0,
            last_heartbeat_age_seconds=0,
            eye_break_started_at=None,
            now=now,
        )
        assert LockReason.BEDTIME in result.lock_reasons
        assert LockReason.DAILY_LIMIT in result.lock_reasons
        assert result.daily_limit_reached is True
