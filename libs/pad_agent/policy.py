"""Policy engine: load policy.json, evaluate rules (pure functions)."""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from .constants import TZ

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class LockReason(str, Enum):
    BEDTIME = "bedtime"
    DAILY_LIMIT = "daily_limit"
    SESSION_LIMIT = "session_limit"
    EYE_BREAK = "eye_break"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    MANUAL = "manual"


@dataclass(frozen=True)
class PolicyConfig:
    daily_limit_minutes: int
    session_limit_minutes: int
    eye_break_interval_minutes: int
    eye_break_duration_seconds: int
    bedtime_start: datetime.time
    bedtime_end: datetime.time
    heartbeat_timeout_seconds: int
    timezone: str = "Asia/Hong_Kong"


@dataclass
class PolicyEvaluation:
    lock_reasons: set[LockReason]
    unlock_reasons: set[LockReason]
    eye_break_due: bool
    eye_break_expired: bool
    daily_limit_reached: bool
    session_limit_reached: bool


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_policy(path: Path) -> PolicyConfig:
    """Load and parse policy.json.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError: if the JSON is malformed or missing required fields.
    """
    if not path.exists():
        raise FileNotFoundError(f"Policy file not found: {path}")

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    try:
        screen = raw["screen_time"]
        bedtime = raw["bedtime"]
        heartbeat = raw["heartbeat"]

        bedtime_start = datetime.time.fromisoformat(bedtime["start"])
        bedtime_end = datetime.time.fromisoformat(bedtime["end"])

        return PolicyConfig(
            daily_limit_minutes=int(screen["daily_limit_minutes"]),
            session_limit_minutes=int(screen["session_limit_minutes"]),
            eye_break_interval_minutes=int(screen["eye_break_interval_minutes"]),
            eye_break_duration_seconds=int(screen["eye_break_duration_seconds"]),
            bedtime_start=bedtime_start,
            bedtime_end=bedtime_end,
            heartbeat_timeout_seconds=int(heartbeat["timeout_seconds"]),
            timezone=raw.get("timezone", "Asia/Hong_Kong"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid policy structure in {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------


def evaluate(
    policy: PolicyConfig,
    active_minutes_today: float,
    current_session_minutes: float,
    minutes_since_last_eye_break: float,
    last_heartbeat_age_seconds: float,
    eye_break_started_at: datetime.datetime | None,
    now: datetime.datetime | None = None,
    is_locked: bool = False,
) -> PolicyEvaluation:
    """Evaluate policy rules against current state.

    This is a **pure function** -- no I/O, no side effects.

    CONTRACT: evaluate() NEVER puts EYE_BREAK in lock_reasons or
    unlock_reasons.  It only sets the eye_break_due and eye_break_expired
    flags.
    """
    if now is None:
        now = datetime.datetime.now(ZoneInfo(policy.timezone))

    lock_reasons: set[LockReason] = set()
    unlock_reasons: set[LockReason] = set()
    eye_break_due = False
    eye_break_expired = False
    daily_limit_reached = False
    session_limit_reached = False

    # 1. Bedtime ----------------------------------------------------------
    current_time = now.time()
    if _is_bedtime(current_time, policy.bedtime_start, policy.bedtime_end):
        lock_reasons.add(LockReason.BEDTIME)
    else:
        unlock_reasons.add(LockReason.BEDTIME)

    # 2. Daily limit ------------------------------------------------------
    if active_minutes_today >= policy.daily_limit_minutes:
        lock_reasons.add(LockReason.DAILY_LIMIT)
        daily_limit_reached = True
    else:
        unlock_reasons.add(LockReason.DAILY_LIMIT)

    # 3. Session limit ----------------------------------------------------
    if current_session_minutes >= policy.session_limit_minutes:
        lock_reasons.add(LockReason.SESSION_LIMIT)
        session_limit_reached = True
    else:
        unlock_reasons.add(LockReason.SESSION_LIMIT)

    # 4. Eye break --------------------------------------------------------
    if (
        minutes_since_last_eye_break >= policy.eye_break_interval_minutes
        and not is_locked
    ):
        eye_break_due = True

    if eye_break_started_at is not None:
        elapsed = (now - eye_break_started_at).total_seconds()
        if elapsed >= policy.eye_break_duration_seconds:
            eye_break_expired = True

    # 5. Heartbeat --------------------------------------------------------
    if last_heartbeat_age_seconds > policy.heartbeat_timeout_seconds:
        lock_reasons.add(LockReason.HEARTBEAT_TIMEOUT)
    else:
        unlock_reasons.add(LockReason.HEARTBEAT_TIMEOUT)

    return PolicyEvaluation(
        lock_reasons=lock_reasons,
        unlock_reasons=unlock_reasons,
        eye_break_due=eye_break_due,
        eye_break_expired=eye_break_expired,
        daily_limit_reached=daily_limit_reached,
        session_limit_reached=session_limit_reached,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_bedtime(
    current: datetime.time,
    start: datetime.time,
    end: datetime.time,
) -> bool:
    """Check whether *current* falls inside the bedtime window.

    Handles midnight-crossing windows (e.g. 21:00 -- 07:00).
    """
    if start > end:
        # Crosses midnight: bedtime if current >= start OR current < end.
        return current >= start or current < end
    else:
        return start <= current < end
