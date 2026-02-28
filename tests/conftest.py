"""Shared test fixtures for ccmux.

Fixture tiers (per acceptance-criteria.md):
  daemon       — ccmux Daemon with isolated test config (no real Claude)
  bare_pane    — tmux pane running `cat` (injection / terminal activity tests)
  mock_pane    — tmux pane running mock_pane.py (stdout pattern tests)
  fire_hook    — helper that calls ccmux/hook.py directly with crafted JSON
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, Generator

import libtmux
import pytest

from ccmux.config import Config
from ccmux.daemon import Daemon
from ccmux.pubsub import ControlServer, OutputBroadcaster

REPO_ROOT = Path(__file__).parent.parent
MOCK_PANE_SCRIPT = REPO_ROOT / "tests" / "helpers" / "mock_pane.py"
HOOK_SCRIPT = REPO_ROOT / "ccmux" / "hook.py"


# ---------------------------------------------------------------------------
# Port / socket helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Isolated Config for a single test: unique session name, tmp runtime dir."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return Config(
        project_name=f"test-{os.getpid()}",
        runtime_dir=runtime,
        idle_threshold=30,
        silence_timeout=1,          # short for tests
        backoff_initial=1,
        backoff_cap=10,
        project_root=REPO_ROOT,
        claude_proxy="",            # no proxy for mock tests; real_claude_proxy fixture sets this
    )


# ---------------------------------------------------------------------------
# Daemon fixture (networking only, no real tmux)
# ---------------------------------------------------------------------------

@pytest.fixture
async def net_daemon(test_config: Config) -> AsyncGenerator[Daemon, None]:
    """Start a Daemon with only control.sock + output.sock + FIFO manager.

    Does NOT set up tmux / Claude / pipe-pane. Suitable for FIFO, pub/sub,
    and hook-chain integration tests.
    """
    d = Daemon(test_config)
    # Partial startup: only networking components
    test_config.runtime_dir.mkdir(parents=True, exist_ok=True)

    d._broadcaster = OutputBroadcaster(test_config.output_sock)
    await d._broadcaster.start()

    d._control = ControlServer(
        test_config.control_sock,
        on_broadcast=d._on_broadcast,
        on_event=d._on_event,
    )
    await d._control.start()

    from ccmux.fifo import FifoManager
    d._fifo_mgr = FifoManager(callback=d._on_message)
    d._fifo_mgr.start(asyncio.get_event_loop())

    # Default input FIFO
    default_in = test_config.runtime_dir / "in"
    os.mkfifo(str(default_in))
    d._fifo_mgr.add(default_in)

    yield d

    # Teardown
    if d._fifo_mgr:
        d._fifo_mgr.stop_all()
    if d._control:
        await d._control.stop()
    if d._broadcaster:
        await d._broadcaster.stop()


# ---------------------------------------------------------------------------
# Pub/sub only fixture (no Daemon)
# ---------------------------------------------------------------------------

@pytest.fixture
async def broadcaster(test_config: Config) -> AsyncGenerator[OutputBroadcaster, None]:
    """Standalone OutputBroadcaster for pub/sub tests."""
    b = OutputBroadcaster(test_config.output_sock)
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
async def control_server(
    test_config: Config,
) -> AsyncGenerator[tuple[ControlServer, list, list], None]:
    """Standalone ControlServer; collects broadcasts and events in lists."""
    broadcasts: list[dict] = []
    events: list[dict] = []

    cs = ControlServer(
        test_config.control_sock,
        on_broadcast=broadcasts.append,
        on_event=events.append,
    )
    await cs.start()
    yield cs, broadcasts, events
    await cs.stop()


# ---------------------------------------------------------------------------
# tmux pane fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmux_server() -> Generator[libtmux.Server, None, None]:
    """A libtmux Server for test pane management."""
    yield libtmux.Server()


@pytest.fixture
def bare_pane(
    tmux_server: libtmux.Server, test_config: Config
) -> Generator[libtmux.Pane, None, None]:
    """tmux pane running `cat` — suitable for injection and terminal activity tests."""
    session_name = test_config.tmux_session
    session = tmux_server.new_session(session_name=session_name, window_name="test")
    pane = session.active_window.active_pane
    pane.send_keys("cat", enter=True)
    time.sleep(0.3)
    yield pane
    try:
        tmux_server.kill_session(target_session=session_name)
    except Exception:
        pass


@pytest.fixture
def mock_pane(
    tmux_server: libtmux.Server, test_config: Config, tmp_path: Path
) -> Generator[libtmux.Pane, None, None]:
    """tmux pane running mock_pane.py — returns pane; env config via env_for_mock_pane."""
    session_name = test_config.tmux_session
    session = tmux_server.new_session(session_name=session_name, window_name="test")
    pane = session.active_window.active_pane
    pane.send_keys(
        f"{sys.executable} {MOCK_PANE_SCRIPT}",
        enter=True,
    )
    time.sleep(0.4)
    yield pane
    try:
        tmux_server.kill_session(target_session=session_name)
    except Exception:
        pass


@pytest.fixture
def make_mock_pane(
    tmux_server: libtmux.Server, test_config: Config
) -> Generator:
    """Factory fixture: creates tmux panes running mock_pane.py with custom env vars.

    Each call creates a uniquely-named tmux session; all are killed on teardown.

    Usage:
        pane = make_mock_pane()                          # default env
        pane = make_mock_pane({"MOCK_SPINNER": "15"})   # custom env
    """
    import itertools

    counter = itertools.count()
    sessions: list[str] = []

    def _factory(env: dict[str, str] | None = None) -> libtmux.Pane:
        session_name = f"{test_config.tmux_session}-mp{next(counter)}"
        sessions.append(session_name)
        session = tmux_server.new_session(session_name=session_name, window_name="test")
        pane = session.active_window.active_pane
        env_prefix = " ".join(f"{k}='{v}'" for k, v in (env or {}).items())
        cmd = f"{env_prefix} {sys.executable} {MOCK_PANE_SCRIPT}".strip()
        pane.send_keys(cmd, enter=True)
        time.sleep(0.4)
        return pane

    yield _factory

    for name in sessions:
        try:
            tmux_server.kill_session(target_session=name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# fire_hook fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fire_hook(test_config: Config):
    """Return a function that calls ccmux/hook.py with crafted JSON.

    Usage:
        result = fire_hook("Stop", {"session_id": "s1", "transcript_path": "..."})
    """
    def _fire(event: str, data: dict | None = None) -> subprocess.CompletedProcess:
        payload = {
            "hook_event_name": event,
            "session_id": data.get("session_id", "test-session") if data else "test-session",
            "cwd": str(test_config.project_root),
            "permission_mode": "default",
        }
        if data:
            payload.update(data)
        env = {
            **os.environ,
            "CCMUX_CONTROL_SOCK": str(test_config.control_sock),
        }
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=10.0,
            env=env,
        )

    return _fire


# ---------------------------------------------------------------------------
# Output socket subscriber helper
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# real_claude fixture
# ---------------------------------------------------------------------------

_PROXY_URL = "http://127.0.0.1:8118"


@pytest.fixture
def real_claude_proxy(test_config: Config):
    """Fixture for tests requiring real Claude Code (marked real_claude).

    Checks that the HTTP proxy is running at port 8118; skips the test if not.
    Sets test_config.claude_proxy so the daemon passes the proxy only to the
    claude send-keys command. The Python test process and all other subprocesses
    are NOT affected — os.environ is never modified.

    Usage:
        @pytest.mark.real_claude
        async def test_foo(real_claude_proxy, test_config, ...):
            ...  # test_config.claude_proxy is already set

    To run real_claude tests:
        .venv/bin/python -m pytest tests/ -m real_claude -v
    """
    result = subprocess.run(
        ["curl", "-s", "--proxy", _PROXY_URL, "--max-time", "3", "https://ipinfo.io/ip"],
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 0:
        pytest.skip(f"HTTP proxy not running at {_PROXY_URL} — start proxy before running real_claude tests")

    test_config.claude_proxy = _PROXY_URL
    yield
    test_config.claude_proxy = ""


# ---------------------------------------------------------------------------
# pytest markers
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_claude: mark test as requiring real claude binary and HTTP proxy",
    )
