"""Shared fixtures for Zulip adapter integration tests.

Provides:
  mock_zulip          — Start/stop MockZulipServer, yield instance
  zulip_config        — ZulipAdapterConfig pointing at mock server
  zulip_adapter       — ZulipAdapter wired to mock server (not running event loop)
  mock_claude_env     — Env vars for mock_claude.py
  patch_claude_cmd    — Intercept tmux send-keys to swap claude → mock_claude
  cleanup_tmux        — Autouse: kill all test-created tmux sessions on teardown
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

from adapters.zulip_adapter.config import StreamConfig, ZulipAdapterConfig
from adapters.zulip_adapter.adapter import ZulipAdapter
from tests.helpers.mock_zulip_server import MockZulipServer

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MOCK_CLAUDE_SCRIPT = REPO_ROOT / "tests" / "helpers" / "mock_claude.py"
RELAY_HOOK_SCRIPT = REPO_ROOT / "scripts" / "zulip_relay_hook.py"


# ---------------------------------------------------------------------------
# pytest markers
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "zulip_integration: Zulip integration test (requires tmux)",
    )


# ---------------------------------------------------------------------------
# Mock Zulip server
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_zulip() -> Generator[MockZulipServer, None, None]:
    """Start a mock Zulip HTTP server on a random port."""
    server = MockZulipServer()
    server.start()
    yield server
    server.stop()


# ---------------------------------------------------------------------------
# Config and adapter fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def zulip_credentials(tmp_path: Path) -> Path:
    """Create a temporary Zulip bot credentials file."""
    creds = tmp_path / "zulip_bot.env"
    creds.write_text('ZULIP_BOT_API_KEY=test-api-key-12345\n')
    return creds


@pytest.fixture
def zulip_env_template(tmp_path: Path) -> Path:
    """Create a minimal env_template.sh for tests."""
    template = tmp_path / "env_template.sh"
    template.write_text("")  # Empty — per-instance vars set by process_mgr
    return template


@pytest.fixture
def zulip_streams_dir(tmp_path: Path) -> Path:
    """Create a streams directory with a test stream."""
    streams = tmp_path / "streams"
    streams.mkdir()
    return streams


@pytest.fixture
def zulip_project_dir(tmp_path: Path) -> Path:
    """Create a fake project directory for test instances."""
    project = tmp_path / "test-project"
    project.mkdir()
    # Create .claude dir so ccmux-init doesn't fail
    (project / ".claude").mkdir()
    return project


@pytest.fixture
def zulip_config(
    mock_zulip: MockZulipServer,
    tmp_path: Path,
    zulip_credentials: Path,
    zulip_env_template: Path,
    zulip_streams_dir: Path,
    zulip_project_dir: Path,
) -> ZulipAdapterConfig:
    """ZulipAdapterConfig wired to the mock server."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    # Create a test stream config
    stream_dir = zulip_streams_dir / "test-stream"
    stream_dir.mkdir()
    (stream_dir / "stream.toml").write_text(
        f'project_path = "{zulip_project_dir}"\nchannel = "zulip"\n'
    )

    cfg = ZulipAdapterConfig(
        site=mock_zulip.base_url,
        bot_email="bot@test.example.com",
        bot_credentials=zulip_credentials,
        streams_dir=zulip_streams_dir,
        env_template=zulip_env_template,
        runtime_dir=runtime,
    )
    # Pre-load stream config
    cfg.streams["test-stream"] = StreamConfig(
        name="test-stream",
        project_path=zulip_project_dir,
    )
    return cfg


@pytest.fixture
def zulip_adapter(zulip_config: ZulipAdapterConfig) -> Generator[ZulipAdapter, None, None]:
    """ZulipAdapter wired to mock server. Not running event loop."""
    adapter = ZulipAdapter(zulip_config)
    yield adapter
    adapter.stop()


# ---------------------------------------------------------------------------
# Complete test environment
# ---------------------------------------------------------------------------

