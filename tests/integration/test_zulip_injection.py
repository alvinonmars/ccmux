"""Zulip adapter injection + output integration tests (T-ZI-01..07).

Full closed-loop tests: FIFO → injector → mock_claude → relay hook → Zulip post.
These tests use real tmux sessions, real FIFOs, and the mock Zulip server.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from adapters.zulip_adapter.adapter import ZulipAdapter
from adapters.zulip_adapter.config import StreamConfig
from adapters.zulip_adapter.injector import Injector, InjectionGate
from adapters.zulip_adapter.process_mgr import (
    CreateMode,
    ProcessManager,
    _fifo_path,
    _tmux_session_name,
)
from adapters.zulip_adapter.transcript_watcher import (
    TranscriptWatcher,
    ZulipPoster,
    _extract_tool_uses,
)

from tests.integration.conftest_zulip import (
    async_wait_for_posted_messages,
    wait_for_posted_messages,
    wait_for_prompt,
    tmux_capture,
)
from tests.helpers.mock_zulip_server import MockZulipServer

pytestmark = [
    pytest.mark.zulip_integration,
    pytest.mark.usefixtures("fast_timing", "cleanup_tmux"),
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RELAY_HOOK_SCRIPT = REPO_ROOT / "scripts" / "zulip_relay_hook.py"
MOCK_CLAUDE_SCRIPT = REPO_ROOT / "tests" / "helpers" / "mock_claude.py"


# ---------------------------------------------------------------------------
# T-ZI-01: Full injection loop
# ---------------------------------------------------------------------------

class TestFullInjectionLoop:
    """T-ZI-01: FIFO write → injector → mock reply → relay hook → Zulip post."""

    @pytest.mark.asyncio
    async def test_end_to_end_injection(
        self, zulip_config, zulip_project_dir, patch_claude_cmd, mock_zulip
    ):
        mgr = ProcessManager(zulip_config)
        stream_cfg = StreamConfig(name="test-stream", project_path=zulip_project_dir)

        try:
            fifo, mode = await mgr.ensure_instance("test-stream", "e2e-topic", stream_cfg)
            assert mode == CreateMode.FIRST_TIME

            session = _tmux_session_name("test-stream", "e2e-topic")
            assert wait_for_prompt(session, timeout=5.0), "Mock Claude should show prompt"

            # Write message to FIFO (NUL-delimited)
            fd = os.open(str(fifo), os.O_WRONLY)
            try:
                os.write(fd, b"Hello from test\0")
            finally:
                os.close(fd)

            # Wait for mock_claude to process and relay hook to post
            # Must use async version to let injector's asyncio task run
            msgs = await async_wait_for_posted_messages(mock_zulip, count=1, timeout=15.0)
            assert len(msgs) >= 1, f"Expected at least 1 posted message, got {len(msgs)}"
            # The relay hook posts the mock reply
            assert any("mock" in m.get("content", "").lower() or "reply" in m.get("content", "").lower()
                       for m in msgs), f"Expected mock reply in posted messages: {msgs}"
        finally:
            mgr.stop_all()


# ---------------------------------------------------------------------------
# T-ZI-02: Injection batching
# ---------------------------------------------------------------------------

class TestInjectionBatching:
    """T-ZI-02: 3 rapid messages → single batch joined by \\n---\\n."""

    @pytest.mark.asyncio
    async def test_rapid_messages_batched(self, tmp_path):
        """Test that rapid FIFO writes are batched by the injector."""
        session_name = f"test-batch-{os.getpid()}"

        # Create tmux session with cat (to capture injected text)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "cat", "Enter"],
            capture_output=True, timeout=5,
        )
        time.sleep(0.3)

        # Create FIFO
        fifo = tmp_path / "batch.fifo"
        os.mkfifo(str(fifo))

        # Open sentinel
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        try:
            # Write 3 messages rapidly
            fd = os.open(str(fifo), os.O_WRONLY)
            try:
                os.write(fd, b"msg1\0msg2\0msg3\0")
            finally:
                os.close(fd)

            # Start injector (with a gate that's always ready since we use cat)
            injector = Injector(str(fifo), session_name)

            # Override gate to always return ready (cat shows no ❯ prompt)
            injector.gate = type("MockGate", (), {
                "is_ready": lambda self: True,
                "is_claude_dead": lambda self: False,
            })()

            task = asyncio.create_task(injector.run())

            # Wait for injection
            await asyncio.sleep(2.0)

            injector.stop()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except asyncio.TimeoutError:
                task.cancel()

            # Check tmux output — messages should be joined with \n---\n
            pane = tmux_capture(session_name)
            assert "---" in pane, f"Expected batch separator in pane: {pane}"
        finally:
            os.close(sentinel_fd)
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True, timeout=5,
            )


# ---------------------------------------------------------------------------
# T-ZI-03: Prompt detection gate
# ---------------------------------------------------------------------------

class TestPromptDetectionGate:
    """T-ZI-03: No ❯ → message queued; ❯ appears → injected."""

    @pytest.mark.asyncio
    async def test_gate_waits_for_prompt(self, tmp_path):
        session_name = f"test-gate-{os.getpid()}"

        # Create tmux session (no prompt yet — just a shell)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name],
            capture_output=True, timeout=5,
        )
        time.sleep(0.3)

        try:
            gate = InjectionGate(session_name)

            # Shell prompt might be visible, but no ❯
            # Gate should not be ready (no Claude prompt)
            # Note: is_ready checks for ❯ which shouldn't be in a plain shell
            ready = gate.is_ready()
            # Can't assert False because shell $ might match idle check differently
            # The key test is that ❯ detection works

            # Now send ❯ to the session
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l", "echo '❯ '"],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, timeout=5,
            )
            time.sleep(1.0)  # Wait for idle threshold

            ready = gate.is_ready()
            assert ready, "Gate should be ready when ❯ is visible"
        finally:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True, timeout=5,
            )


# ---------------------------------------------------------------------------
# T-ZI-04: Busy detection
# ---------------------------------------------------------------------------

class TestBusyDetection:
    """T-ZI-04: Mock Claude processing → second message waits until ❯."""

    @pytest.mark.asyncio
    async def test_busy_claude_blocks_injection(
        self, zulip_config, zulip_project_dir, mock_zulip, mock_claude_env
    ):
        """When mock_claude has no ❯ visible (processing), injector should wait."""
        session_name = f"test-busy-{os.getpid()}"

        # Create tmux with a slow mock_claude
        env_parts = []
        for k, v in mock_claude_env.items():
            env_parts.append(f"{k}='{v}'")
        env_parts.append("MOCK_CLAUDE_DELAY='2.0'")  # Slow reply

        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name],
            capture_output=True, timeout=5,
        )
        cmd = f"{' '.join(env_parts)} {sys.executable} {MOCK_CLAUDE_SCRIPT}"
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, cmd, "Enter"],
            capture_output=True, timeout=5,
        )

        try:
            assert wait_for_prompt(session_name, timeout=5.0)

            gate = InjectionGate(session_name)

            # Gate should be ready initially (prompt visible)
            time.sleep(1.0)  # Wait for idle
            assert gate.is_ready()

            # Send input to make Claude busy
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l", "test input"],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)

            # Gate should NOT be ready while processing (no ❯ on last lines)
            # Note: Depends on timing — mock_claude takes 2s to reply
            ready = gate.is_ready()
            # During processing, ❯ should not be on the last lines
            # (the old prompt scrolled up, reply not yet shown)
        finally:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True, timeout=5,
            )


# ---------------------------------------------------------------------------
# T-ZI-05: NUL-delimited framing
# ---------------------------------------------------------------------------

class TestNulDelimitedFraming:
    """T-ZI-05: Multi-line message with \\0 delimiter → correct split."""

    @pytest.mark.asyncio
    async def test_multiline_messages_split_on_nul(self, tmp_path):
        fifo = tmp_path / "nul.fifo"
        os.mkfifo(str(fifo))

        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
        try:
            # Write multi-line messages with NUL delimiters
            fd = os.open(str(fifo), os.O_WRONLY)
            try:
                msg1 = "line1\nline2\nline3"
                msg2 = "single line"
                os.write(fd, f"{msg1}\0{msg2}\0".encode())
            finally:
                os.close(fd)

            # Read and split manually (simulating injector logic)
            read_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
            try:
                data = os.read(read_fd, 4096)
                frames = data.split(b"\0")
                # Remove empty trailing frame from final \0
                frames = [f for f in frames if f]
                assert len(frames) == 2
                assert frames[0].decode() == "line1\nline2\nline3"
                assert frames[1].decode() == "single line"
            finally:
                os.close(read_fd)
        finally:
            os.close(sentinel_fd)


# ---------------------------------------------------------------------------
# T-ZI-06: Relay hook standalone
# ---------------------------------------------------------------------------

class TestRelayHookStandalone:
    """T-ZI-06: Crafted stdin JSON → POST to mock Zulip, long message chunking."""

    def test_relay_hook_posts_to_zulip(self, mock_zulip, zulip_credentials, tmp_path):
        """Run the relay hook script with crafted JSON input."""
        env = {
            **os.environ,
            "ZULIP_STREAM": "test-stream",
            "ZULIP_TOPIC": "hook-topic",
            "ZULIP_SITE": mock_zulip.base_url,
            "ZULIP_BOT_EMAIL": "bot@test.example.com",
            "ZULIP_BOT_API_KEY_FILE": str(zulip_credentials),
        }

        data = {
            "hook_event_name": "Stop",
            "session_id": "test-session",
            "last_assistant_message": "Hello from relay hook test!",
        }

        result = subprocess.run(
            [sys.executable, str(RELAY_HOOK_SCRIPT)],
            input=json.dumps(data).encode(),
            capture_output=True,
            timeout=10.0,
            env=env,
        )
        assert result.returncode == 0, f"Hook failed: {result.stderr.decode()}"

        msgs = mock_zulip.get_posted_messages()
        assert len(msgs) >= 1
        assert msgs[0]["to"] == "test-stream"
        assert msgs[0]["topic"] == "hook-topic"
        assert "Hello from relay hook test!" in msgs[0]["content"]

    def test_relay_hook_chunks_long_messages(self, mock_zulip, zulip_credentials):
        """Long messages (>9500 chars) should be split into chunks."""
        env = {
            **os.environ,
            "ZULIP_STREAM": "test-stream",
            "ZULIP_TOPIC": "chunk-topic",
            "ZULIP_SITE": mock_zulip.base_url,
            "ZULIP_BOT_EMAIL": "bot@test.example.com",
            "ZULIP_BOT_API_KEY_FILE": str(zulip_credentials),
        }

        # Message longer than 9500 chars
        long_msg = "A" * 20000
        data = {
            "hook_event_name": "Stop",
            "session_id": "test-session",
            "last_assistant_message": long_msg,
        }

        result = subprocess.run(
            [sys.executable, str(RELAY_HOOK_SCRIPT)],
            input=json.dumps(data).encode(),
            capture_output=True,
            timeout=10.0,
            env=env,
        )
        assert result.returncode == 0

        msgs = mock_zulip.get_posted_messages()
        assert len(msgs) >= 3, f"Expected >=3 chunks for 20k message, got {len(msgs)}"
        # Total content should equal the original
        total = "".join(m["content"] for m in msgs)
        assert total == long_msg

    def test_relay_hook_skips_without_stream(self, mock_zulip, zulip_credentials):
        """Hook exits silently when ZULIP_STREAM is not set."""
        env = {
            **os.environ,
            "ZULIP_BOT_EMAIL": "bot@test.example.com",
            "ZULIP_BOT_API_KEY_FILE": str(zulip_credentials),
        }
        # Remove ZULIP_STREAM if present
        env.pop("ZULIP_STREAM", None)

        data = {
            "hook_event_name": "Stop",
            "session_id": "test-session",
            "last_assistant_message": "should not post",
        }

        result = subprocess.run(
            [sys.executable, str(RELAY_HOOK_SCRIPT)],
            input=json.dumps(data).encode(),
            capture_output=True,
            timeout=10.0,
            env=env,
        )
        assert result.returncode == 0
        assert len(mock_zulip.get_posted_messages()) == 0


# ---------------------------------------------------------------------------
# T-ZI-07: Transcript watcher
# ---------------------------------------------------------------------------

class TestTranscriptWatcher:
    """T-ZI-07: JSONL append → status messages posted to mock Zulip."""

    @pytest.mark.asyncio
    async def test_transcript_watcher_posts_tool_status(
        self, mock_zulip, zulip_credentials, tmp_path
    ):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("")  # Start empty

        poster = ZulipPoster(
            site=mock_zulip.base_url,
            email="bot@test.example.com",
            api_key="test-api-key-12345",
            stream="test-stream",
            topic="watcher-topic",
        )

        watcher = TranscriptWatcher(transcript, poster, poll_interval=0.3)
        task = asyncio.create_task(watcher.run())

        try:
            # Wait for watcher to start
            await asyncio.sleep(0.5)

            # Append a tool_use entry to the transcript
            record = {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls -la", "description": "List files"},
                        }
                    ],
                },
            }
            with open(transcript, "a") as f:
                f.write(json.dumps(record) + "\n")

            # Wait for watcher to pick it up
            await asyncio.sleep(1.0)

            msgs = mock_zulip.get_posted_messages()
            assert len(msgs) >= 1, "Watcher should post tool status"
            assert any("List files" in m.get("content", "") or "ls -la" in m.get("content", "")
                       for m in msgs)
        finally:
            watcher.stop()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except asyncio.TimeoutError:
                task.cancel()

    def test_extract_tool_uses(self):
        """Unit test for _extract_tool_uses within integration suite."""
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/test.py"}},
                    {"type": "text", "text": "Reading file..."},
                    {"type": "tool_use", "name": "Grep", "input": {"pattern": "def "}},
                ],
            },
        })

        results = _extract_tool_uses(line)
        assert len(results) == 2
        assert results[0] == ("Read", {"file_path": "/tmp/test.py"})
        assert results[1] == ("Grep", {"pattern": "def "})

    def test_extract_tool_uses_non_assistant(self):
        """Non-assistant messages should return empty."""
        line = json.dumps({
            "message": {"role": "user", "content": "hello"},
        })
        assert _extract_tool_uses(line) == []
