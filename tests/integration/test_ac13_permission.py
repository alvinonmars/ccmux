"""AC-13: PermissionRequest routing.

Layer: Integration/mock.

Tests:
  T-13-1: PermissionRequest hook fires (fire_hook + net_daemon)
           → daemon's _on_event sets _permission_detected → injection suppressed
           → permission_request alert broadcast to output.sock subscribers
  T-13-2: capture-pane fallback (bare_pane with typed permission text)
           → ReadyDetector.get_state() returns PERMISSION → injection suppressed
  T-13-3: injection resumes after permission resolved
           → fire_hook("Stop") → _on_broadcast clears _permission_detected → inject
  T-13-4: capture-pane recovery clears stale permission flag
           → _permission_detected=True but capture-pane shows no permission → flag cleared

Note: Tests use fire_hook + bare_pane instead of mock_pane with MOCK_HOOK_SCRIPT
because mock_pane's sys.stdin.readline() for permission-resolution wait returns
immediately in PTY context (EOF behaviour), completing the turn before the test
can sample the permission state.
"""
import asyncio
import json
import time

from ccmux.daemon import Daemon
from ccmux.detector import ReadyDetector, State
from ccmux.fifo import Message


async def test_T13_1_permission_hook_suppresses(
    net_daemon, test_config, fire_hook, monkeypatch
):
    """T-13-1: PermissionRequest hook fires via hook.py subprocess → injection suppressed.

    Fires hook.py directly with PermissionRequest JSON (via fire_hook fixture).
    hook.py sends to control.sock → daemon's _on_event sets _permission_detected = True.
    Also verifies that a permission_request alert is broadcast to output.sock subscribers.
    """
    d = net_daemon
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    # Connect a subscriber to output.sock before firing the hook.
    reader, writer = await asyncio.open_unix_connection(str(test_config.output_sock))

    # Fire PermissionRequest event through the real hook.py subprocess
    result = fire_hook("PermissionRequest", {"session_id": "perm-session"})
    assert result.returncode == 0

    await asyncio.sleep(0.3)  # let event propagate through control.sock → _on_event

    # _on_event should have set _permission_detected
    assert d._permission_detected is True, (
        "_permission_detected should be set by PermissionRequest hook"
    )

    # Subscriber should have received a permission_request alert.
    data = await asyncio.wait_for(reader.readline(), timeout=2.0)
    alert = json.loads(data)
    assert alert["type"] == "permission_request"
    assert alert["session"] == "perm-session"
    assert "ts" in alert

    # Verify injection is suppressed (pane is None → early return).
    d._message_queue.append(
        Message(channel="test", content="blocked", ts=int(time.time()))
    )
    await d._maybe_inject()

    assert len(d._message_queue) == 1, (
        "message should remain in queue while permission prompt is active"
    )

    writer.close()


async def test_T13_2_capture_pane_fallback(
    test_config, bare_pane, monkeypatch
):
    """T-13-2: capture-pane detects permission prompt when hook has not fired.

    Types the permission prompt text into a bare_pane (running cat) without
    pressing Enter so it stays visible without ❯ at the end.  The daemon
    detects the prompt via ReadyDetector.get_state() (capture-pane fallback).
    """
    # Type permission text WITHOUT Enter — stays on screen, no ❯ at end
    bare_pane.send_keys("Allow this action? Yes/No ", enter=False)
    time.sleep(0.3)  # let tmux render the text

    d = Daemon(test_config)
    d._pane = bare_pane
    d._detector = ReadyDetector(bare_pane, test_config.silence_timeout)
    d._permission_detected = False  # hook has NOT fired
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    # Confirm capture-pane sees the permission prompt
    assert d._detector.get_state() == State.PERMISSION, (
        "ReadyDetector should detect PERMISSION from capture-pane"
    )

    # Try inject — should be suppressed via capture-pane path
    d._message_queue.append(
        Message(channel="test", content="blocked", ts=int(time.time()))
    )
    await d._maybe_inject()

    assert len(d._message_queue) == 1, (
        "message should remain in queue when permission detected via capture-pane"
    )
    # Flag is now set by the capture-pane path (not by a hook)
    assert d._permission_detected is True


async def test_T13_3_injection_resumes_after_resolution(
    net_daemon, test_config, fire_hook, bare_pane, monkeypatch
):
    """T-13-3: injection resumes after permission prompt is resolved.

    Fires PermissionRequest via hook.py → _permission_detected = True.
    Then fires Stop via hook.py → _on_broadcast clears _permission_detected.
    With no active permission state, _maybe_inject() drains the queue.
    """
    d = net_daemon
    d._pane = bare_pane  # bare_pane for state detection (cat, State.UNKNOWN → allows inject)
    d._detector = ReadyDetector(bare_pane, test_config.silence_timeout)
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    # Phase 1: fire PermissionRequest → flag set
    result = fire_hook("PermissionRequest", {"session_id": "resolve-session"})
    assert result.returncode == 0
    await asyncio.sleep(0.2)
    assert d._permission_detected is True, (
        "_permission_detected should be set after PermissionRequest hook"
    )

    # Phase 2: fire Stop → _on_broadcast clears _permission_detected
    result = fire_hook("Stop", {
        "session_id": "resolve-session",
        "last_assistant_message": "done",
    })
    assert result.returncode == 0
    await asyncio.sleep(0.3)

    # Stop hook → _on_broadcast → _permission_detected cleared
    assert d._permission_detected is False, (
        "_permission_detected should be cleared when Stop hook fires"
    )

    # Phase 3: inject should proceed — flag is clear, bare_pane is UNKNOWN (not blocking)
    d._message_queue.append(
        Message(channel="test", content="after-resolution", ts=int(time.time()))
    )
    await d._maybe_inject()

    assert len(d._message_queue) == 0, (
        "message should be injected after permission resolved"
    )


async def test_T13_4_capture_pane_recovery(
    test_config, bare_pane, monkeypatch
):
    """T-13-4: capture-pane recovery clears stale permission flag.

    Simulates the scenario where a PermissionRequest hook set the flag,
    but the user resolved the prompt without a Stop hook firing (e.g.
    answered No, or hook failed).  On the next _maybe_inject() call,
    capture-pane no longer shows permission text → flag is cleared and
    queued messages are injected.
    """
    d = Daemon(test_config)
    d._pane = bare_pane
    d._detector = ReadyDetector(bare_pane, test_config.silence_timeout)
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    # Simulate: hook set the flag earlier, but prompt is already resolved.
    # bare_pane runs cat — capture-pane shows no permission keywords.
    d._permission_detected = True

    d._message_queue.append(
        Message(channel="test", content="after-recovery", ts=int(time.time()))
    )
    await d._maybe_inject()

    # Flag should be cleared by capture-pane recovery.
    assert d._permission_detected is False, (
        "_permission_detected should be cleared when capture-pane shows no permission"
    )
    # Messages should have been injected.
    assert len(d._message_queue) == 0, (
        "messages should be injected after capture-pane recovery clears permission flag"
    )
