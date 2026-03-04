"""Unit tests for pad_agent FIFO notifier."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from libs.pad_agent.constants import PIPE_BUF
from libs.pad_agent.notifier import PadNotifier

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPadNotifier:
    @patch("libs.pad_agent.notifier.os.close")
    @patch("libs.pad_agent.notifier.os.write")
    @patch("libs.pad_agent.notifier.os.open", return_value=42)
    def test_write_to_fifo(
        self,
        mock_open: MagicMock,
        mock_write: MagicMock,
        mock_close: MagicMock,
    ) -> None:
        notifier = PadNotifier(Path("/run/ccmux"))
        result = notifier.notify("test_event", "hello", "TestChild")

        assert result is True
        mock_open.assert_called_once_with(
            "/run/ccmux/in.pad", os.O_WRONLY | os.O_NONBLOCK
        )
        mock_write.assert_called_once()
        mock_close.assert_called_once_with(42)

    @patch("libs.pad_agent.notifier.os.close")
    @patch("libs.pad_agent.notifier.os.write")
    @patch("libs.pad_agent.notifier.os.open", return_value=42)
    def test_payload_format(
        self,
        mock_open: MagicMock,
        mock_write: MagicMock,
        mock_close: MagicMock,
    ) -> None:
        notifier = PadNotifier(Path("/run/ccmux"))
        notifier.notify("lock_change", "locked", "TestChild2", meta={"reason": "bedtime"})

        written_bytes = mock_write.call_args[0][1]
        payload = json.loads(written_bytes.decode("utf-8"))

        assert payload["channel"] == "pad"
        assert payload["event"] == "lock_change"
        assert payload["content"] == "locked"
        assert payload["child"] == "TestChild2"
        assert "ts" in payload
        assert payload["reason"] == "bedtime"

    @patch("libs.pad_agent.notifier.os.close")
    @patch("libs.pad_agent.notifier.os.write")
    @patch("libs.pad_agent.notifier.os.open", return_value=42)
    def test_payload_under_pipe_buf(
        self,
        mock_open: MagicMock,
        mock_write: MagicMock,
        mock_close: MagicMock,
    ) -> None:
        notifier = PadNotifier(Path("/run/ccmux"))
        # Send a very long content string.
        long_content = "x" * 8000
        notifier.notify("test", long_content, "TestChild")

        written_bytes = mock_write.call_args[0][1]
        assert len(written_bytes) < PIPE_BUF

    @patch("libs.pad_agent.notifier.os.open", side_effect=FileNotFoundError("no fifo"))
    def test_missing_fifo_no_crash(self, mock_open: MagicMock) -> None:
        notifier = PadNotifier(Path("/run/ccmux"))
        result = notifier.notify("test", "hello", "TestChild")

        assert result is False

    @patch("libs.pad_agent.notifier.os.close")
    @patch("libs.pad_agent.notifier.os.write")
    @patch("libs.pad_agent.notifier.os.open", return_value=42)
    def test_screen_time_update_helper(
        self,
        mock_open: MagicMock,
        mock_write: MagicMock,
        mock_close: MagicMock,
    ) -> None:
        notifier = PadNotifier(Path("/run/ccmux"))
        result = notifier.notify_screen_time_update(
            child_name="TestChild",
            active_min=25.5,
            daily_limit=60,
            session_min=10.0,
            session_limit=30,
        )

        assert result is True
        written_bytes = mock_write.call_args[0][1]
        payload = json.loads(written_bytes.decode("utf-8"))
        assert payload["event"] == "screen_time_update"
        assert payload["active_min"] == 25.5
        assert payload["daily_limit"] == 60

    @patch("libs.pad_agent.notifier.os.close")
    @patch("libs.pad_agent.notifier.os.write")
    @patch("libs.pad_agent.notifier.os.open", return_value=42)
    def test_lock_change_helper(
        self,
        mock_open: MagicMock,
        mock_write: MagicMock,
        mock_close: MagicMock,
    ) -> None:
        notifier = PadNotifier(Path("/run/ccmux"))
        result = notifier.notify_lock_change(
            child_name="TestChild2",
            action="lock",
            reason="bedtime",
            active_reasons=["bedtime", "daily_limit"],
        )

        assert result is True
        written_bytes = mock_write.call_args[0][1]
        payload = json.loads(written_bytes.decode("utf-8"))
        assert payload["event"] == "lock_change"
        assert payload["action"] == "lock"
        assert payload["reason"] == "bedtime"
        assert payload["active_reasons"] == ["bedtime", "daily_limit"]
