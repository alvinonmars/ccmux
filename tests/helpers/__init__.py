"""Test helpers for ccmux."""
from __future__ import annotations

import asyncio
from pathlib import Path


async def connect_subscriber(
    sock_path: Path, timeout: float = 2.0
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to output.sock; returns (reader, writer).

    Use reader.readline() to receive broadcasts.
    Call writer.close() / await writer.wait_closed() to disconnect.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(sock_path)),
        timeout=timeout,
    )
    return reader, writer
