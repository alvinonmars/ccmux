"""Unix socket pub/sub for ccmux.

control.sock: receives messages from hook.py (one-shot connections)
output.sock:  persistent subscriber connections, receives broadcasts
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class OutputBroadcaster:
    """Maintains a list of persistent subscriber connections on output.sock."""

    def __init__(self, sock_path: Path) -> None:
        self.sock_path = sock_path
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None
        self._handler_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        sock_path = str(self.sock_path)
        self.sock_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_subscriber, path=sock_path
        )
        log.info("output.sock listening", extra={"path": sock_path})

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            # Do NOT await wait_closed() — it waits for subscriber handler
            # coroutines (blocked on reader.read()) which would deadlock.

        # Cancel all subscriber handler tasks so their coroutines unblock
        for task in self._handler_tasks:
            task.cancel()
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)
        self._handler_tasks.clear()

        # Close all writer transports (sends EOF to clients)
        for w in self._writers[:]:
            try:
                w.close()
            except Exception:
                pass
        self._writers.clear()
        self.sock_path.unlink(missing_ok=True)

    async def _handle_subscriber(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Register this task so stop() can cancel it
        current = asyncio.current_task()
        if current:
            self._handler_tasks.append(current)

        self._writers.append(writer)
        log.debug("subscriber connected", extra={"count": len(self._writers)})
        try:
            # Await until client disconnects or task is cancelled
            await reader.read()
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            try:
                writer.close()
            except Exception:
                pass
            if current and current in self._handler_tasks:
                self._handler_tasks.remove(current)
            log.debug(
                "subscriber disconnected", extra={"count": len(self._writers)}
            )

    async def broadcast(self, payload: dict) -> int:
        """Broadcast payload to all subscribers. Returns subscriber count."""
        if not self._writers:
            log.debug("broadcast: no subscribers")
            return 0
        data = json.dumps(payload).encode() + b"\n"
        dead: list[asyncio.StreamWriter] = []
        for w in self._writers:
            try:
                w.write(data)
                await w.drain()
            except Exception:
                dead.append(w)
        for w in dead:
            self._writers.remove(w)
        count = len(self._writers)
        log.debug("broadcast sent", extra={"subscriber_count": count})
        return count

    @property
    def subscriber_count(self) -> int:
        return len(self._writers)


class ControlServer:
    """Receives messages from hook.py on control.sock.

    Each hook invocation connects, sends one JSON line, and disconnects.
    """

    def __init__(
        self,
        sock_path: Path,
        on_broadcast: Callable[[dict], None],
        on_event: Callable[[dict], None],
    ) -> None:
        self.sock_path = sock_path
        self._on_broadcast = on_broadcast
        self._on_event = on_event
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        sock_path = str(self.sock_path)
        self.sock_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_hook, path=sock_path
        )
        log.info("control.sock listening", extra={"path": sock_path})

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self.sock_path.unlink(missing_ok=True)

    async def _handle_hook(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not line:
                return
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                log.warning("control: invalid JSON from hook")
                return
            msg_type = msg.get("type")
            if msg_type == "broadcast":
                self._on_broadcast(msg)
            elif msg_type == "event":
                self._on_event(msg)
            else:
                log.warning("control: unknown message type", extra={"type": msg_type})
        except asyncio.TimeoutError:
            log.warning("control: hook connection timed out")
        except Exception as e:
            log.error("control: error handling hook", extra={"error": str(e)})
        finally:
            try:
                writer.close()
            except Exception:
                pass
