"""Unit tests for pad_agent ADB connection manager."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from libs.pad_agent.adb import ADBError
from libs.pad_agent.adb_manager import ADBManager, ConnectionState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(**kwargs: object) -> ADBManager:
    """Create an ADBManager with a mocked ADB instance."""
    with patch("libs.pad_agent.adb_manager.ADB") as mock_adb_cls:
        mock_adb = MagicMock()
        mock_adb_cls.return_value = mock_adb
        mgr = ADBManager(host="10.0.0.1", port=5555, **kwargs)
    # Replace the internal _adb with a fresh mock we can control.
    mgr._adb = mock_adb
    return mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestADBManager:
    def test_connect_success(self) -> None:
        mgr = _make_manager()
        mgr._adb.connect_wireless = MagicMock()

        result = mgr.connect()

        assert result is True
        assert mgr.state == ConnectionState.CONNECTED
        assert mgr._last_connected is not None
        mgr._adb.connect_wireless.assert_called_once_with("10.0.0.1", 5555)

    def test_connect_failure(self) -> None:
        mgr = _make_manager()
        mgr._adb.connect_wireless = MagicMock(side_effect=ADBError("refused"))

        result = mgr.connect()

        assert result is False
        assert mgr.state == ConnectionState.DISCONNECTED

    @patch("libs.pad_agent.adb_manager.time.sleep")
    def test_reconnect_with_backoff(self, mock_sleep: MagicMock) -> None:
        mgr = _make_manager(backoff_initial=1.0, backoff_cap=120.0)
        mgr._adb.connect_wireless = MagicMock(side_effect=ADBError("refused"))

        # First failure: sleep(1.0), next backoff = 2.0
        mgr.reconnect()
        mock_sleep.assert_called_with(1.0)

        # Second failure: sleep(2.0), next backoff = 4.0
        mgr.reconnect()
        mock_sleep.assert_called_with(2.0)

        # Third failure: sleep(4.0), next backoff = 8.0
        mgr.reconnect()
        mock_sleep.assert_called_with(4.0)

    @patch("libs.pad_agent.adb_manager.time.sleep")
    def test_backoff_caps_at_max(self, mock_sleep: MagicMock) -> None:
        mgr = _make_manager(backoff_initial=64.0, backoff_cap=120.0)
        mgr._adb.connect_wireless = MagicMock(side_effect=ADBError("refused"))

        mgr.reconnect()  # sleep(64), next = 120 (capped)
        mgr.reconnect()  # sleep(120), next = 120 (capped)
        mgr.reconnect()  # sleep(120), still capped

        assert mock_sleep.call_args_list[-1][0][0] == 120.0

    @patch("libs.pad_agent.adb_manager.time.sleep")
    def test_backoff_resets_on_success(self, mock_sleep: MagicMock) -> None:
        mgr = _make_manager(backoff_initial=1.0, backoff_cap=120.0)

        # Fail twice to increase backoff.
        mgr._adb.connect_wireless = MagicMock(side_effect=ADBError("refused"))
        mgr.reconnect()
        mgr.reconnect()

        # Now succeed.
        mgr._adb.connect_wireless = MagicMock()
        mgr.reconnect()

        assert mgr._backoff == 1.0
        assert mgr._reconnect_attempts == 0
        assert mgr.state == ConnectionState.CONNECTED

    def test_liveness_check_pass(self) -> None:
        mgr = _make_manager()
        mgr._adb.shell = MagicMock(return_value="ok")

        assert mgr.is_device_responsive() is True

    def test_liveness_check_fail(self) -> None:
        mgr = _make_manager()
        mgr._adb.shell = MagicMock(side_effect=ADBError("timeout"))

        assert mgr.is_device_responsive() is False

    @patch("libs.pad_agent.adb_manager.time.sleep")
    def test_targeted_disconnect_after_3_failures(
        self, mock_sleep: MagicMock
    ) -> None:
        mgr = _make_manager()
        mgr._adb.connect_wireless = MagicMock(side_effect=ADBError("refused"))
        mgr._adb.run = MagicMock()

        for _ in range(3):
            mgr.reconnect()

        # After 3 failures, disconnect should have been called.
        disconnect_calls = [
            c for c in mgr._adb.run.call_args_list
            if c[0][0] == ["disconnect", "10.0.0.1:5555"]
        ]
        assert len(disconnect_calls) >= 1

        # kill-server should NOT have been called yet.
        kill_calls = [
            c for c in mgr._adb.run.call_args_list
            if c[0][0] == ["kill-server"]
        ]
        assert len(kill_calls) == 0

    @patch("libs.pad_agent.adb_manager.time.sleep")
    def test_kill_server_after_10_failures(self, mock_sleep: MagicMock) -> None:
        mgr = _make_manager()
        mgr._adb.connect_wireless = MagicMock(side_effect=ADBError("refused"))
        mgr._adb.run = MagicMock()

        for _ in range(10):
            mgr.reconnect()

        kill_calls = [
            c for c in mgr._adb.run.call_args_list
            if c[0][0] == ["kill-server"]
        ]
        assert len(kill_calls) >= 1

    def test_state_snapshot_restore(self) -> None:
        mgr = _make_manager(backoff_initial=1.0)
        mgr._reconnect_attempts = 5
        mgr._backoff = 32.0

        snapshot = mgr.get_state_snapshot()
        assert snapshot["reconnect_attempts"] == 5
        assert snapshot["backoff_seconds"] == 32.0
        assert snapshot["device_serial"] == "10.0.0.1:5555"

        mgr2 = _make_manager(backoff_initial=1.0)
        mgr2.restore_from_state(snapshot)
        assert mgr2._reconnect_attempts == 5
        assert mgr2._backoff == 32.0
