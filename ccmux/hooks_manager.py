"""Install and remove ccmux hooks in ~/.claude/settings.json.

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

_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


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


def install(config: Config, settings_path: Path = _SETTINGS_PATH) -> None:
    """Write ccmux hook entries into settings.json (idempotent)."""
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


def remove(config: Config, settings_path: Path = _SETTINGS_PATH) -> None:
    """Remove ccmux hook entries from settings.json."""
    command = str(config.hook_script.resolve())
    settings = _read_settings(settings_path)
    hooks_section: dict = settings.get("hooks", {})

    for event in HOOK_EVENTS:
        if event in hooks_section:
            hooks_section[event] = [
                w for w in hooks_section[event]
                if not _is_ccmux_wrapper(w, command)
            ]
            if not hooks_section[event]:
                del hooks_section[event]

    if not hooks_section:
        settings.pop("hooks", None)

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
