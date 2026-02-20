"""AC-05: Ready Detection tests.

SP-02 verified detection mechanisms:
  - Primary: 3s stdout silence (StdoutMonitor watches stdout.log mtime)
  - Auxiliary: capture-pane last line contains ❯ (READY) / spinner char (GENERATING)
  - Permission: capture-pane keyword match (Yes/No/allow/y/n/Allow/yes/no)

Tests:
  T-05-1: StdoutMonitor fires on_ready + ReadyDetector returns READY (❯ present)
  T-05-2: StdoutMonitor fires via silence even with no ❯; get_state() returns UNKNOWN
  T-05-3: MOCK_PERMISSION_INTERVAL=1 → get_state() returns PERMISSION after first turn
  T-05-4: MOCK_SPINNER=15 (1.5s) → GENERATING during spin, READY after spin completes
  T-05-5: silence_timeout=2s config is respected; not fired at 1.8s, fired after 2.0s
  T-05-6: StdoutMonitor.reset() restarts the silence timer; on_ready fires again
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import libtmux
import pytest

from ccmux.detector import ReadyDetector, State, StdoutMonitor


async def test_t05_1_ready_via_stdout_silence_and_capture(
    make_mock_pane, tmp_path: Path
):
    """T-05-1: StdoutMonitor fires on_ready after silence; ReadyDetector returns READY.

    Verifies primary (silence) + auxiliary (❯ prompt) detection agree.
    """
    pane: libtmux.Pane = make_mock_pane()  # default: shows ❯ and waits for input

    stdout_log = tmp_path / "stdout.log"
    stdout_log.write_text("initial activity\n")

    ready_fired = asyncio.Event()
    monitor = StdoutMonitor(
        stdout_log=stdout_log,
        silence_timeout=1.0,
        on_ready=ready_fired.set,
        poll_interval=0.1,
    )
    monitor.start()

    await asyncio.wait_for(ready_fired.wait(), timeout=4.0)
    monitor.stop()

    detector = ReadyDetector(pane, silence_timeout=1.0)
    assert detector.get_state() == State.READY


async def test_t05_2_ready_fired_no_prompt(make_mock_pane, tmp_path: Path):
    """T-05-2: StdoutMonitor fires via silence even with no ❯; get_state() returns UNKNOWN.

    Fallback path: silence fires but auxiliary check finds no recognisable state.
    The daemon treats UNKNOWN the same as READY for injection purposes (only
    PERMISSION and GENERATING suppress injection).
    """
    pane: libtmux.Pane = make_mock_pane({"MOCK_PROMPT": ""})

    stdout_log = tmp_path / "stdout.log"
    stdout_log.write_text("some output\n")

    ready_fired = asyncio.Event()
    monitor = StdoutMonitor(
        stdout_log=stdout_log,
        silence_timeout=1.0,
        on_ready=ready_fired.set,
        poll_interval=0.1,
    )
    monitor.start()

    await asyncio.wait_for(ready_fired.wait(), timeout=4.0)
    monitor.stop()

    detector = ReadyDetector(pane, silence_timeout=1.0)
    assert detector.get_state() == State.UNKNOWN


def test_t05_3_permission_prompt_detected(bare_pane):
    """T-05-3: capture-pane keyword match detects PERMISSION state.

    Types the permission prompt text into a bare_pane (running cat) without
    pressing Enter so the text stays visible without ❯ at the end.  The
    ReadyDetector must identify this as State.PERMISSION.

    Uses bare_pane instead of mock_pane because mock_pane's sys.stdin.readline()
    for the permission-resolution wait returns immediately in a PTY context (EOF
    behaviour), completing the turn before the test can sample the pane state.
    """
    # Type permission text WITHOUT Enter — stays on-screen without ❯ appearing
    bare_pane.send_keys("Allow this action? Yes/No ", enter=False)
    time.sleep(0.3)

    detector = ReadyDetector(bare_pane, silence_timeout=1.0)
    assert detector.get_state() == State.PERMISSION


def test_t05_4_generating_state_during_spinner(make_mock_pane):
    """T-05-4: MOCK_SPINNER=15 (1.5s of spinner chars) → GENERATING during spin, READY after.

    MOCK_SPINNER=15: emits 15 × SPINNER_SEQ (each containing ✻) at 0.1s intervals = 1.5s.
    At 0.5s: spinner chars visible in last pane line → GENERATING.
    At 2.2s total: spinner done, reply shown, ❯ prompt visible → READY.
    """
    pane: libtmux.Pane = make_mock_pane({"MOCK_SPINNER": "15"})

    # Trigger turn 1; mock_pane sleeps 0.1s (MOCK_DELAY) then emits 15 spinner sequences
    pane.send_keys("go", enter=True)

    # At 0.5s: spinner in progress (~3-4 ✻ chars visible)
    time.sleep(0.5)
    detector = ReadyDetector(pane, silence_timeout=1.0)
    assert detector.get_state() == State.GENERATING

    # At 2.2s total: delay(0.1) + spinner(1.5) + reply + prompt all done
    time.sleep(1.7)
    assert detector.get_state() == State.READY


async def test_t05_5_silence_timeout_config_respected(tmp_path: Path):
    """T-05-5: StdoutMonitor respects silence_timeout=2.0s.

    Verifies: not fired at 1.8s, fired exactly once after 2.0s.
    """
    stdout_log = tmp_path / "stdout.log"
    stdout_log.write_text("activity\n")

    fired_at: list[float] = []
    start = time.monotonic()

    def on_ready() -> None:
        fired_at.append(time.monotonic() - start)

    monitor = StdoutMonitor(
        stdout_log=stdout_log,
        silence_timeout=2.0,
        on_ready=on_ready,
        poll_interval=0.1,
    )
    monitor.start()

    # 1.8s: must not have fired yet
    await asyncio.sleep(1.8)
    assert len(fired_at) == 0, f"on_ready fired early at {fired_at[0]:.2f}s"

    # Wait past silence_timeout + poll buffer (2.0 + 0.2 = 2.2s total from start)
    await asyncio.sleep(0.7)
    monitor.stop()

    assert len(fired_at) == 1, f"on_ready should fire exactly once; fired={fired_at}"
    assert fired_at[0] >= 2.0, f"on_ready fired too early: {fired_at[0]:.3f}s"


async def test_t05_6_reset_restarts_silence_timer(tmp_path: Path):
    """T-05-6: StdoutMonitor.reset() restarts the silence timer; on_ready fires again.

    Bug fixed in Iter-2 closure: reset() now also clears _last_mtime so the next
    poll treats the current mtime as new activity and restarts the silence countdown.
    Without the fix, if stdout.log is not written between turns, the silence timer
    would never restart and on_ready would not fire after the first turn.
    """
    stdout_log = tmp_path / "stdout.log"
    stdout_log.write_text("initial activity\n")

    fire_count = 0

    def on_ready() -> None:
        nonlocal fire_count
        fire_count += 1

    monitor = StdoutMonitor(
        stdout_log=stdout_log,
        silence_timeout=1.0,
        on_ready=on_ready,
        poll_interval=0.1,
    )
    monitor.start()

    # First fire: silence detected after 1.0s
    await asyncio.sleep(1.5)
    assert fire_count == 1, f"Expected first on_ready fire, got {fire_count}"

    # Call reset() — must restart the silence timer even without new file writes
    monitor.reset()

    # Second fire: reset() cleared _last_mtime → next poll sees mtime as new
    # activity → starts silence countdown → fires again after silence_timeout
    await asyncio.sleep(1.5)
    monitor.stop()

    assert fire_count == 2, f"Expected second on_ready fire after reset(), got {fire_count}"
