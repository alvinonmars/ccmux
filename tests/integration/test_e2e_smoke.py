"""E2E mock smoke test — full chain without real Claude.

Exercises the complete message lifecycle:
  1. Connect subscriber to output.sock
  2. Write JSON message to FIFO (in)
  3. Verify daemon queued it
  4. Call _on_silence_ready() (simulates StdoutMonitor timeout)
  5. Verify queue drained, text in pane (bare_pane)
  6. fire_hook("Stop") with transcript
  7. Verify subscriber received broadcast with correct session + turn

Fixtures: net_daemon + bare_pane + fire_hook + connect_subscriber
"""
import asyncio
import json
import os
import time

from ccmux.detector import ReadyDetector
from tests.helpers import connect_subscriber


async def test_e2e_full_chain(
    net_daemon, test_config, bare_pane, fire_hook, monkeypatch
):
    """Full chain: FIFO → queue → silence ready → inject → hook → broadcast."""
    d = net_daemon
    d._pane = bare_pane
    d._detector = ReadyDetector(bare_pane, test_config.silence_timeout)
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    # 1. Connect subscriber to output.sock
    reader, writer = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)  # let server register subscriber

    # 2. Write JSON message to FIFO
    fifo_path = test_config.runtime_dir / "in"
    msg_data = json.dumps({
        "channel": "telegram",
        "content": "e2e smoke test message",
        "ts": int(time.time()),
        "meta": {},
    })
    fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, (msg_data + "\n").encode())
    os.close(fd)

    await asyncio.sleep(0.5)  # let FIFO reader process + auto-inject

    # 3. _on_message triggers _maybe_inject immediately, so the queue
    #    should already be drained and the text injected into the pane.
    assert len(d._message_queue) == 0, "queue should be drained by auto-injection"

    capture = bare_pane.cmd("capture-pane", "-p").stdout
    text = "\n".join(capture) if isinstance(capture, list) else capture
    assert "e2e smoke test message" in text, (
        f"injected text should be visible in pane, got: {text}"
    )

    # 6. fire_hook("Stop") with transcript data
    result = fire_hook("Stop", {
        "session_id": "e2e-session",
        "last_assistant_message": "e2e reply from claude",
    })
    assert result.returncode == 0

    # 7. Verify subscriber received broadcast
    data = await asyncio.wait_for(reader.readline(), timeout=3.0)
    msg = json.loads(data)
    assert msg["session"] == "e2e-session"
    assert "turn" in msg
    assert msg["turn"][0]["text"] == "e2e reply from claude"
    assert "ts" in msg

    writer.close()
