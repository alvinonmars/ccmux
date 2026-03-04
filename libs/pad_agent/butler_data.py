"""Read butler data (schedule, homework, weather) for KidPad dashboard.

Pure functions that read local files only — no network calls.
Called by monitor each cycle to enrich state.json with butler fields.
Dashboard JS ignores missing fields (backward compatible).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Day-of-week names used in family_context.jsonl activity entries
_DOW_NAMES = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday",
}


def get_butler_state(
    child_name: str,
    data_dir: Path,
    now: datetime,
) -> dict:
    """Read family context + homework, return butler fields for state.json.

    Returns dict with keys: schedule, homework, reminder, weather, date_display.
    All fields optional — dashboard handles missing gracefully.

    Args:
        child_name: Child's name (e.g. "Alice").
        data_dir: Root data directory (~/.ccmux/data/).
        now: Current time with timezone.
    """
    result: dict = {}

    schedule = _read_schedule(data_dir, child_name, now)
    if schedule:
        result["schedule"] = schedule

    homework = _read_homework(data_dir, child_name, now)
    if homework:
        result["homework"] = homework

    if schedule:
        reminder = _compute_reminder(schedule, now)
        if reminder:
            result["reminder"] = reminder

    weather = _read_weather(data_dir)
    if weather:
        result["weather"] = weather

    result["date_display"] = now.strftime("%A, %b %-d")

    return result


def _read_schedule(
    data_dir: Path, child_name: str, now: datetime
) -> list[dict]:
    """Read today's schedule from family_context.jsonl.

    Looks for entries with category=activity that match today's day-of-week.
    """
    context_file = data_dir / "household" / "family_context.jsonl"
    if not context_file.exists():
        return []

    dow = _DOW_NAMES[now.weekday()]
    activities: list[dict] = []

    try:
        with open(context_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("category") != "activity":
                    continue

                value = entry.get("value", {})
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        continue

                if not isinstance(value, dict):
                    continue

                # Match child name (case-insensitive)
                entry_child = value.get("child", "")
                if entry_child.lower() != child_name.lower():
                    continue

                # Match day-of-week
                days = value.get("days", [])
                if isinstance(days, str):
                    days = [days]
                days_lower = [d.lower() for d in days]
                if dow not in days_lower:
                    continue

                activity = {
                    "time": value.get("time", ""),
                    "name": value.get("name", entry.get("key", "")),
                    "icon": value.get("icon", _default_icon(value)),
                    "type": value.get("type", "in_person"),
                }
                activities.append(activity)
    except OSError:
        log.warning("failed to read family context: %s", context_file)
        return []

    # Sort by time
    activities.sort(key=lambda a: a.get("time", ""))
    return activities


def _default_icon(value: dict) -> str:
    """Pick a default icon based on activity type."""
    activity_type = value.get("type", "")
    if activity_type == "online":
        return "\U0001F4BB"  # laptop
    return "\U0001F4C5"  # calendar


def _read_homework(
    data_dir: Path, child_name: str, now: datetime
) -> list[dict]:
    """Read homework assignments due today or tomorrow.

    Scans homework/isf/<child>/YYYY-MM/*_assignments.json for due items.
    """
    child_lower = child_name.lower()
    hw_dir = data_dir / "household" / "homework" / "isf" / child_lower
    if not hw_dir.exists():
        return []

    # Check current month and previous month directories
    month_str = now.strftime("%Y-%m")
    prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    assignments: list[dict] = []

    for month_dir_name in [month_str, prev_month]:
        month_dir = hw_dir / month_dir_name
        if not month_dir.exists():
            continue

        try:
            for f in sorted(month_dir.glob("*_assignments.json"), reverse=True):
                try:
                    data = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError):
                    continue

                items = data if isinstance(data, list) else data.get("assignments", [])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    due = item.get("due", "")
                    if due == today_str:
                        due_label = "today"
                    elif due == tomorrow_str:
                        due_label = "tomorrow"
                    else:
                        continue

                    assignments.append({
                        "subject": item.get("subject", ""),
                        "due": due_label,
                        "icon": item.get("icon", _homework_icon(item.get("subject", ""))),
                    })

                # Only read the latest assignments file per month
                if assignments:
                    break
        except OSError:
            continue

        # Stop scanning older months once we have results
        if assignments:
            break

    return assignments


def _homework_icon(subject: str) -> str:
    """Pick a default icon for a homework subject."""
    s = subject.lower()
    if "math" in s:
        return "\U0001F522"  # numbers
    if "chinese" in s or "mandarin" in s:
        return "\U0001F4D6"  # book
    if "english" in s or "reading" in s:
        return "\U0001F4DA"  # books
    if "science" in s:
        return "\U0001F52C"  # microscope
    if "art" in s or "draw" in s:
        return "\U0001F3A8"  # palette
    if "music" in s:
        return "\U0001F3B5"  # music note
    return "\U0001F4DD"  # memo


def _compute_reminder(schedule: list[dict], now: datetime) -> dict | None:
    """Generate a reminder if a class starts within 20 minutes."""
    now_minutes = now.hour * 60 + now.minute

    for activity in schedule:
        time_str = activity.get("time", "")
        if not time_str or ":" not in time_str:
            continue

        try:
            parts = time_str.split(":")
            act_minutes = int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            continue

        diff = act_minutes - now_minutes
        if 0 < diff <= 20:
            return {
                "icon": activity.get("icon", "\U0001F514"),  # bell
                "text": f"{activity['name']} in {diff} min!",
                "type": "class_soon",
            }

    return None


def _read_weather(data_dir: Path) -> dict | None:
    """Read cached weather data from butler's weather cache."""
    cache_file = data_dir / "household" / "butler" / "weather_cache.json"
    if not cache_file.exists():
        return None

    try:
        data = json.loads(cache_file.read_text())
        if not isinstance(data, dict):
            return None
        # Expect at minimum: temp, icon, text
        if "temp" not in data:
            return None
        return {
            "temp": data["temp"],
            "icon": data.get("icon", "\u2600"),  # sun
            "text": data.get("text", ""),
        }
    except (json.JSONDecodeError, OSError):
        return None


def get_tomorrow_schedule(
    data_dir: Path, child_name: str, now: datetime
) -> list[dict]:
    """Read tomorrow's schedule for lock screen display.

    Same logic as _read_schedule but for tomorrow's day-of-week.
    """
    tomorrow = now + timedelta(days=1)
    return _read_schedule(data_dir, child_name, tomorrow)
