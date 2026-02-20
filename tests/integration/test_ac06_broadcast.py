"""AC-06: Output socket full broadcast.

Tests that stop hook → control.sock → output.sock broadcast works end-to-end.
Layer: Integration/mock — fire_hook + control_server + broadcaster.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from tests.helpers import connect_subscriber


@pytest.mark.asyncio
async def test_T06_1_single_subscriber(broadcaster, test_config):
    """T-06-1: one subscriber receives broadcast."""
    reader, writer = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)  # let server register the subscriber

    payload = {
        "ts": int(time.time()),
        "session": "sess1",
        "turn": [{"type": "text", "text": "hello"}],
    }
    await broadcaster.broadcast(payload)

    data = await asyncio.wait_for(reader.readline(), timeout=2.0)
    msg = json.loads(data)
    assert msg["session"] == "sess1"
    assert msg["turn"][0]["text"] == "hello"

    writer.close()


@pytest.mark.asyncio
async def test_T06_2_multiple_subscribers(broadcaster, test_config):
    """T-06-2: 3 subscribers all receive identical broadcast within ≤100ms."""
    connections = []
    for _ in range(3):
        r, w = await connect_subscriber(test_config.output_sock)
        connections.append((r, w))

    await asyncio.sleep(0.1)  # let server register all subscribers
    assert broadcaster.subscriber_count == 3

    payload = {
        "ts": int(time.time()),
        "session": "s",
        "turn": [{"type": "text", "text": "hello all"}],
    }
    await broadcaster.broadcast(payload)

    received_times = []
    for reader, _ in connections:
        data = await asyncio.wait_for(reader.readline(), timeout=2.0)
        msg = json.loads(data)
        assert msg["turn"][0]["text"] == "hello all"
        received_times.append(time.time())

    spread = max(received_times) - min(received_times)
    assert spread <= 0.1

    for _, w in connections:
        w.close()


@pytest.mark.asyncio
async def test_T06_3_no_subscribers(broadcaster):
    """T-06-3: broadcasting with no subscribers does not crash."""
    payload = {"ts": int(time.time()), "session": "s", "turn": []}
    count = await broadcaster.broadcast(payload)
    assert count == 0


@pytest.mark.asyncio
async def test_T06_4_subscriber_disconnect(broadcaster, test_config):
    """T-06-4: disconnected subscriber does not affect remaining subscribers."""
    r1, w1 = await connect_subscriber(test_config.output_sock)
    r2, w2 = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)
    assert broadcaster.subscriber_count == 2

    # Close r1 properly — this signals disconnect to the server
    w1.close()
    await asyncio.sleep(0.2)  # let server detect disconnect

    # Broadcast should succeed and reach r2
    payload = {"ts": int(time.time()), "session": "s", "turn": [{"type": "text", "text": "hi"}]}
    await broadcaster.broadcast(payload)

    data = await asyncio.wait_for(r2.readline(), timeout=2.0)
    msg = json.loads(data)
    assert msg["turn"][0]["text"] == "hi"

    w2.close()
