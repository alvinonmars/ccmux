"""Async FIFO reader using O_NONBLOCK + asyncio add_reader.

SP-03 verified:
- Open with O_RDWR to prevent EOF when all writers close
- Use os.read() (not readline) to avoid deadlock
- Short messages (< PIPE_BUF = 4096 B) are atomically written
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Callable

from ccmux.injector import Message


def parse_message(line: str, fifo_name: str) -> Message:
    """Parse a FIFO line into a Message.

    Supports two formats:
    1. JSON: {"channel": "...", "content": "...", "ts": 123, "meta": {}}
    2. Plain text: anything else; channel inferred from FIFO name (in.<channel>)
    """
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            channel = data.get("channel") or _channel_from_name(fifo_name)
            content = data.get("content", stripped)
            ts = int(data.get("ts") or time.time())
            # Extract intent classification metadata if present
            meta = None
            if "intent" in data:
                meta = {
                    "intent": data["intent"],
                    "intent_meta": data.get("intent_meta", {}),
                }
            return Message(channel=channel, content=content, ts=ts, meta=meta)
        except (json.JSONDecodeError, ValueError):
            pass
    # Plain text fallback
    channel = _channel_from_name(fifo_name)
    return Message(channel=channel, content=stripped, ts=int(time.time()))


def _channel_from_name(fifo_name: str) -> str:
    """Extract channel name from FIFO filename (in.telegram -> telegram)."""
    name = Path(fifo_name).name
    if name.startswith("in."):
        return name[3:]
    return name


class FifoReader:
    """Async reader for a single named FIFO."""

    def __init__(self, path: Path, callback: Callable[[Message], None]) -> None:
        self.path = path
        self.callback = callback
        self._fd: int | None = None
        self._buf = b""
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Open the FIFO and register with the event loop."""
        # O_RDWR prevents EOF when all external writers close
        self._fd = os.open(str(self.path), os.O_RDWR | os.O_NONBLOCK)
        self._loop = loop
        loop.add_reader(self._fd, self._on_readable)

    def stop(self) -> None:
        """Remove from event loop and close fd."""
        if self._fd is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._fd)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._loop = None

    def _on_readable(self) -> None:
        assert self._fd is not None
        try:
            data = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            self.stop()
            return

        if not data:
            return

        self._buf += data
        while b"\n" in self._buf:
            line_bytes, self._buf = self._buf.split(b"\n", 1)
            line = line_bytes.decode(errors="replace")
            if line.strip():
                msg = parse_message(line, self.path.name)
                self.callback(msg)


class FifoManager:
    """Manages a set of FifoReaders; add/remove readers dynamically."""

    def __init__(self, callback: Callable[[Message], None]) -> None:
        self.callback = callback
        self._readers: dict[Path, FifoReader] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, path: Path) -> None:
        """Start reading from a new FIFO."""
        if path in self._readers or self._loop is None:
            return
        reader = FifoReader(path, self.callback)
        try:
            reader.start(self._loop)
            self._readers[path] = reader
        except OSError:
            pass

    def remove(self, path: Path) -> None:
        """Stop reading from a FIFO."""
        reader = self._readers.pop(path, None)
        if reader:
            reader.stop()

    def stop_all(self) -> None:
        """Stop all readers."""
        for reader in list(self._readers.values()):
            reader.stop()
        self._readers.clear()

    @property
    def active_fifos(self) -> list[Path]:
        return list(self._readers.keys())
