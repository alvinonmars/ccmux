"""Unit tests for ccmux.hooks_manager — idempotent hook installation."""
import json
from pathlib import Path

import pytest

from ccmux.config import Config, load
from ccmux.hooks_manager import install, _is_ccmux_wrapper, HOOK_EVENTS


@pytest.fixture
def cfg(tmp_path) -> Config:
    return load(tmp_path)


@pytest.fixture
def settings_path(tmp_path) -> Path:
    return tmp_path / "settings.json"


def test_install_writes_all_events(cfg, settings_path):
    install(cfg, settings_path)
    data = json.loads(settings_path.read_text())
    hooks = data["hooks"]
    for event in HOOK_EVENTS:
        assert event in hooks, f"Missing hook event: {event}"


def test_install_hook_command_is_absolute_path(cfg, settings_path):
    install(cfg, settings_path)
    data = json.loads(settings_path.read_text())
    for event in HOOK_EVENTS:
        wrappers = data["hooks"][event]
        assert len(wrappers) == 1
        cmd = wrappers[0]["hooks"][0]["command"]
        assert Path(cmd).is_absolute()
        assert cmd.endswith("hook.py")


def test_install_is_idempotent(cfg, settings_path):
    """Installing twice results in exactly one hook entry per event."""
    install(cfg, settings_path)
    install(cfg, settings_path)
    data = json.loads(settings_path.read_text())
    for event in HOOK_EVENTS:
        assert len(data["hooks"][event]) == 1


def test_install_preserves_existing_fields(cfg, settings_path):
    """Existing settings.json fields are not overwritten."""
    settings_path.write_text(json.dumps({"apiKey": "secret", "theme": "dark"}))
    install(cfg, settings_path)
    data = json.loads(settings_path.read_text())
    assert data["apiKey"] == "secret"
    assert data["theme"] == "dark"


def test_install_preserves_other_hooks(cfg, settings_path):
    """Non-ccmux hooks in an event list are preserved."""
    existing = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "/other/hook.sh"}]}]
        }
    }
    settings_path.write_text(json.dumps(existing))
    install(cfg, settings_path)
    data = json.loads(settings_path.read_text())
    stop_hooks = data["hooks"]["Stop"]
    commands = [w["hooks"][0]["command"] for w in stop_hooks]
    assert "/other/hook.sh" in commands
    assert any("hook.py" in c for c in commands)


def test_is_ccmux_wrapper_identifies_correctly(cfg):
    command = str(cfg.hook_script.resolve())
    wrapper = {"hooks": [{"type": "command", "command": command}]}
    assert _is_ccmux_wrapper(wrapper, command)
    assert not _is_ccmux_wrapper({"hooks": [{"type": "command", "command": "/other"}]}, command)
    assert not _is_ccmux_wrapper("not_a_dict", command)
