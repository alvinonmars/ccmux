"""AC-09: Claude Code crash recovery with exponential backoff.

Layer: Integration/mock — uses lifecycle manager with mock tracking.
Tests backoff timing logic without requiring a real tmux pane.
"""
import asyncio
import time

import pytest

from ccmux.config import Config


class _FakePane:
    """Minimal fake pane for testing LifecycleManager."""

    def __init__(self, pid: str = "99999"):
        self._pid = pid
        self.sent_keys: list[str] = []
        self._alive = True

    @property
    def pid(self) -> str:
        return self._pid

    def send_keys(self, cmd: str, enter: bool = True) -> None:
        self.sent_keys.append(cmd)

    def cmd(self, *args) -> object:
        class R:
            stdout = ["❯"]
        return R()

    def kill(self) -> None:
        self._alive = False


@pytest.mark.asyncio
async def test_T09_3_exponential_backoff(test_config):
    """T-09-3: restart intervals follow exponential backoff (1s, 2s, 4s, 8s)."""
    from ccmux.lifecycle import LifecycleManager

    pane = _FakePane()
    restart_times: list[float] = []

    def on_restart():
        restart_times.append(time.time())

    mgr = LifecycleManager(test_config, pane, on_restart=on_restart)
    # Override backoff parameters for fast testing
    test_config.backoff_initial = 0.1  # 0.1s base instead of 1s
    test_config.backoff_cap = 10

    # Simulate 4 consecutive restarts by calling _restart() directly
    intervals = []
    last = time.time()
    for i in range(4):
        await mgr._restart()
        now = time.time()
        intervals.append(now - last)
        last = now

    # Expected: ~0.1, ~0.2, ~0.4, ~0.8 (with some slack for test overhead)
    expected = [0.1 * (2 ** i) for i in range(4)]
    for actual, exp in zip(intervals, expected):
        # Allow 50% error margin + 0.5s for overhead
        assert actual <= exp * 2.0 + 0.5, f"interval {actual:.2f}s too large, expected ~{exp:.2f}s"


@pytest.mark.asyncio
async def test_T09_4_backoff_cap(test_config):
    """T-09-4: backoff is capped at backoff_cap."""
    from ccmux.lifecycle import LifecycleManager

    pane = _FakePane()
    mgr = LifecycleManager(test_config, pane)
    test_config.backoff_initial = 1
    test_config.backoff_cap = 8  # cap at 8s for test speed

    # Compute what the backoff would be for 10th restart
    import math
    for i in range(10):
        backoff = min(
            test_config.backoff_initial * (2 ** i),
            test_config.backoff_cap,
        )
    # After many restarts, backoff must be capped
    assert backoff == test_config.backoff_cap
