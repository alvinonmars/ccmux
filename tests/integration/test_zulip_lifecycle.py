"""Zulip adapter lifecycle integration tests (T-ZL-01..07).

Tests instance lifecycle: creation, resume, fallback, crash recovery,
injector recovery, clean shutdown, and warm path.

These tests use real tmux sessions with mock_claude.py and a mock Zulip server.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from adapters.zulip_adapter.process_mgr import (
    CreateMode,
    ProcessManager,
    _fifo_path,
    _pid_file,
    _runtime_dir,
    _sanitize_name,
    _session_jsonl_exists,
    _tmux_has_session,
    _tmux_session_name,
    _write_instance_toml,
)
from adapters.zulip_adapter.config import StreamConfig

from tests.integration.conftest_zulip import wait_for_prompt

pytestmark = [
    pytest.mark.zulip_integration,
    pytest.mark.usefixtures("fast_timing", "cleanup_tmux"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _instance_toml_path(cfg, stream, topic) -> Path:
    return cfg.streams_dir / _sanitize_name(stream) / _sanitize_name(topic) / "instance.toml"


# ---------------------------------------------------------------------------
# T-ZL-01: First-time creation
# ---------------------------------------------------------------------------

class TestFirstTimeCreation:
    """T-ZL-01: tmux session, FIFO, PID file, instance.toml, notification."""

    @pytest.mark.asyncio
    async def test_first_time_creates_all_artifacts(
        self, zulip_config, zulip_project_dir, patch_claude_cmd, mock_zulip
    ):
        mgr = ProcessManager(zulip_config)
        stream_cfg = StreamConfig(name="test-stream", project_path=zulip_project_dir)

        try:
            fifo, mode = await mgr.ensure_instance("test-stream", "topic1", stream_cfg)

            assert mode == CreateMode.FIRST_TIME
            assert fifo.exists(), "FIFO should be created"
            assert fifo.name == "in.zulip"

            # PID file
            pf = _pid_file(zulip_config, "test-stream", "topic1")
            assert pf.exists(), "PID file should be created"
            pid = int(pf.read_text().strip())
            assert pid > 0

            # tmux session
            session = _tmux_session_name("test-stream", "topic1")
            assert _tmux_has_session(session), "tmux session should exist"

            # instance.toml
            inst = _instance_toml_path(zulip_config, "test-stream", "topic1")
            assert inst.exists(), "instance.toml should be created"
            content = inst.read_text()
            assert "session_id" in content

            # Prompt should appear
            assert wait_for_prompt(session, timeout=5.0)
        finally:
            mgr.stop_all()


# ---------------------------------------------------------------------------
# T-ZL-02: Session resume
# ---------------------------------------------------------------------------

class TestSessionResume:
    """T-ZL-02: CreateMode.RESUMED, --resume flag."""

    @pytest.mark.asyncio
    async def test_resume_with_existing_jsonl(
        self, zulip_config, zulip_project_dir, patch_claude_cmd
    ):
        mgr = ProcessManager(zulip_config)
        stream_cfg = StreamConfig(name="test-stream", project_path=zulip_project_dir)

        try:
            # First creation
            fifo, mode = await mgr.ensure_instance("test-stream", "resume-topic", stream_cfg)
            assert mode == CreateMode.FIRST_TIME

            # Read the session_id from instance.toml
            inst = _instance_toml_path(zulip_config, "test-stream", "resume-topic")
            import sys
            if sys.version_info >= (3, 11):
                import tomllib
            else:
                try:
                    import tomllib
                except ModuleNotFoundError:
                    import tomli as tomllib
            with open(inst, "rb") as f:
                data = tomllib.load(f)
            session_id = data["session_id"]

            # Create a fake session JSONL to simulate existing session
            from adapters.zulip_adapter.process_mgr import _claude_session_dir
            session_dir = _claude_session_dir(zulip_project_dir)
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / f"{session_id}.jsonl").write_text('{"test": true}\n')

            # Kill the tmux session and PID to force recreation
            session_name = _tmux_session_name("test-stream", "resume-topic")
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True, timeout=5,
            )
            pf = _pid_file(zulip_config, "test-stream", "resume-topic")
            if pf.exists():
                pf.unlink()

            # Stop old injector
            mgr.stop_all()
            mgr._injectors.clear()
            mgr._injector_tasks.clear()
            mgr._sentinel_fds.clear()

            # Second creation should resume
            fifo2, mode2 = await mgr.ensure_instance("test-stream", "resume-topic", stream_cfg)
            assert mode2 == CreateMode.RESUMED
        finally:
            mgr.stop_all()


# ---------------------------------------------------------------------------
# T-ZL-03: Session fallback
# ---------------------------------------------------------------------------

class TestSessionFallback:
    """T-ZL-03: Missing JSONL → CreateMode.FALLBACK."""

    @pytest.mark.asyncio
    async def test_fallback_when_jsonl_missing(
        self, zulip_config, zulip_project_dir, patch_claude_cmd
    ):
        mgr = ProcessManager(zulip_config)
        stream_cfg = StreamConfig(name="test-stream", project_path=zulip_project_dir)

        try:
            # Create instance first
            fifo, mode = await mgr.ensure_instance("test-stream", "fallback-topic", stream_cfg)
            assert mode == CreateMode.FIRST_TIME

            # Kill session + pid to force recreation
            session_name = _tmux_session_name("test-stream", "fallback-topic")
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True, timeout=5,
            )
            pf = _pid_file(zulip_config, "test-stream", "fallback-topic")
            if pf.exists():
                pf.unlink()

            mgr.stop_all()
            mgr._injectors.clear()
            mgr._injector_tasks.clear()
            mgr._sentinel_fds.clear()

            # instance.toml has session_id but JSONL does not exist → FALLBACK
            fifo2, mode2 = await mgr.ensure_instance("test-stream", "fallback-topic", stream_cfg)
            assert mode2 == CreateMode.FALLBACK
        finally:
            mgr.stop_all()


# ---------------------------------------------------------------------------
# T-ZL-04: Instance death detection
# ---------------------------------------------------------------------------

class TestInstanceDeathDetection:
    """T-ZL-04: Kill tmux externally → next message triggers _lazy_create."""

    @pytest.mark.asyncio
    async def test_dead_instance_recreated(
        self, zulip_config, zulip_project_dir, patch_claude_cmd
    ):
        mgr = ProcessManager(zulip_config)
        stream_cfg = StreamConfig(name="test-stream", project_path=zulip_project_dir)

        try:
            # Create instance
            fifo, mode = await mgr.ensure_instance("test-stream", "death-topic", stream_cfg)
            assert mode == CreateMode.FIRST_TIME

            # Kill the tmux session externally
            session_name = _tmux_session_name("test-stream", "death-topic")
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True, timeout=5,
            )
            # Remove PID file to simulate dead instance
            pf = _pid_file(zulip_config, "test-stream", "death-topic")
            if pf.exists():
                pf.unlink()

            # Wait for injector to notice
            await asyncio.sleep(0.5)

            # Next ensure_instance should recreate
            fifo2, mode2 = await mgr.ensure_instance("test-stream", "death-topic", stream_cfg)
            assert mode2 in (CreateMode.FIRST_TIME, CreateMode.FALLBACK)
            assert _tmux_has_session(session_name)
        finally:
            mgr.stop_all()


# ---------------------------------------------------------------------------
# T-ZL-05: Injector crash recovery
# ---------------------------------------------------------------------------

class TestInjectorCrashRecovery:
    """T-ZL-05: Force injector task.done() → new injector started."""

    @pytest.mark.asyncio
    async def test_injector_crash_triggers_recreate(
        self, zulip_config, zulip_project_dir, patch_claude_cmd
    ):
        mgr = ProcessManager(zulip_config)
        stream_cfg = StreamConfig(name="test-stream", project_path=zulip_project_dir)

        try:
            fifo, mode = await mgr.ensure_instance("test-stream", "inj-crash", stream_cfg)
            assert mode == CreateMode.FIRST_TIME

            # Cancel the injector task to simulate crash
            key = "test-stream/inj-crash"
            if key in mgr._injector_tasks:
                mgr._injector_tasks[key].cancel()
                try:
                    await mgr._injector_tasks[key]
                except asyncio.CancelledError:
                    pass

            # The task should now be done
            assert mgr._injector_tasks[key].done()

            # Next ensure_instance should detect dead injector and recreate
            fifo2, mode2 = await mgr.ensure_instance("test-stream", "inj-crash", stream_cfg)
            assert mode2 != CreateMode.NONE, "Should recreate after injector crash"

            # New injector task should be running
            assert key in mgr._injector_tasks
            assert not mgr._injector_tasks[key].done()
        finally:
            mgr.stop_all()


# ---------------------------------------------------------------------------
# T-ZL-06: Clean shutdown
# ---------------------------------------------------------------------------

class TestCleanShutdown:
    """T-ZL-06: adapter.stop() → all injectors stopped, sentinel fds closed."""

    @pytest.mark.asyncio
    async def test_stop_all_cleans_up(
        self, zulip_config, zulip_project_dir, patch_claude_cmd
    ):
        mgr = ProcessManager(zulip_config)
        stream_cfg = StreamConfig(name="test-stream", project_path=zulip_project_dir)

        fifo, mode = await mgr.ensure_instance("test-stream", "shutdown-topic", stream_cfg)
        assert mode == CreateMode.FIRST_TIME

        key = "test-stream/shutdown-topic"
        assert key in mgr._injectors
        assert key in mgr._injector_tasks
        assert key in mgr._sentinel_fds

        mgr.stop_all()

        assert len(mgr._injectors) == 0
        assert len(mgr._injector_tasks) == 0
        assert len(mgr._sentinel_fds) == 0


# ---------------------------------------------------------------------------
# T-ZL-07: Warm path (no recreation)
# ---------------------------------------------------------------------------

class TestWarmPath:
    """T-ZL-07: Second message → CreateMode.NONE, no new session notification."""

    @pytest.mark.asyncio
    async def test_warm_path_no_recreation(
        self, zulip_config, zulip_project_dir, patch_claude_cmd
    ):
        mgr = ProcessManager(zulip_config)
        stream_cfg = StreamConfig(name="test-stream", project_path=zulip_project_dir)

        try:
            # First call creates
            fifo1, mode1 = await mgr.ensure_instance("test-stream", "warm-topic", stream_cfg)
            assert mode1 == CreateMode.FIRST_TIME

            # Wait for prompt
            session_name = _tmux_session_name("test-stream", "warm-topic")
            wait_for_prompt(session_name, timeout=5.0)

            # Second call should be warm (NONE)
            fifo2, mode2 = await mgr.ensure_instance("test-stream", "warm-topic", stream_cfg)
            assert mode2 == CreateMode.NONE
            assert fifo1 == fifo2
        finally:
            mgr.stop_all()
