"""Unit tests for pad_agent HeartbeatManager."""

from __future__ import annotations

import logging
from unittest.mock import patch

from libs.pad_agent.heartbeat import HeartbeatManager

log = logging.getLogger(__name__)


class TestHeartbeatManager:

    def test_fresh_instance_not_timed_out(self) -> None:
        """A newly created manager should NOT be timed out."""
        hb = HeartbeatManager(timeout_seconds=300)
        assert hb.is_timed_out() is False

    def test_timed_out_after_timeout_seconds(self) -> None:
        """After timeout_seconds elapse the manager reports timed out."""
        with patch("libs.pad_agent.heartbeat.time.monotonic") as mock_mono:
            mock_mono.return_value = 1000.0
            hb = HeartbeatManager(timeout_seconds=300)

            # Advance past the timeout
            mock_mono.return_value = 1301.0
            assert hb.is_timed_out() is True

    def test_record_successful_cycle_resets_timeout(self) -> None:
        """Recording a cycle should reset the timeout clock."""
        with patch("libs.pad_agent.heartbeat.time.monotonic") as mock_mono:
            mock_mono.return_value = 1000.0
            hb = HeartbeatManager(timeout_seconds=300)

            # Advance close to timeout
            mock_mono.return_value = 1299.0
            assert hb.is_timed_out() is False

            # Record a cycle (resets clock)
            hb.record_successful_cycle()

            # Advance again — should not be timed out relative to new baseline
            mock_mono.return_value = 1500.0
            assert hb.is_timed_out() is False

            # Advance past timeout from the recorded cycle
            mock_mono.return_value = 1600.0
            assert hb.is_timed_out() is True

    def test_seq_increments(self) -> None:
        """Each record_successful_cycle increments seq by one."""
        hb = HeartbeatManager()
        assert hb.seq == 0
        hb.record_successful_cycle()
        assert hb.seq == 1
        hb.record_successful_cycle()
        assert hb.seq == 2

    def test_state_snapshot_restore_roundtrip(self) -> None:
        """Snapshot/restore preserves seq and resets monotonic."""
        with patch("libs.pad_agent.heartbeat.time.monotonic") as mock_mono:
            mock_mono.return_value = 1000.0
            hb = HeartbeatManager(timeout_seconds=300)
            hb.record_successful_cycle()
            hb.record_successful_cycle()
            hb.record_successful_cycle()
            assert hb.seq == 3

            snap = hb.get_state_snapshot()
            assert snap["seq"] == 3
            assert "last_device_seen" in snap

            # Restore into a fresh manager — seq preserved, monotonic reset
            mock_mono.return_value = 5000.0
            hb2 = HeartbeatManager(timeout_seconds=300)
            hb2.restore_from_state(snap)
            assert hb2.seq == 3
            assert hb2.is_timed_out() is False  # monotonic just reset

    def test_no_cycle_recorded_not_timed_out(self) -> None:
        """With no cycle ever recorded the manager starts 'just-seen'."""
        hb = HeartbeatManager(timeout_seconds=10)
        assert hb.is_timed_out() is False
        assert hb.seq == 0
