"""Unit tests for scripts/daily_butler.py — FIFO notify retry logic."""
import errno
import os
from unittest import mock

import pytest

from scripts.daily_butler import notify_ccmux


@pytest.fixture(autouse=True)
def _tmp_fifo(tmp_path, monkeypatch):
    """Point FIFO_PATH to a temp dir and pre-create the FIFO."""
    fifo = tmp_path / "in.butler"
    os.mkfifo(str(fifo))
    monkeypatch.setattr("scripts.daily_butler.FIFO_PATH", fifo)
    return fifo


class TestNotifyCcmuxRetry:
    """notify_ccmux retries on ENXIO (no reader) then succeeds."""

    def test_succeeds_on_first_attempt(self):
        """Happy path: reader present, no retries needed."""
        with mock.patch("scripts.daily_butler.os.open", return_value=99) as m_open, \
             mock.patch("scripts.daily_butler.os.write") as m_write, \
             mock.patch("scripts.daily_butler.os.close"):
            assert notify_ccmux("hello") is True
            m_open.assert_called_once()
            m_write.assert_called_once()

    def test_retries_on_enxio_then_succeeds(self):
        """Simulates boot race: first 2 attempts fail, third succeeds."""
        enxio = OSError(errno.ENXIO, "No such device or address")
        with mock.patch("scripts.daily_butler.os.open", side_effect=[enxio, enxio, 99]), \
             mock.patch("scripts.daily_butler.os.write") as m_write, \
             mock.patch("scripts.daily_butler.os.close"), \
             mock.patch("scripts.daily_butler.time.sleep") as m_sleep:
            assert notify_ccmux("hello", max_retries=5, retry_delay=0.1) is True
            assert m_sleep.call_count == 2
            m_write.assert_called_once()

    def test_gives_up_after_max_retries(self):
        """All retries exhausted — returns False."""
        enxio = OSError(errno.ENXIO, "No such device or address")
        with mock.patch("scripts.daily_butler.os.open", side_effect=enxio), \
             mock.patch("scripts.daily_butler.time.sleep") as m_sleep:
            assert notify_ccmux("hello", max_retries=3, retry_delay=0.1) is False
            assert m_sleep.call_count == 2  # retries between attempts 1-2 and 2-3

    def test_payload_too_large_returns_false(self):
        """Payload exceeding PIPE_BUF is rejected without writing."""
        assert notify_ccmux("x" * 5000) is False