@pytest.fixture
def zulip_test_env(
    mock_zulip: MockZulipServer,
    zulip_config: ZulipAdapterConfig,
    zulip_adapter: ZulipAdapter,
    zulip_project_dir: Path,
    zulip_credentials: Path,
    tmp_path: Path,
) -> dict:
    """Complete test environment dict with all components."""
    return {
        "server": mock_zulip,
        "config": zulip_config,
        "adapter": zulip_adapter,
        "project_dir": zulip_project_dir,
        "credentials": zulip_credentials,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# Mock Claude environment
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_claude_env(
    mock_zulip: MockZulipServer,
    zulip_credentials: Path,
) -> dict[str, str]:
    """Environment variables for mock_claude.py when run inside tmux."""
    return {
        "MOCK_CLAUDE_REPLY": "test reply from mock",
        "MOCK_CLAUDE_DELAY": "0.1",
        "MOCK_CLAUDE_STARTUP_DELAY": "0.2",
        "MOCK_HOOK_SCRIPT": str(RELAY_HOOK_SCRIPT),
        "ZULIP_SITE": mock_zulip.base_url,
        "ZULIP_BOT_EMAIL": "bot@test.example.com",
        "ZULIP_BOT_API_KEY_FILE": str(zulip_credentials),
    }


# ---------------------------------------------------------------------------
# Claude command interception
# ---------------------------------------------------------------------------

_CLAUDE_CMD_RE = re.compile(r"^claude\s+--")


@pytest.fixture
def patch_claude_cmd(mock_claude_env: dict[str, str]):
    """Patch subprocess.run to intercept tmux send-keys that start Claude.

    Replaces the `claude --session-id ...` command with
    `python3 mock_claude.py` while passing through all other subprocess calls.
    """
    original_run = subprocess.run

    def _patched_run(args, **kwargs):
        if (
            isinstance(args, list)
            and len(args) >= 4
            and args[0] == "tmux"
            and args[1] == "send-keys"
        ):
            # Check if any arg is a claude command
            for i, arg in enumerate(args):
                if isinstance(arg, str) and _CLAUDE_CMD_RE.match(arg):
                    # Build env var prefix for tmux send-keys
                    env_parts = []
                    for k, v in mock_claude_env.items():
                        env_parts.append(f"{k}='{v}'")
                    env_prefix = " ".join(env_parts)
                    mock_cmd = f"{env_prefix} {sys.executable} {MOCK_CLAUDE_SCRIPT}"
                    args = list(args)
                    args[i] = mock_cmd
                    break
        return original_run(args, **kwargs)

    with patch("subprocess.run", side_effect=_patched_run):
        yield


# ---------------------------------------------------------------------------
# Timing overrides for fast tests
# ---------------------------------------------------------------------------

@pytest.fixture
def fast_timing():
    """Override injector timing constants for fast tests."""
    import adapters.zulip_adapter.injector as inj
    old_idle = inj.IDLE_THRESHOLD
    old_poll = inj.POLL_INTERVAL
    old_grace = inj.Injector.STARTUP_GRACE
    inj.IDLE_THRESHOLD = 0.5
    inj.POLL_INTERVAL = 0.2
    inj.Injector.STARTUP_GRACE = 1.0
    yield
    inj.IDLE_THRESHOLD = old_idle
    inj.POLL_INTERVAL = old_poll
    inj.Injector.STARTUP_GRACE = old_grace


# ---------------------------------------------------------------------------
# tmux session cleanup
# ---------------------------------------------------------------------------

@pytest.fixture
def cleanup_tmux():
    """Kill all test-created tmux sessions on teardown."""
    sessions_before = _list_tmux_sessions()
    yield
    sessions_after = _list_tmux_sessions()
    new_sessions = sessions_after - sessions_before
    for name in new_sessions:
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass


def _list_tmux_sessions() -> set[str]:
    """List current tmux session names."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return set(result.stdout.strip().splitlines())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return set()


# ---------------------------------------------------------------------------
# Helpers for tests
# ---------------------------------------------------------------------------

def wait_for_prompt(session: str, timeout: float = 5.0) -> bool:
    """Wait until ❯ prompt appears in tmux pane."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session, "-p"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "❯" in result.stdout:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        time.sleep(0.2)
    return False


def wait_for_posted_messages(
    server: MockZulipServer,
    count: int = 1,
    timeout: float = 10.0,
) -> list[dict]:
    """Wait until the mock server has received at least `count` posted messages.

    Sync version — use async_wait_for_posted_messages in async tests.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = server.get_posted_messages()
        if len(msgs) >= count:
            return msgs
        time.sleep(0.2)
    return server.get_posted_messages()


async def async_wait_for_posted_messages(
    server: MockZulipServer,
    count: int = 1,
    timeout: float = 10.0,
) -> list[dict]:
    """Async version: yields to event loop between checks."""
    import asyncio
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = server.get_posted_messages()
        if len(msgs) >= count:
            return msgs
        await asyncio.sleep(0.2)
    return server.get_posted_messages()


def tmux_capture(session: str) -> str:
    """Capture tmux pane content."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
