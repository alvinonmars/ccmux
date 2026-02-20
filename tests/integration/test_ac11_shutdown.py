"""AC-11: Graceful shutdown.

Tests that stopping the daemon cleans up sockets.
Layer: Integration/mock — net_daemon fixture.
"""
import asyncio
import os
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_T11_1_shutdown_removes_sockets(test_config):
    """T-11-1: after stop(), socket files are cleaned up, exit is clean."""
    from ccmux.pubsub import ControlServer, OutputBroadcaster

    broadcaster = OutputBroadcaster(test_config.output_sock)
    await broadcaster.start()
    assert test_config.output_sock.exists()

    control = ControlServer(
        test_config.control_sock,
        on_broadcast=lambda m: None,
        on_event=lambda m: None,
    )
    await control.start()
    assert test_config.control_sock.exists()

    # Stop both
    await broadcaster.stop()
    await control.stop()

    assert not test_config.output_sock.exists()
    assert not test_config.control_sock.exists()


@pytest.mark.asyncio
async def test_T11_2_subscriber_closed_on_shutdown(test_config):
    """T-11-2: active subscriber connection is closed when broadcaster stops."""
    from ccmux.pubsub import OutputBroadcaster
    from tests.helpers import connect_subscriber

    broadcaster = OutputBroadcaster(test_config.output_sock)
    await broadcaster.start()

    reader, writer = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)
    assert broadcaster.subscriber_count == 1

    await broadcaster.stop()

    # Reader should see EOF when server closes the connection
    data = await asyncio.wait_for(reader.read(100), timeout=2.0)
    assert data == b""  # EOF
