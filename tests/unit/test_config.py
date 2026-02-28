"""Unit tests for ccmux.config."""
import os
import tomllib
from pathlib import Path

import pytest

from ccmux.config import Config, load


def test_load_defaults(tmp_path):
    """load() with no ccmux.toml returns all defaults."""
    cfg = load(tmp_path)
    assert cfg.runtime_dir == Path("/tmp/ccmux")
    assert cfg.idle_threshold == 30
    assert cfg.silence_timeout == 3
    assert cfg.backoff_initial == 1
    assert cfg.backoff_cap == 60
    assert cfg.project_root == tmp_path.resolve()


def test_load_from_toml(tmp_path):
    """load() reads all fields from ccmux.toml."""
    (tmp_path / "ccmux.toml").write_text(
        "[project]\nname = \"myproject\"\n"
        "[runtime]\ndir = \"/var/run/ccmux\"\n"
        "[timing]\nidle_threshold = 10\nsilence_timeout = 2\n"
        "[recovery]\nbackoff_initial = 2\nbackoff_cap = 30\n"
    )
    cfg = load(tmp_path)
    assert cfg.project_name == "myproject"
    assert cfg.runtime_dir == Path("/var/run/ccmux")
    assert cfg.idle_threshold == 10
    assert cfg.silence_timeout == 2
    assert cfg.backoff_initial == 2
    assert cfg.backoff_cap == 30


def test_project_name_defaults_to_cwd_basename(tmp_path):
    """When [project] name is absent, project_name is the CWD basename."""
    cfg = load(tmp_path)
    assert cfg.project_name == tmp_path.name


def test_tmux_session_format(tmp_path):
    (tmp_path / "ccmux.toml").write_text("[project]\nname = \"hub\"\n")
    cfg = load(tmp_path)
    assert cfg.tmux_session == "ccmux-hub"


def test_derived_paths(tmp_path):
    """control_sock, output_sock, hook_script are derived correctly."""
    cfg = load(tmp_path)
    assert cfg.control_sock == cfg.runtime_dir / "control.sock"
    assert cfg.output_sock == cfg.runtime_dir / "output.sock"
    assert cfg.hook_script == tmp_path.resolve() / "ccmux" / "hook.py"


def test_partial_toml(tmp_path):
    """Only some sections present; others fall back to defaults."""
    (tmp_path / "ccmux.toml").write_text("[timing]\nsilence_timeout = 5\n")
    cfg = load(tmp_path)
    assert cfg.silence_timeout == 5
    assert cfg.idle_threshold == 30  # default
