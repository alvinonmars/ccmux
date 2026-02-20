"""AC-01: Input FIFO message acceptance.

Tests that the daemon correctly reads messages from named FIFOs.
Layer: Integration/mock — daemon only (net_daemon fixture).
"""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest



@pytest.mark.asyncio
async def test_T01_1_plain_text(net_daemon, test_config):
    """T-01-1: plain text written to default FIFO is received by daemon."""
    fifo = test_config.runtime_dir / "in"
    msg = "hello from test"

    fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, (msg + "\n").encode())
    os.close(fd)

    await asyncio.sleep(0.3)
    assert len(net_daemon.message_queue) == 1
    assert net_daemon.message_queue[0].content == msg


@pytest.mark.asyncio
async def test_T01_2_json_format(net_daemon, test_config):
    """T-01-2: JSON format is parsed; channel and content are correct."""
    fifo = test_config.runtime_dir / "in"
    payload = json.dumps({
        "channel": "telegram",
        "content": "json message",
        "ts": 1700000000,
        "meta": {},
    })

    fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, (payload + "\n").encode())
    os.close(fd)

    await asyncio.sleep(0.3)
    assert len(net_daemon.message_queue) == 1
    msg = net_daemon.message_queue[0]
    assert msg.channel == "telegram"
    assert msg.content == "json message"
    assert msg.ts == 1700000000


@pytest.mark.asyncio
async def test_T01_3_invalid_json_treated_as_plain_text(net_daemon, test_config):
    """T-01-3: invalid JSON is treated as plain text; daemon does not crash."""
    fifo = test_config.runtime_dir / "in"
    bad_json = "{bad json"

    fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, (bad_json + "\n").encode())
    os.close(fd)

    await asyncio.sleep(0.3)
    assert len(net_daemon.message_queue) == 1
    assert net_daemon.message_queue[0].content == "{bad json"


@pytest.mark.asyncio
async def test_T01_4_concurrent_writers(net_daemon, test_config):
    """T-01-4: 5 concurrent writers; all messages received, no interleaving."""
    fifo = test_config.runtime_dir / "in"
    n = 5
    messages = [f"message-{i}" for i in range(n)]

    import threading

    def write_msg(m: str):
        fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, (m + "\n").encode())
        os.close(fd)

    threads = [threading.Thread(target=write_msg, args=(m,)) for m in messages]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    await asyncio.sleep(0.5)
    received = {msg.content for msg in net_daemon.message_queue}
    assert received == set(messages)
