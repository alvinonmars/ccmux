"""AC-09: Claude Code crash recovery with exponential backoff.

Layer: Integration/mock — uses LifecycleManager with fake pane and mocked I/O.

Tests:
  T-09-1: crash detected within poll_interval + backoff; log recorded; restart sent
  T-09-2: restart always sends CLAUDE_CONTINUE_CMD; on_restart callback fires
  T-09-3: restart intervals follow exponential backoff (lower and upper bounds)
  T-09-4: backoff saturates at backoff_cap after enough restarts
"""
import asyncio
import logging
import time
from unittest.mock import patch

from ccmux.injector import Message, inject_messages
from ccmux.lifecycle import LifecycleManager


class _FakePane:
    """Minimal fake pane for testing LifecycleManager."""

    def __init__(self, pid: str = "99999"):
        self._pid = pid
        self.sent_keys: list[str] = []

    @property
    def pid(self) -> str:
        return self._pid

    def send_keys(self, cmd: str, enter: bool = True, **kwargs) -> None:
        self.sent_keys.append(cmd)

    def cmd(self, *args) -> object:
        class R:
            stdout = ["❯"]
        return R()


async def test_T09_1_crash_detected_and_restarted(test_config, caplog):
    """T-09-1: crash detected within poll_interval + backoff window; log recorded."""
    pane = _FakePane()
    alive = [True]
    restart_event = asyncio.Event()

    def on_restart() -> None:
        restart_event.set()

    test_config.backoff_initial = 0.1
    test_config.backoff_cap = 10
    mgr = LifecycleManager(test_config, pane, on_restart=on_restart, poll_interval=0.2, startup_grace=0)
    # Replace _is_claude_running so the test controls crash timing
    mgr._is_claude_running = lambda: alive[0]

    mgr.start()
    await asyncio.sleep(0.4)  # let one poll cycle observe alive=True

    with caplog.at_level(logging.WARNING, logger="ccmux.lifecycle"):
        alive[0] = False  # simulate crash
        # Expected: next poll (≤0.2s) detects crash, then backoff (0.1s) → on_restart
        await asyncio.wait_for(restart_event.wait(), timeout=3.0)

    mgr.stop()

    assert restart_event.is_set()
    assert any("claude process died" in r.message for r in caplog.records)
    assert pane.sent_keys, "restart command should have been sent to pane"


async def test_T09_2_restart_uses_continue_cmd(test_config):
    """T-09-2: restart command includes --continue, proxy env vars, and CCMUX_CONTROL_SOCK.

    Note: pipe-pane re-mount requires a real tmux session; that behavior is verified
    end-to-end in AC-12 T-12-1 (daemon restart + attach to existing session + re-mount).
    """
    pane = _FakePane()
    alive = [True]
    on_restart_fired = asyncio.Event()

    def on_restart() -> None:
        on_restart_fired.set()

    test_config.backoff_initial = 0.1
    test_config.backoff_cap = 10
    test_config.claude_proxy = "http://127.0.0.1:8118"
    mgr = LifecycleManager(test_config, pane, on_restart=on_restart, poll_interval=0.2, startup_grace=0)
    mgr._is_claude_running = lambda: alive[0]

    mgr.start()
    await asyncio.sleep(0.4)

    alive[0] = False
    await asyncio.wait_for(on_restart_fired.wait(), timeout=3.0)
    mgr.stop()

    assert mgr.restart_count == 1
    assert pane.sent_keys, "restart command should have been sent to pane"

    restart_cmd = pane.sent_keys[0]
    # Must include --continue to preserve conversation history
    assert "--continue" in restart_cmd, f"missing --continue: {restart_cmd}"
    # Must include proxy env vars (matching daemon.py fresh-start command)
    assert "HTTP_PROXY=http://127.0.0.1:8118" in restart_cmd, (
        f"missing HTTP_PROXY: {restart_cmd}"
    )
    assert "HTTPS_PROXY=http://127.0.0.1:8118" in restart_cmd, (
        f"missing HTTPS_PROXY: {restart_cmd}"
    )
    # Must include CCMUX_CONTROL_SOCK so hook.py can find the daemon
    assert "CCMUX_CONTROL_SOCK=" in restart_cmd, (
        f"missing CCMUX_CONTROL_SOCK: {restart_cmd}"
    )

    # Pane is still usable for injection after restart
    inject_messages(pane, [Message(channel="test", content="hello after restart", ts=int(time.time()))])
    assert any("hello after restart" in k for k in pane.sent_keys)


async def test_T09_3_exponential_backoff(test_config):
    """T-09-3: restart intervals follow exponential backoff: initial, 2x, 4x, 8x."""
    pane = _FakePane()
    test_config.backoff_initial = 0.1
    test_config.backoff_cap = 10
    mgr = LifecycleManager(test_config, pane, poll_interval=0.1)

    intervals: list[float] = []
    last = time.monotonic()
    for _ in range(4):
        await mgr._restart()
        now = time.monotonic()
        intervals.append(now - last)
        last = now

    # Expected: ~0.1, ~0.2, ~0.4, ~0.8s
    expected = [0.1 * (2 ** i) for i in range(4)]
    for actual, exp in zip(intervals, expected):
        assert actual >= exp * 0.5, (
            f"backoff {actual:.3f}s too short; expected ≥ {exp * 0.5:.3f}s"
        )
        assert actual <= exp * 2.0 + 0.5, (
            f"backoff {actual:.3f}s too long; expected ≤ {exp * 2.0 + 0.5:.3f}s"
        )


async def test_is_claude_running_returns_false_on_total_failure(test_config):
    """P1-1: _is_claude_running() returns False when both pgrep and capture-pane fail.

    Uses a _BrokenPane where pid=None and cmd() raises. Verifies the fail-safe
    default (False) instead of the old True (which caused zombie risk).
    """
    class _BrokenPane:
        @property
        def pid(self):
            return None

        def cmd(self, *args):
            raise RuntimeError("pane destroyed")

        def send_keys(self, cmd: str, enter: bool = True, **kwargs):
            pass

    pane = _BrokenPane()
    mgr = LifecycleManager(test_config, pane, poll_interval=0.1)
    assert mgr._is_claude_running() is False, (
        "_is_claude_running should return False when both detection methods fail"
    )


async def test_T09_4_backoff_cap(test_config):
    """T-09-4: backoff is capped at backoff_cap; verified via mocked asyncio.sleep."""
    pane = _FakePane()
    test_config.backoff_initial = 1
    test_config.backoff_cap = 8
    mgr = LifecycleManager(test_config, pane, poll_interval=0.1)

    sleep_args: list[float] = []

    async def mock_sleep(seconds: float) -> None:
        sleep_args.append(seconds)

    with patch("ccmux.lifecycle.asyncio.sleep", side_effect=mock_sleep):
        for _ in range(10):
            await mgr._restart()

    # Every sleep value must be ≤ backoff_cap
    assert all(s <= test_config.backoff_cap for s in sleep_args), (
        f"some backoff exceeded cap: {sleep_args}"
    )
    # After enough restarts the cap must be reached (not just approached)
    assert test_config.backoff_cap in sleep_args, (
        f"backoff never reached cap {test_config.backoff_cap}: {sleep_args}"
    )
    # The last several values must all equal the cap
    assert sleep_args[-1] == test_config.backoff_cap
