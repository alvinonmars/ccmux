"""Install ccmux hooks in project-level .claude/settings.json.

Project-level hooks only fire for Claude instances running in this project
directory, avoiding interference with independent Claude instances on the
same machine.

The hook entry is written idempotently: existing fields are preserved,
and the hook command appears exactly once per event.
"""
from __future__ import annotations

import json
from pathlib import Path

from ccmux.config import Config

HOOK_EVENTS = [
    "SessionStart",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
    "PermissionRequest",
]


def _read_settings(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def install(config: Config, settings_path: Path | None = None) -> None:
    """Write ccmux hook entries into project-level .claude/settings.json (idempotent)."""
    if settings_path is None:
        settings_path = config.project_root / ".claude" / "settings.json"
    command = str(config.hook_script.resolve())
    hook_entry = {"type": "command", "command": command}
    wrapper = {"hooks": [hook_entry]}

    settings = _read_settings(settings_path)
    hooks_section: dict = settings.setdefault("hooks", {})

    for event in HOOK_EVENTS:
        event_list: list = hooks_section.setdefault(event, [])
        # Remove stale ccmux entries (same command, possibly outdated wrapper)
        event_list[:] = [
            w for w in event_list
            if not _is_ccmux_wrapper(w, command)
        ]
        event_list.append(wrapper)

    _write_settings(settings_path, settings)


def _is_ccmux_wrapper(wrapper: dict, command: str) -> bool:
    """Return True if wrapper is a ccmux hook entry for the given command."""
    if not isinstance(wrapper, dict):
        return False
    hooks = wrapper.get("hooks", [])
    return any(
        isinstance(h, dict) and h.get("command") == command
        for h in hooks
    )
