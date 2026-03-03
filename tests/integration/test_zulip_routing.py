"""Zulip adapter routing integration tests (T-ZR-01..11).

Tests message routing through _handle_message: stream dispatch, topic isolation,
echo prevention, attachment handling, and message formatting.

Most tests don't need tmux — they test the adapter's routing logic with
real FIFOs and a mock Zulip server but mock out process_mgr.ensure_instance
to avoid creating actual tmux sessions.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.zulip_adapter.adapter import ZulipAdapter
from adapters.zulip_adapter.config import StreamConfig, ZulipAdapterConfig, scan_streams
from adapters.zulip_adapter.process_mgr import CreateMode

# Import fixtures from conftest_zulip
from tests.integration.conftest_zulip import (
    wait_for_posted_messages,
)

pytestmark = [
    pytest.mark.zulip_integration,
    pytest.mark.usefixtures("fast_timing", "cleanup_tmux"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message_event(
    stream: str,
    topic: str,
    content: str,
    sender_email: str = "user@example.com",
    sender_full_name: str = "Test User",
    msg_type: str = "stream",
) -> dict:
    """Build a Zulip message event dict."""
    return {
        "type": "message",
        "message": {
            "type": msg_type,
            "sender_email": sender_email,
            "sender_full_name": sender_full_name,
            "display_recipient": stream,
            "subject": topic,
            "content": content,
            "timestamp": int(time.time()),
        },
    }


@pytest.fixture
def adapter_with_fifo(zulip_adapter: ZulipAdapter, tmp_path: Path):
    """Patch ensure_instance to create a real FIFO but no tmux session."""
    fifos: dict[str, Path] = {}

    async def _fake_ensure(stream, topic, stream_cfg):
        key = f"{stream}/{topic}"
        if key not in fifos:
            fifo_dir = tmp_path / "fifos" / stream / topic
            fifo_dir.mkdir(parents=True, exist_ok=True)
            fifo = fifo_dir / "in.zulip"
            os.mkfifo(str(fifo))
            fifos[key] = fifo
            # Open sentinel to prevent ENXIO
            fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
            # Store fd for cleanup
            if not hasattr(_fake_ensure, "_fds"):
                _fake_ensure._fds = []
            _fake_ensure._fds.append(fd)
            return fifo, CreateMode.FIRST_TIME
        return fifos[key], CreateMode.NONE

    _fake_ensure._fds = []
    zulip_adapter.process_mgr.ensure_instance = _fake_ensure

    yield zulip_adapter, fifos

    # Cleanup sentinel fds
    for fd in _fake_ensure._fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_fifo_nonblocking(fifo: Path) -> str:
    """Read all available data from a FIFO without blocking."""
    try:
        fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
        try:
            chunks = []
            while True:
                try:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    chunks.append(data)
                except BlockingIOError:
                    break
            return b"".join(chunks).decode("utf-8", errors="replace")
        finally:
            os.close(fd)
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# T-ZR-01: Single stream routing
# ---------------------------------------------------------------------------

class TestSingleStreamRouting:
    """T-ZR-01: Message arrives in correct FIFO."""

    @pytest.mark.asyncio
    async def test_message_routed_to_stream_fifo(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event("test-stream", "general", "hello world")

        await adapter._handle_message(event)

        data = _read_fifo_nonblocking(fifos["test-stream/general"])
        assert "hello world" in data

    @pytest.mark.asyncio
    async def test_message_has_nul_delimiter(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event("test-stream", "general", "test msg")

        await adapter._handle_message(event)

        fifo = fifos["test-stream/general"]
        fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
        try:
            data = os.read(fd, 4096)
            assert data.endswith(b"\0"), "FIFO message must be NUL-terminated"
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# T-ZR-02: Multi-stream isolation
# ---------------------------------------------------------------------------

class TestMultiStreamIsolation:
    """T-ZR-02: Each stream's FIFO receives only its messages."""

    @pytest.mark.asyncio
    async def test_two_streams_isolated(
        self, adapter_with_fifo, zulip_config, zulip_streams_dir, tmp_path
    ):
        adapter, fifos = adapter_with_fifo

        # Register a second stream (both in dict and on disk for hot-reload)
        project2 = tmp_path / "project2"
        project2.mkdir()
        stream_dir = zulip_streams_dir / "other-stream"
        stream_dir.mkdir()
        (stream_dir / "stream.toml").write_text(
            f'project_path = "{project2}"\nchannel = "zulip"\n'
        )
        # Force mtime cache invalidation so scan_streams picks up the new stream
        zulip_config._streams_mtime = 0.0

        event1 = _make_message_event("test-stream", "topic1", "msg for stream A")
        event2 = _make_message_event("other-stream", "topic1", "msg for stream B")

        await adapter._handle_message(event1)
        await adapter._handle_message(event2)

        data_a = _read_fifo_nonblocking(fifos["test-stream/topic1"])
        data_b = _read_fifo_nonblocking(fifos["other-stream/topic1"])

        assert "msg for stream A" in data_a
        assert "msg for stream B" in data_b
        assert "msg for stream B" not in data_a
        assert "msg for stream A" not in data_b


