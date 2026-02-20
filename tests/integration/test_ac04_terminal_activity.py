"""AC-04: Terminal Activity Detection.

Tests that injection is gated on #{client_activity}: suppressed when a human
has recently used the terminal, resumes when the terminal has been idle.

Layer: Integration/mock — bare_pane fixture + monkeypatching.

Design note: #{client_activity} tracks real tmux client keyboard events only.
Daemon injections via send-keys do NOT update it (unlike pipe-pane -I which
would capture all PTY stdin and create a circular dependency).
"""
import dataclasses
import time

import pytest

from ccmux.fifo import Message


def _msg(content: str = "hello world") -> Message:
    return Message(channel="test", content=content, ts=int(time.time()))


# ---------------------------------------------------------------------------
# Design premise tests — verify the tmux mechanism itself before logic tests
# ---------------------------------------------------------------------------

def test_T04_0a_client_activity_readable(bare_pane, test_config):
    """_get_client_activity_ts() returns a parseable int from a real tmux session.

    Returns 0 when no client is attached (correct: no human activity = idle = injection allowed).
    Must never raise.
    """
    from ccmux.daemon import Daemon

    d = Daemon(test_config)
    d._pane = bare_pane
    ts = d._get_client_activity_ts()

    assert isinstance(ts, int)
    assert ts >= 0


def test_T04_0a2_client_activity_returns_zero_on_error(test_config, monkeypatch):
    """_get_client_activity_ts() returns 0 (not raises) when tmux cmd fails.

    Fail-safe: on error, ts=0 → _is_terminal_active()=False → injection allowed.
    This is the correct safe default for automated/headless environments.
    """
    from ccmux.daemon import Daemon
    import libtmux

    d = Daemon(test_config)

    # Simulate a pane whose cmd() raises (e.g. session died)
    class _BadPane:
        def cmd(self, *args):
            raise RuntimeError("tmux session gone")

    d._pane = _BadPane()  # type: ignore[assignment]
    assert d._get_client_activity_ts() == 0
    assert d._is_terminal_active() is False  # safe default: allow injection


def test_T04_0b_send_keys_does_not_update_client_activity(bare_pane, test_config):
    """send-keys injection does NOT advance #{client_activity}.

    This is the key design property that makes #{client_activity} correct:
    daemon injections via send-keys are server-side API calls, not client
    keyboard events, so they leave client_activity unchanged.

    If this test fails, the circular dependency from pipe-pane -I would
    be reproduced: each injection would advance the timestamp and suppress
    the next injection.

    Headless note: in CI with no tmux client attached, #{client_activity}=0
    throughout (before=0, after=0). The test still has value as a regression
    guard: if tmux ever began updating client_activity on send-keys, the
    timestamp would become non-zero and the assertion would catch it.
    The capture-pane assertion below verifies send-keys actually executed,
    making this a genuine test rather than a trivial 0==0 check.
    """
    from ccmux.daemon import Daemon

    d = Daemon(test_config)
    d._pane = bare_pane

    before = d._get_client_activity_ts()
    # Inject immediately — minimise the window between before/after reads
    # to reduce interference from interactive user key presses
    bare_pane.send_keys("sentinel-phrase", enter=True)
    after = d._get_client_activity_ts()  # read IMMEDIATELY after send-keys (~1ms window)

    # Verify send-keys actually executed (proves the test is not trivially vacuous)
    time.sleep(0.3)
    content = "\n".join(bare_pane.cmd("capture-pane", "-p").stdout)
    assert "sentinel-phrase" in content, (
        "send-keys did not execute — test is invalid if this assertion fails"
    )

    assert after == before, (
        f"#{{client_activity}} advanced from {before} to {after} after send-keys. "
        "Daemon injections must not count as human activity. "
        "If flaky in interactive environments, user key presses in the ~1ms window "
        "between send-keys and the after-read may cause a false failure. "
        "See Iter-1 empirical verification for definitive proof."
    )


# ---------------------------------------------------------------------------
# Logic tests — verify injection gating behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_T04_1_suppress_when_active(test_config, bare_pane, monkeypatch):
    """T-04-1: injection suppressed when terminal recently active.

    Uses bare_pane (pane IS set) to prove it is the terminal-activity check
    suppressing injection, not the pane=None early-return.
    """
    from ccmux.daemon import Daemon

    d = Daemon(test_config)
    d._pane = bare_pane  # pane is set — rules out pane=None as the cause
    d._message_queue = [_msg()]

    # Simulate: human pressed a key just now
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: int(time.time()))

    await d._maybe_inject()

    # Messages must remain — pane exists, so only the terminal-activity gate
    # could have caused suppression
    assert len(d._message_queue) == 1


@pytest.mark.asyncio
async def test_T04_4_active_then_idle_sequence(test_config, bare_pane, monkeypatch):
    """T-04-4: messages suppressed while active, then injected once idle.

    Verifies the full active→suppress→idle→inject sequence in a single test.
    """
    from ccmux.daemon import Daemon

    d = Daemon(test_config)
    d._pane = bare_pane
    d._message_queue = [_msg("sequence-marker")]

    # Phase 1: terminal active → suppress
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: int(time.time()))
    await d._maybe_inject()
    assert len(d._message_queue) == 1, "message should stay while terminal is active"

    # Phase 2: terminal idle → inject
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)
    await d._maybe_inject()
    assert len(d._message_queue) == 0, "message should drain once terminal is idle"

    time.sleep(0.2)
    content = "\n".join(bare_pane.cmd("capture-pane", "-p").stdout)
    assert "sequence-marker" in content


@pytest.mark.asyncio
async def test_T04_2_inject_when_idle(test_config, bare_pane, monkeypatch):
    """T-04-2: injection happens when terminal has been idle."""
    from ccmux.daemon import Daemon

    d = Daemon(test_config)
    d._pane = bare_pane
    d._message_queue = [_msg("inject-marker")]

    # Simulate: no human keyboard activity ever (ts=0)
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    await d._maybe_inject()

    # Queue must be drained
    assert len(d._message_queue) == 0

    # Injected text must appear in the pane
    time.sleep(0.2)
    content = "\n".join(bare_pane.cmd("capture-pane", "-p").stdout)
    assert "inject-marker" in content


def test_T04_3_threshold_configurable(test_config, monkeypatch):
    """T-04-3: idle_threshold boundary is respected."""
    from ccmux.daemon import Daemon

    cfg5 = dataclasses.replace(test_config, idle_threshold=5)
    d = Daemon(cfg5)

    # 4 seconds ago: within 5s threshold → active
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: int(time.time()) - 4)
    assert d._is_terminal_active() is True

    # 6 seconds ago: beyond 5s threshold → idle
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: int(time.time()) - 6)
    assert d._is_terminal_active() is False
