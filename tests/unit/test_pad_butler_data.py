"""Tests for KidPad butler data parsing (schedule, homework, weather)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from libs.pad_agent.butler_data import (
    get_butler_state,
    get_tomorrow_schedule,
    _compute_reminder,
    _homework_icon,
    _read_homework,
    _read_schedule,
    _read_weather,
)

HKT = ZoneInfo("Asia/Hong_Kong")

# Wednesday 2026-03-04 15:00
WEDNESDAY_3PM = datetime(2026, 3, 4, 15, 0, 0, tzinfo=HKT)


def _write_family_context(data_dir: Path, entries: list[dict]) -> None:
    """Write entries to family_context.jsonl."""
    ctx_file = data_dir / "household" / "family_context.jsonl"
    ctx_file.parent.mkdir(parents=True, exist_ok=True)
    with open(ctx_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _write_homework(data_dir: Path, child: str, month: str, assignments: list[dict]) -> None:
    """Write assignments file."""
    hw_dir = data_dir / "household" / "homework" / "isf" / child.lower() / month
    hw_dir.mkdir(parents=True, exist_ok=True)
    path = hw_dir / "20260304_assignments.json"
    path.write_text(json.dumps({"assignments": assignments}))


def _write_weather(data_dir: Path, weather: dict) -> None:
    """Write weather cache."""
    cache_dir = data_dir / "household" / "butler"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "weather_cache.json").write_text(json.dumps(weather))


# -- Schedule tests ----------------------------------------------------------


class TestReadSchedule:
    """Tests for schedule parsing from family_context.jsonl."""

    def test_matches_day_of_week(self, tmp_path: Path) -> None:
        """Activities matching today's day-of-week are returned."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "lingo_ace",
                "value": {
                    "child": "TestChild",
                    "name": "Lingo Ace",
                    "time": "14:00",
                    "days": ["wednesday"],
                    "type": "online",
                    "icon": "\U0001F4BB",
                },
            },
        ])

        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert len(result) == 1
        assert result[0]["name"] == "Lingo Ace"
        assert result[0]["time"] == "14:00"
        assert result[0]["type"] == "online"

    def test_filters_other_days(self, tmp_path: Path) -> None:
        """Activities on other days are excluded."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "piano",
                "value": {
                    "child": "TestChild",
                    "name": "Piano",
                    "time": "16:00",
                    "days": ["monday"],
                    "type": "in_person",
                },
            },
        ])

        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert result == []

    def test_filters_other_children(self, tmp_path: Path) -> None:
        """Activities for other children are excluded."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "swimming",
                "value": {
                    "child": "OtherChild",
                    "name": "Swimming",
                    "time": "10:00",
                    "days": ["wednesday"],
                    "type": "in_person",
                },
            },
        ])

        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert result == []

    def test_case_insensitive_child_match(self, tmp_path: Path) -> None:
        """Child name matching is case-insensitive."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "ballet",
                "value": {
                    "child": "TESTCHILD",
                    "name": "Ballet",
                    "time": "15:00",
                    "days": ["Wednesday"],
                    "type": "in_person",
                },
            },
        ])

        result = _read_schedule(tmp_path, "testchild", WEDNESDAY_3PM)
        assert len(result) == 1

    def test_multiple_activities_sorted_by_time(self, tmp_path: Path) -> None:
        """Multiple activities are returned sorted by time."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "basketball",
                "value": {
                    "child": "TestChild",
                    "name": "Basketball",
                    "time": "17:00",
                    "days": ["wednesday"],
                    "icon": "\U0001F3C0",
                },
            },
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "lingo_ace",
                "value": {
                    "child": "TestChild",
                    "name": "Lingo Ace",
                    "time": "14:00",
                    "days": ["wednesday"],
                    "type": "online",
                },
            },
        ])

        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert len(result) == 2
        assert result[0]["name"] == "Lingo Ace"
        assert result[1]["name"] == "Basketball"

    def test_missing_context_file(self, tmp_path: Path) -> None:
        """Returns empty list when family_context.jsonl doesn't exist."""
        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert result == []

    def test_skips_non_activity_entries(self, tmp_path: Path) -> None:
        """Non-activity entries in family_context.jsonl are ignored."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "routine",
                "key": "bedtime",
                "value": {"time": "21:00"},
            },
        ])

        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert result == []

    def test_string_days_field(self, tmp_path: Path) -> None:
        """days field as a single string (not list) is handled."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "art",
                "value": {
                    "child": "TestChild",
                    "name": "Art Class",
                    "time": "10:00",
                    "days": "wednesday",
                },
            },
        ])

        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert len(result) == 1

    def test_corrupted_jsonl_lines_skipped(self, tmp_path: Path) -> None:
        """Corrupted JSONL lines are skipped without crashing."""
        ctx_file = tmp_path / "household" / "family_context.jsonl"
        ctx_file.parent.mkdir(parents=True, exist_ok=True)
        valid_entry = json.dumps({
            "ts": "2026-03-01T10:00:00+08:00",
            "category": "activity",
            "key": "art",
            "value": {
                "child": "TestChild",
                "name": "Art",
                "time": "10:00",
                "days": ["wednesday"],
            },
        })
        with open(ctx_file, "w") as f:
            f.write("not valid json\n")
            f.write("\n")  # empty line
            f.write(valid_entry + "\n")
            f.write("{broken\n")

        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert len(result) == 1
        assert result[0]["name"] == "Art"

    def test_default_icon_for_online(self, tmp_path: Path) -> None:
        """Online activity gets laptop icon by default."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "coding",
                "value": {
                    "child": "TestChild",
                    "name": "Coding",
                    "time": "14:00",
                    "days": ["wednesday"],
                    "type": "online",
                },
            },
        ])

        result = _read_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert result[0]["icon"] == "\U0001F4BB"


# -- Homework tests ----------------------------------------------------------


class TestReadHomework:
    """Tests for homework assignment parsing."""

    def test_homework_due_today(self, tmp_path: Path) -> None:
        """Assignments due today are returned with 'today' label."""
        _write_homework(tmp_path, "TestChild", "2026-03", [
            {"subject": "Chinese", "due": "2026-03-04", "icon": "\U0001F4D6"},
        ])

        result = _read_homework(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert len(result) == 1
        assert result[0]["subject"] == "Chinese"
        assert result[0]["due"] == "today"

    def test_homework_due_tomorrow(self, tmp_path: Path) -> None:
        """Assignments due tomorrow are returned with 'tomorrow' label."""
        _write_homework(tmp_path, "TestChild", "2026-03", [
            {"subject": "Math", "due": "2026-03-05"},
        ])

        result = _read_homework(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert len(result) == 1
        assert result[0]["due"] == "tomorrow"

    def test_homework_due_later_excluded(self, tmp_path: Path) -> None:
        """Assignments due after tomorrow are excluded."""
        _write_homework(tmp_path, "TestChild", "2026-03", [
            {"subject": "Science", "due": "2026-03-10"},
        ])

        result = _read_homework(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert result == []

    def test_homework_default_icon(self, tmp_path: Path) -> None:
        """Default icons assigned based on subject name."""
        assert _homework_icon("Math worksheet") == "\U0001F522"
        assert _homework_icon("Chinese reading") == "\U0001F4D6"
        assert _homework_icon("English essay") == "\U0001F4DA"
        assert _homework_icon("Unknown subject") == "\U0001F4DD"

    def test_no_homework_dir(self, tmp_path: Path) -> None:
        """Returns empty list when homework directory doesn't exist."""
        result = _read_homework(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert result == []

    def test_assignments_as_list(self, tmp_path: Path) -> None:
        """Supports assignments file as a bare list (not wrapped in object)."""
        child_lower = "testchild"
        hw_dir = tmp_path / "household" / "homework" / "isf" / child_lower / "2026-03"
        hw_dir.mkdir(parents=True, exist_ok=True)
        (hw_dir / "20260304_assignments.json").write_text(json.dumps([
            {"subject": "Art", "due": "2026-03-04"},
        ]))

        result = _read_homework(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert len(result) == 1
        assert result[0]["subject"] == "Art"

    def test_no_duplicates_across_months(self, tmp_path: Path) -> None:
        """Current month results prevent scanning previous month (no duplicates)."""
        # Write same assignment in both current and previous month dirs
        _write_homework(tmp_path, "TestChild", "2026-03", [
            {"subject": "Chinese", "due": "2026-03-04"},
        ])
        # Also write in previous month dir
        child_lower = "testchild"
        prev_dir = tmp_path / "household" / "homework" / "isf" / child_lower / "2026-02"
        prev_dir.mkdir(parents=True, exist_ok=True)
        (prev_dir / "20260228_assignments.json").write_text(json.dumps({
            "assignments": [{"subject": "Math", "due": "2026-03-04"}],
        }))

        result = _read_homework(tmp_path, "TestChild", WEDNESDAY_3PM)
        # Should only have Chinese from current month, not Math from prev month
        assert len(result) == 1
        assert result[0]["subject"] == "Chinese"


# -- Weather tests -----------------------------------------------------------


class TestReadWeather:
    """Tests for weather cache reading."""

    def test_reads_weather_cache(self, tmp_path: Path) -> None:
        """Weather data is read from cache file."""
        _write_weather(tmp_path, {"temp": 22, "icon": "\u26C5", "text": "Partly cloudy"})

        result = _read_weather(tmp_path)
        assert result is not None
        assert result["temp"] == 22
        assert result["icon"] == "\u26C5"
        assert result["text"] == "Partly cloudy"

    def test_missing_weather_cache(self, tmp_path: Path) -> None:
        """Returns None when weather cache doesn't exist."""
        result = _read_weather(tmp_path)
        assert result is None

    def test_invalid_weather_json(self, tmp_path: Path) -> None:
        """Returns None when weather cache contains invalid JSON."""
        cache_dir = tmp_path / "household" / "butler"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "weather_cache.json").write_text("not json")

        result = _read_weather(tmp_path)
        assert result is None

    def test_weather_missing_temp(self, tmp_path: Path) -> None:
        """Returns None when weather cache has no temp field."""
        _write_weather(tmp_path, {"icon": "\u2600", "text": "Sunny"})

        result = _read_weather(tmp_path)
        assert result is None


# -- Reminder tests ----------------------------------------------------------


class TestComputeReminder:
    """Tests for class reminder computation."""

    def test_reminder_when_class_in_15_min(self) -> None:
        """Reminder generated when class starts in <=20 min."""
        now = datetime(2026, 3, 4, 13, 45, 0, tzinfo=HKT)
        schedule = [{"time": "14:00", "name": "Lingo Ace", "icon": "\U0001F4BB"}]

        result = _compute_reminder(schedule, now)
        assert result is not None
        assert result["type"] == "class_soon"
        assert "Lingo Ace" in result["text"]
        assert "15 min" in result["text"]

    def test_no_reminder_when_class_far(self) -> None:
        """No reminder when next class is more than 20 min away."""
        now = datetime(2026, 3, 4, 13, 0, 0, tzinfo=HKT)
        schedule = [{"time": "14:00", "name": "Lingo Ace", "icon": "\U0001F4BB"}]

        result = _compute_reminder(schedule, now)
        assert result is None

    def test_no_reminder_when_class_passed(self) -> None:
        """No reminder when class time has already passed."""
        now = datetime(2026, 3, 4, 14, 30, 0, tzinfo=HKT)
        schedule = [{"time": "14:00", "name": "Lingo Ace", "icon": "\U0001F4BB"}]

        result = _compute_reminder(schedule, now)
        assert result is None

    def test_reminder_picks_nearest_class(self) -> None:
        """Reminder picks the nearest upcoming class within 20 min."""
        now = datetime(2026, 3, 4, 16, 45, 0, tzinfo=HKT)
        schedule = [
            {"time": "14:00", "name": "Lingo Ace", "icon": "\U0001F4BB"},
            {"time": "17:00", "name": "Basketball", "icon": "\U0001F3C0"},
        ]

        result = _compute_reminder(schedule, now)
        assert result is not None
        assert "Basketball" in result["text"]

    def test_empty_schedule_no_reminder(self) -> None:
        """No reminder when schedule is empty."""
        result = _compute_reminder([], WEDNESDAY_3PM)
        assert result is None


# -- Integration: get_butler_state -------------------------------------------


class TestGetButlerState:
    """Integration tests for get_butler_state."""

    def test_full_state_with_all_data(self, tmp_path: Path) -> None:
        """Full state returned when all data sources available."""
        # Schedule
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "lingo_ace",
                "value": {
                    "child": "TestChild",
                    "name": "Lingo Ace",
                    "time": "14:00",
                    "days": ["wednesday"],
                    "type": "online",
                    "icon": "\U0001F4BB",
                },
            },
        ])
        # Homework
        _write_homework(tmp_path, "TestChild", "2026-03", [
            {"subject": "Chinese", "due": "2026-03-04"},
        ])
        # Weather
        _write_weather(tmp_path, {"temp": 22, "icon": "\u26C5", "text": "Partly cloudy"})

        result = get_butler_state("TestChild", tmp_path, WEDNESDAY_3PM)

        assert "schedule" in result
        assert "homework" in result
        assert "weather" in result
        assert "date_display" in result
        assert result["date_display"] == "Wednesday, Mar 4"

    def test_empty_state_no_data(self, tmp_path: Path) -> None:
        """Only date_display returned when no data sources exist."""
        result = get_butler_state("TestChild", tmp_path, WEDNESDAY_3PM)

        assert "date_display" in result
        assert "schedule" not in result
        assert "homework" not in result
        assert "weather" not in result
        assert "reminder" not in result

    def test_reminder_included_when_class_soon(self, tmp_path: Path) -> None:
        """Reminder included when a class starts within 20 minutes."""
        now = datetime(2026, 3, 4, 13, 45, 0, tzinfo=HKT)
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "lingo_ace",
                "value": {
                    "child": "TestChild",
                    "name": "Lingo Ace",
                    "time": "14:00",
                    "days": ["wednesday"],
                    "icon": "\U0001F4BB",
                },
            },
        ])

        result = get_butler_state("TestChild", tmp_path, now)
        assert "reminder" in result
        assert result["reminder"]["type"] == "class_soon"

    def test_no_reminder_when_no_schedule(self, tmp_path: Path) -> None:
        """No reminder field when no schedule exists."""
        result = get_butler_state("TestChild", tmp_path, WEDNESDAY_3PM)
        assert "reminder" not in result


# -- Tomorrow schedule (lock screen) -----------------------------------------


class TestTomorrowSchedule:
    """Tests for get_tomorrow_schedule (lock screen feature)."""

    def test_tomorrow_schedule(self, tmp_path: Path) -> None:
        """Tomorrow's schedule is read using tomorrow's day-of-week."""
        # Wednesday -> Thursday
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "wukong",
                "value": {
                    "child": "TestChild",
                    "name": "Wukong",
                    "time": "13:20",
                    "days": ["thursday"],
                    "icon": "\U0001F412",
                },
            },
        ])

        result = get_tomorrow_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert len(result) == 1
        assert result[0]["name"] == "Wukong"

    def test_tomorrow_no_activities(self, tmp_path: Path) -> None:
        """Empty list when no activities tomorrow."""
        _write_family_context(tmp_path, [
            {
                "ts": "2026-03-01T10:00:00+08:00",
                "category": "activity",
                "key": "ballet",
                "value": {
                    "child": "TestChild",
                    "name": "Ballet",
                    "time": "15:00",
                    "days": ["monday"],
                },
            },
        ])

        result = get_tomorrow_schedule(tmp_path, "TestChild", WEDNESDAY_3PM)
        assert result == []
