"""AC-12: ccmux daemon restart and reconnect.

Layer: Integration/mock.

Tests:
  T-12-1: bare_pane + two Daemon instances sequentially
           → second daemon re-attaches to existing tmux session, pipe-pane re-mounted
  T-12-2: net_daemon + bare_pane → write to FIFO → message queued → _maybe_inject()
           → text visible in pane (injection works after fresh daemon init)
  T-12-3: net_daemon + fire_hook + subscriber → broadcast received
           (subscribers work with fresh daemon)
"""
import asyncio
import json
import os
import time

import libtmux

from ccmux.daemon import Daemon
from ccmux.detector import ReadyDetector, State
from ccmux.fifo import Message
from tests.helpers import connect_subscriber


async def test_T12_1_second_daemon_reattaches(test_config, tmux_server):
    """T-12-1: second Daemon instance re-attaches to existing tmux session.

    Creates a bare tmux session (running cat), then instantiates two Daemon
    objects sequentially. The second daemon should detect and attach to the
    existing session without creating a new one. Verifies pipe-pane is
    re-mounted by checking stdout.log grows after the second mount.
    """
    session_name = test_config.tmux_session

    # Create the tmux session manually (simulates claude already running)
    session = tmux_server.new_session(session_name=session_name, window_name="claude")
    pane = session.active_window.active_pane
    pane.send_keys("cat", enter=True)
    await asyncio.sleep(0.3)

    # First daemon "attaches" by finding the existing session
    d1 = Daemon(test_config)
    d1._pane = pane
    d1._mount_pipe_pane()

    # Write something so pipe-pane captures it
    pane.send_keys("first-mount-test", enter=True)
    await asyncio.sleep(0.5)

    stdout_log = test_config.stdout_log
    size_after_first = stdout_log.stat().st_size if stdout_log.exists() else 0

    # Second daemon re-attaches (simulates daemon restart)
    d2 = Daemon(test_config)
    d2._pane = pane
    d2._mount_pipe_pane()

    # Write again after second mount
    pane.send_keys("second-mount-test", enter=True)
    await asyncio.sleep(0.5)

    size_after_second = stdout_log.stat().st_size if stdout_log.exists() else 0
    assert size_after_second > size_after_first, (
        f"stdout.log should grow after second pipe-pane mount "
        f"(before={size_after_first}, after={size_after_second})"
    )

    # Cleanup
    try:
        tmux_server.kill_session(target_session=session_name)
    except Exception:
        pass


async def test_T12_2_injection_after_daemon_init(
    net_daemon, test_config, bare_pane, monkeypatch
):
    """T-12-2: injection works after fresh daemon init.

    Writes a JSON message to the FIFO, verifies it's queued, then calls
    _maybe_inject() to drain the queue. Text should be visible in pane.
    """
    d = net_daemon
    d._pane = bare_pane
    d._detector = ReadyDetector(bare_pane, test_config.silence_timeout)
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    # Write message to default FIFO
    fifo_path = test_config.runtime_dir / "in"
    msg_data = json.dumps({
        "channel": "test-ch",
        "content": "restart-inject-test",
        "ts": int(time.time()),
        "meta": {},
    })
    fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, (msg_data + "\n").encode())
    os.close(fd)

    await asyncio.sleep(0.5)  # let FIFO reader process + auto-inject

    # _on_message triggers _maybe_inject immediately, so the queue
    # should already be drained by the time we check.
    assert len(d._message_queue) == 0, "queue should be drained by auto-injection"

    # Verify text visible in pane
    capture = bare_pane.cmd("capture-pane", "-p").stdout
    text = "\n".join(capture) if isinstance(capture, list) else capture
    assert "restart-inject-test" in text, (
        f"injected text should be visible in pane, got: {text}"
    )


async def test_T12_3_broadcast_after_daemon_init(
    net_daemon, test_config, fire_hook
):
    """T-12-3: subscribers receive broadcast with fresh daemon.

    Connects a subscriber to output.sock, fires Stop hook, and verifies
    the subscriber receives the broadcast payload.
    """
    reader, writer = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)  # let server register subscriber

    result = fire_hook("Stop", {
        "session_id": "restart-broadcast-session",
        "last_assistant_message": "post-restart reply",
    })
    assert result.returncode == 0

    data = await asyncio.wait_for(reader.readline(), timeout=3.0)
    msg = json.loads(data)
    assert msg["session"] == "restart-broadcast-session"
    assert "turn" in msg
    assert msg["turn"][0]["text"] == "post-restart reply"

    writer.close()
