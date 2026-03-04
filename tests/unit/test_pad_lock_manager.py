"""Unit tests for pad_agent lock manager."""

from __future__ import annotations

import datetime
import logging
from unittest.mock import MagicMock, patch

import pytest

from libs.pad_agent.lock_manager import LockManager
from libs.pad_agent.policy import LockReason, PolicyEvaluation

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lock_manager() -> LockManager:
    """Create a LockManager with a mocked ADBManager and patched requests."""
    adb_mgr = MagicMock()
    mgr = LockManager(
        adb_mgr=adb_mgr,
        fk_password="testpass",
        fk_base_url="http://10.0.0.1:2323",
        lock_url="file:///sdcard/kidpad/lock.html",
        dashboard_url="file:///sdcard/kidpad/dashboard.html",
    )
    return mgr


def _make_evaluation(
    lock_reasons: set[LockReason] | None = None,
    unlock_reasons: set[LockReason] | None = None,
) -> PolicyEvaluation:
    """Create a PolicyEvaluation with given reasons."""
    return PolicyEvaluation(
        lock_reasons=lock_reasons or set(),
        unlock_reasons=unlock_reasons or set(),
        eye_break_due=False,
        eye_break_expired=False,
        daily_limit_reached=False,
        session_limit_reached=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@patch("libs.pad_agent.lock_manager.requests")
class TestLockManager:
    def test_add_first_reason_locks(self, mock_requests: MagicMock) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        result = mgr.add_reason(LockReason.BEDTIME)

        assert result is True
        assert mgr.is_locked is True
        mock_requests.get.assert_called_once()
        # Verify lock URL contains the reason.
        call_kwargs = mock_requests.get.call_args
        assert "reason=bedtime" in call_kwargs.kwargs.get("params", call_kwargs[1].get("params", {})).get("url", "")

    def test_add_second_reason_no_device_action(
        self, mock_requests: MagicMock
    ) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        mgr.add_reason(LockReason.BEDTIME)
        mock_requests.get.reset_mock()

        result = mgr.add_reason(LockReason.DAILY_LIMIT)

        assert result is False  # no state transition
        mock_requests.get.assert_not_called()

    def test_remove_one_of_two_stays_locked(
        self, mock_requests: MagicMock
    ) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        mgr.add_reason(LockReason.BEDTIME)
        mgr.add_reason(LockReason.DAILY_LIMIT)
        mock_requests.get.reset_mock()

        result = mgr.remove_reason(LockReason.DAILY_LIMIT)

        assert result is False  # still locked (bedtime remains)
        assert mgr.is_locked is True
        mock_requests.get.assert_not_called()

    def test_remove_last_reason_unlocks(self, mock_requests: MagicMock) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        mgr.add_reason(LockReason.BEDTIME)
        mock_requests.get.reset_mock()

        result = mgr.remove_reason(LockReason.BEDTIME)

        assert result is True  # state transition: LOCKED -> UNLOCKED
        assert mgr.is_locked is False
        # Verify unlock navigated to dashboard.
        call_kwargs = mock_requests.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params.get("url") == "file:///sdcard/kidpad/dashboard.html"

    def test_duplicate_add_noop(self, mock_requests: MagicMock) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        mgr.add_reason(LockReason.MANUAL)
        result = mgr.add_reason(LockReason.MANUAL)

        assert result is False

    def test_duplicate_remove_noop(self, mock_requests: MagicMock) -> None:
        mgr = _make_lock_manager()

        result = mgr.remove_reason(LockReason.MANUAL)

        assert result is False

    def test_apply_evaluation_mixed(self, mock_requests: MagicMock) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        # Pre-add a reason that will be unlocked.
        mgr.add_reason(LockReason.SESSION_LIMIT)
        mock_requests.get.reset_mock()

        evaluation = _make_evaluation(
            lock_reasons={LockReason.BEDTIME},
            unlock_reasons={LockReason.SESSION_LIMIT},
        )
        transitions = mgr.apply_evaluation(evaluation)

        assert ("lock", LockReason.BEDTIME) in transitions
        assert ("unlock", LockReason.SESSION_LIMIT) in transitions
        assert mgr.is_locked is True  # bedtime still active

    def test_apply_evaluation_skips_eye_break(
        self, mock_requests: MagicMock
    ) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        evaluation = _make_evaluation(
            lock_reasons={LockReason.EYE_BREAK, LockReason.BEDTIME},
        )
        transitions = mgr.apply_evaluation(evaluation)

        assert ("lock", LockReason.BEDTIME) in transitions
        # EYE_BREAK should NOT appear in transitions.
        eye_break_transitions = [t for t in transitions if t[1] == LockReason.EYE_BREAK]
        assert len(eye_break_transitions) == 0
        assert LockReason.EYE_BREAK not in mgr._active_reasons

    def test_eye_break_started_at_tracked(
        self, mock_requests: MagicMock
    ) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)

        mgr.add_reason(LockReason.EYE_BREAK, now=now)

        assert mgr.eye_break_started_at == now

    def test_lock_device_uses_primary_reason(
        self, mock_requests: MagicMock
    ) -> None:
        """When multiple reasons are active, lock URL uses highest priority."""
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        # Manually add reasons without triggering _lock_device.
        mgr._active_reasons = {LockReason.SESSION_LIMIT, LockReason.DAILY_LIMIT}
        mgr._lock_device(mgr._get_primary_reason())

        call_kwargs = mock_requests.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        # daily_limit has higher priority than session_limit.
        assert "reason=daily_limit" in params.get("url", "")

    def test_assert_device_state_locked(
        self, mock_requests: MagicMock
    ) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )
        mgr._adb_mgr.shell.return_value = "12345"

        # Make it locked.
        mgr._active_reasons = {LockReason.BEDTIME}
        mgr.assert_device_state()

        # Should have called FK to load lock page.
        call_kwargs = mock_requests.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert "lock.html" in params.get("url", "")

    def test_assert_device_state_unlocked(
        self, mock_requests: MagicMock
    ) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )
        mgr._adb_mgr.shell.return_value = "12345"

        # No active reasons = unlocked.
        mgr.assert_device_state()

        call_kwargs = mock_requests.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params.get("url") == "file:///sdcard/kidpad/dashboard.html"

    def test_reason_history_capped(self, mock_requests: MagicMock) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        # Add and remove 60 times (120 history entries, should cap at 100).
        for _ in range(60):
            mgr.add_reason(LockReason.MANUAL)
            mgr.remove_reason(LockReason.MANUAL)

        assert len(mgr._reason_history) <= 100

    def test_state_snapshot_restore(self, mock_requests: MagicMock) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={"status": "ok"}),
            raise_for_status=MagicMock(),
        )

        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
        mgr.add_reason(LockReason.BEDTIME)
        mgr.add_reason(LockReason.EYE_BREAK, now=now)

        snapshot = mgr.get_state_snapshot()
        assert snapshot["is_locked"] is True
        assert "bedtime" in snapshot["active_reasons"]
        assert "eye_break" in snapshot["active_reasons"]

        mgr2 = _make_lock_manager()
        mgr2.restore_from_state(snapshot)
        assert mgr2._active_reasons == {LockReason.BEDTIME, LockReason.EYE_BREAK}
        assert mgr2._eye_break_started_at == now

    def test_fk_request_timeout_handling(
        self, mock_requests: MagicMock
    ) -> None:
        import requests as real_requests

        mgr = _make_lock_manager()
        mock_requests.get.side_effect = real_requests.Timeout("timeout")
        mock_requests.RequestException = real_requests.RequestException

        result = mgr._fk_request({"cmd": "test", "password": "testpass"})

        assert result is None
        # Should have retried (1 + FK_REST_MAX_RETRIES = 2 attempts).
        assert mock_requests.get.call_count == 2

    def test_verify_fk_page(self, mock_requests: MagicMock) -> None:
        mgr = _make_lock_manager()
        mock_requests.get.return_value = MagicMock(
            json=MagicMock(return_value={
                "currentPage": "file:///sdcard/kidpad/dashboard.html",
                "deviceName": "test",
            }),
            raise_for_status=MagicMock(),
        )

        page = mgr.verify_fk_page()

        assert page == "file:///sdcard/kidpad/dashboard.html"