# ---------------------------------------------------------------------------
# T-ZR-03: Multi-topic isolation
# ---------------------------------------------------------------------------

class TestMultiTopicIsolation:
    """T-ZR-03: Separate FIFO per topic."""

    @pytest.mark.asyncio
    async def test_two_topics_separate_fifos(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo

        event1 = _make_message_event("test-stream", "topic-A", "msg A")
        event2 = _make_message_event("test-stream", "topic-B", "msg B")

        await adapter._handle_message(event1)
        await adapter._handle_message(event2)

        assert "test-stream/topic-A" in fifos
        assert "test-stream/topic-B" in fifos

        data_a = _read_fifo_nonblocking(fifos["test-stream/topic-A"])
        data_b = _read_fifo_nonblocking(fifos["test-stream/topic-B"])

        assert "msg A" in data_a
        assert "msg B" in data_b


# ---------------------------------------------------------------------------
# T-ZR-04: Unregistered stream
# ---------------------------------------------------------------------------

class TestUnregisteredStream:
    """T-ZR-04: No instance created, no crash."""

    @pytest.mark.asyncio
    async def test_unregistered_stream_ignored(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event("unknown-stream", "topic", "hello")

        # Should not raise
        await adapter._handle_message(event)

        # No FIFO created for unknown stream
        assert "unknown-stream/topic" not in fifos


# ---------------------------------------------------------------------------
# T-ZR-05: Bot echo prevention
# ---------------------------------------------------------------------------

class TestBotEchoPrevention:
    """T-ZR-05: sender_email == bot_email → ignored."""

    @pytest.mark.asyncio
    async def test_bot_message_ignored(self, adapter_with_fifo, zulip_config):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event(
            "test-stream",
            "general",
            "bot reply",
            sender_email=zulip_config.bot_email,
        )

        await adapter._handle_message(event)

        # No FIFO should be created — message was dropped
        assert "test-stream/general" not in fifos


# ---------------------------------------------------------------------------
# T-ZR-06: Private message ignored
# ---------------------------------------------------------------------------

class TestPrivateMessageIgnored:
    """T-ZR-06: type == "private" → dropped."""

    @pytest.mark.asyncio
    async def test_private_message_dropped(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event(
            "test-stream",
            "general",
            "private msg",
            msg_type="private",
        )

        await adapter._handle_message(event)
        assert "test-stream/general" not in fifos


# ---------------------------------------------------------------------------
# T-ZR-07: Empty content ignored
# ---------------------------------------------------------------------------

class TestEmptyContentIgnored:
    """T-ZR-07: Empty message → dropped."""

    @pytest.mark.asyncio
    async def test_empty_content_dropped(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event("test-stream", "general", "")

        await adapter._handle_message(event)
        assert "test-stream/general" not in fifos

    @pytest.mark.asyncio
    async def test_missing_stream_dropped(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event("", "general", "hello")

        await adapter._handle_message(event)
        assert len(fifos) == 0


# ---------------------------------------------------------------------------
# T-ZR-08: Hot-reload streams
# ---------------------------------------------------------------------------

class TestHotReloadStreams:
    """T-ZR-08: New stream.toml detected mid-run."""

    @pytest.mark.asyncio
    async def test_new_stream_detected(
        self, adapter_with_fifo, zulip_config, zulip_streams_dir, tmp_path
    ):
        adapter, fifos = adapter_with_fifo

        # Create a new stream directory mid-run
        new_project = tmp_path / "new-project"
        new_project.mkdir()
        new_stream_dir = zulip_streams_dir / "new-stream"
        new_stream_dir.mkdir()
        (new_stream_dir / "stream.toml").write_text(
            f'project_path = "{new_project}"\nchannel = "zulip"\n'
        )

        # Force mtime cache invalidation
        zulip_config._streams_mtime = 0.0

        # Message to the new stream should work after scan_streams
        event = _make_message_event("new-stream", "topic", "hello new stream")
        await adapter._handle_message(event)

        assert "new-stream/topic" in fifos
        data = _read_fifo_nonblocking(fifos["new-stream/topic"])
        assert "hello new stream" in data


# ---------------------------------------------------------------------------
# T-ZR-09: File attachment
# ---------------------------------------------------------------------------

class TestFileAttachment:
    """T-ZR-09: Download file → prepend path → strip raw link."""

    @pytest.mark.asyncio
    async def test_attachment_downloaded_and_stripped(
        self, adapter_with_fifo, mock_zulip, zulip_project_dir
    ):
        adapter, fifos = adapter_with_fifo

        # Register a file on the mock server
        file_content = b"test file content"
        mock_zulip.add_upload_file(
            "/user_uploads/1/ab/report.pdf",
            file_content,
        )

        event = _make_message_event(
            "test-stream",
            "general",
            "Here is the file [report.pdf](/user_uploads/1/ab/report.pdf) please review",
        )

        await adapter._handle_message(event)

        data = _read_fifo_nonblocking(fifos["test-stream/general"])
        # Should contain [File: ...] notification
        assert "[File:" in data
        # Raw /user_uploads/ link should be stripped
        assert "/user_uploads/" not in data
        # Display name should remain
        assert "report.pdf" in data

        # File should be downloaded to project dir
        download_dir = zulip_project_dir / ".zulip-uploads" / "general"
        assert (download_dir / "report.pdf").exists()
        assert (download_dir / "report.pdf").read_bytes() == file_content


# ---------------------------------------------------------------------------
# T-ZR-10: Message format
# ---------------------------------------------------------------------------

class TestMessageFormat:
    """T-ZR-10: [YY/MM/DD HH:MM From zulip] <content>."""

    @pytest.mark.asyncio
    async def test_message_format_prefix(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event("test-stream", "general", "formatted msg")

        await adapter._handle_message(event)

        data = _read_fifo_nonblocking(fifos["test-stream/general"])
        # Check format: [YY/MM/DD HH:MM From zulip]
        pattern = r"\[\d{2}/\d{2}/\d{2} \d{2}:\d{2} From zulip\]"
        assert re.search(pattern, data), f"Expected timestamp prefix in: {data}"
        assert "formatted msg" in data


# ---------------------------------------------------------------------------
# T-ZR-11: Special chars in topic
# ---------------------------------------------------------------------------

class TestSpecialCharsInTopic:
    """T-ZR-11: Chinese/colons/spaces → sanitized tmux name + correct routing."""

    @pytest.mark.asyncio
    async def test_chinese_topic_routed(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event("test-stream", "测试主题", "chinese topic msg")

        await adapter._handle_message(event)

        assert "test-stream/测试主题" in fifos
        data = _read_fifo_nonblocking(fifos["test-stream/测试主题"])
        assert "chinese topic msg" in data

    @pytest.mark.asyncio
    async def test_colons_and_spaces_routed(self, adapter_with_fifo):
        adapter, fifos = adapter_with_fifo
        event = _make_message_event(
            "test-stream", "topic: with spaces", "special chars"
        )

        await adapter._handle_message(event)

        assert "test-stream/topic: with spaces" in fifos
        data = _read_fifo_nonblocking(fifos["test-stream/topic: with spaces"])
        assert "special chars" in data
