"""AC-02: Filesystem dynamic FIFO registration via inotify.

Layer: Integration/mock — daemon only.

Tests:
  T-02-1: creating in.test after watcher starts → message received
  T-02-2: deleting in.temp → daemon does not crash
"""
import asyncio
import os
from pathlib import Path

from ccmux.fifo import FifoManager
from ccmux.watcher import DirectoryWatcher
from ccmux.injector import Message


async def test_T02_1_dynamic_input_fifo_registration(test_config):
    """T-02-1: creating in.test after watcher starts → message received."""
    received: list[Message] = []

    mgr = FifoManager(callback=received.append)
    loop = asyncio.get_event_loop()
    mgr.start(loop)

    watcher = DirectoryWatcher(
        test_config.runtime_dir,
        loop,
        on_input_add=mgr.add,
        on_input_remove=mgr.remove,
    )
    watcher.start()

    # Create new FIFO after watcher is running
    fifo = test_config.runtime_dir / "in.test"
    os.mkfifo(str(fifo))
    await asyncio.sleep(0.5)  # inotify event propagation

    # Write to the new FIFO
    fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, b"dynamic message\n")
    os.close(fd)
    await asyncio.sleep(0.3)

    assert len(received) == 1
    assert received[0].content == "dynamic message"
    assert received[0].channel == "test"

    watcher.stop()
    mgr.stop_all()


async def test_T02_2_fifo_removal_does_not_crash(test_config):
    """T-02-2: removing a FIFO after registration → daemon does not crash."""
    removed: list[Path] = []

    mgr = FifoManager(callback=lambda m: None)
    loop = asyncio.get_event_loop()
    mgr.start(loop)

    watcher = DirectoryWatcher(
        test_config.runtime_dir,
        loop,
        on_input_add=mgr.add,
        on_input_remove=lambda p: (removed.append(p), mgr.remove(p)),
    )
    watcher.start()

    fifo = test_config.runtime_dir / "in.temp"
    os.mkfifo(str(fifo))
    await asyncio.sleep(0.5)

    os.unlink(str(fifo))
    await asyncio.sleep(0.5)

    assert any(p.name == "in.temp" for p in removed)

    watcher.stop()
    mgr.stop_all()
