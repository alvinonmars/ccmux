"""MCP server providing the send_to_channel tool to Claude Code.

Transport: SSE over HTTP on 127.0.0.1:<port> (loopback only).
Claude Code connects to http://127.0.0.1:<port>/sse.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable

from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)


async def _send_to_channel(runtime_dir: Path, channel: str, message: str) -> str:
    """Core send_to_channel logic: write to out.<channel> FIFO.

    Returns 'ok' on success, or an 'Error: ...' string on failure.
    Testable directly without the MCP layer.
    """
    fifo_path = runtime_dir / f"out.{channel}"
    if not fifo_path.exists():
        err = f"channel '{channel}' not found (out.{channel} does not exist)"
        log.warning("send_to_channel: channel not found", extra={"channel": channel})
        return f"Error: {err}"

    try:
        fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        payload = (message.rstrip("\n") + "\n").encode()
        os.write(fd, payload)
        os.close(fd)
        log.info(
            "send_to_channel",
            extra={"channel": channel, "message_len": len(message)},
        )
        return "ok"
    except BlockingIOError:
        # No reader on the FIFO (adapter not consuming)
        err = f"channel '{channel}' is not being read (FIFO full or no reader)"
        log.warning("send_to_channel: FIFO not readable", extra={"channel": channel})
        return f"Error: {err}"
    except OSError as e:
        log.error("send_to_channel: OSError", extra={"error": str(e)})
        return f"Error: {e}"


def create_server(
    runtime_dir: Path,
    on_tool_call: Callable[[str, str], None] | None = None,
) -> FastMCP:
    """Create and configure the FastMCP server instance."""
    mcp = FastMCP("ccmux")

    @mcp.tool()
    async def send_to_channel(channel: str, message: str) -> str:
        """Send a message to a named output channel.

        The channel must be registered: an adapter must have created the
        FIFO /tmp/ccmux/out.<channel> before calling this tool.
        Returns 'ok' on success, or an error description.
        """
        result = await _send_to_channel(runtime_dir, channel, message)
        if on_tool_call:
            on_tool_call(channel, message)
        return result

    return mcp


async def run_server(
    mcp: FastMCP,
    host: str,
    port: int,
    ready_event: asyncio.Event | None = None,
) -> None:
    """Run the MCP SSE server (blocks until cancelled).

    If *ready_event* is provided it is set after the server has bound the port
    and is accepting connections.  This lets the daemon wait until the MCP
    server is reachable before writing ``.mcp.json``.
    """
    import uvicorn

    app = mcp.sse_app()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Replicate server.serve() with a readiness signal after startup.
    config.load()
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    if ready_event is not None:
        ready_event.set()
    if server.should_exit:
        return
    await server.main_loop()
    await server.shutdown()
