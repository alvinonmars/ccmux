"""Unit tests for ccmux.pending_tasks."""
from pathlib import Path

from ccmux.pending_tasks import PendingTaskTracker


def test_add_and_list(tmp_path):
    tracker = PendingTaskTracker(tmp_path / "tasks.jsonl")
    tracker.add("task-1", "Reply to teacher")
    tracker.add("task-2", "Confirm delivery", follow_up_hours=24)

    tasks = tracker.list_open()
    assert len(tasks) == 2
    assert tasks[0].task_id == "task-1"
    assert tasks[1].follow_up_hours == 24


def test_update_and_close(tmp_path):
    tracker = PendingTaskTracker(tmp_path / "tasks.jsonl")
    tracker.add("task-1", "Reply to teacher")

    tracker.update("task-1", status="notified")
    task = tracker.get("task-1")
    assert task.status == "notified"

    tracker.close("task-1", note="Done")
    assert len(tracker.list_open()) == 0
    assert tracker.get("task-1").status == "closed"


def test_overwrite_same_id(tmp_path):
    tracker = PendingTaskTracker(tmp_path / "tasks.jsonl")
    tracker.add("task-1", "Version 1")
    tracker.add("task-1", "Version 2")

    tasks = tracker.list_all()
    assert len(tasks) == 1
    assert tasks[0].description == "Version 2"


def test_persistence(tmp_path):
    path = tmp_path / "tasks.jsonl"
    tracker1 = PendingTaskTracker(path)
    tracker1.add("task-1", "Survives reload")

    tracker2 = PendingTaskTracker(path)
    tasks = tracker2.list_open()
    assert len(tasks) == 1
    assert tasks[0].description == "Survives reload"


def test_empty_file(tmp_path):
    tracker = PendingTaskTracker(tmp_path / "tasks.jsonl")
    assert tracker.list_open() == []
    assert tracker.get("nonexistent") is None
