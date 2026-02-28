"""Persistent pending task tracker for cross-session continuity.

Tasks that require external confirmation or follow-up are tracked here
so they survive session restarts and reboots.

Storage: ~/.ccmux/data/pending_tasks.jsonl
Lifecycle: pending -> notified -> follow_up -> confirmed -> closed

Usage from Claude session:
    from ccmux.pending_tasks import PendingTaskTracker
    tracker = PendingTaskTracker()
    tracker.add("reply-teacher", "Reply to Ms. Wong after wife confirms",
                follow_up_hours=24)
    tracker.list_open()  # returns all non-closed tasks
    tracker.update("reply-teacher", status="confirmed", note="Wife approved")
    tracker.close("reply-teacher", note="Email sent")
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from ccmux.paths import DATA_ROOT

TASKS_FILE = DATA_ROOT / "pending_tasks.jsonl"

Status = Literal["pending", "notified", "follow_up", "confirmed", "closed"]


@dataclass
class PendingTask:
    task_id: str
    description: str
    status: Status = "pending"
    created_at: str = ""
    updated_at: str = ""
    follow_up_hours: float = 0
    note: str = ""
    source: str = ""  # who/what created this task

    def __post_init__(self) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


class PendingTaskTracker:
    """JSONL-backed persistent task tracker."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or TASKS_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        task_id: str,
        description: str,
        follow_up_hours: float = 0,
        source: str = "",
    ) -> PendingTask:
        """Create a new pending task. Overwrites if task_id already exists."""
        task = PendingTask(
            task_id=task_id,
            description=description,
            follow_up_hours=follow_up_hours,
            source=source,
        )
        tasks = self._load_all()
        tasks = [t for t in tasks if t.task_id != task_id]
        tasks.append(task)
        self._save_all(tasks)
        return task

    def update(
        self,
        task_id: str,
        status: Status | None = None,
        note: str | None = None,
    ) -> PendingTask | None:
        """Update a task's status and/or note. Returns None if not found."""
        tasks = self._load_all()
        for task in tasks:
            if task.task_id == task_id:
                if status:
                    task.status = status
                if note is not None:
                    task.note = note
                task.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                self._save_all(tasks)
                return task
        return None

    def close(self, task_id: str, note: str = "") -> PendingTask | None:
        """Close a task."""
        return self.update(task_id, status="closed", note=note)

    def list_open(self) -> list[PendingTask]:
        """Return all non-closed tasks, oldest first."""
        return [t for t in self._load_all() if t.status != "closed"]

    def list_all(self) -> list[PendingTask]:
        """Return all tasks including closed."""
        return self._load_all()

    def get(self, task_id: str) -> PendingTask | None:
        """Get a specific task by ID."""
        for t in self._load_all():
            if t.task_id == task_id:
                return t
        return None

    def overdue(self) -> list[PendingTask]:
        """Return open tasks past their follow-up window."""
        now = time.time()
        result = []
        for task in self.list_open():
            if task.follow_up_hours <= 0:
                continue
            try:
                created = time.mktime(
                    time.strptime(task.created_at[:19], "%Y-%m-%dT%H:%M:%S")
                )
                if (now - created) > task.follow_up_hours * 3600:
                    result.append(task)
            except (ValueError, OverflowError):
                continue
        return result

    def _load_all(self) -> list[PendingTask]:
        """Load all tasks from JSONL file."""
        if not self._path.exists():
            return []
        tasks = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                tasks.append(PendingTask(**data))
            except (json.JSONDecodeError, TypeError):
                continue
        return tasks

    def _save_all(self, tasks: list[PendingTask]) -> None:
        """Write all tasks to JSONL file (full rewrite)."""
        lines = [json.dumps(asdict(t)) for t in tasks]
        self._path.write_text("\n".join(lines) + "\n" if lines else "")
