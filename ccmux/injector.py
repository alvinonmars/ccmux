"""Inject messages into the Claude Code tmux pane via send-keys.

SP-04 verified: use -l flag + separate Enter; special characters are lossless.

IMPORTANT: libtmux's send_keys() uses subprocess.run() internally, which
blocks the calling thread.  If tmux hangs (e.g. full pane buffer), the
entire asyncio event loop freezes.  This module therefore provides both
synchronous helpers (inject / inject_messages) and an async wrapper
(async_inject_messages) that runs in a thread executor with a timeout.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass

import libtmux

log = logging.getLogger(__name__)

# Timeout for a single tmux send-keys invocation (seconds).
SEND_KEYS_TIMEOUT = 10


class InjectionTimeout(Exception):
    """Raised when tmux send-keys does not complete within the timeout."""


@dataclass
class Message:
    channel: str
    content: str
    ts: int  # unix timestamp
    meta: dict | None = None  # optional structured metadata (e.g. intent classification)


def format_messages(messages: list[Message]) -> str:
    """Format queued messages into the injection string Claude will receive.

    Format per message: [HH:MM channel] content
    Multiple messages separated by newlines.
    """
    lines = []
    for msg in messages:
        t = time.strftime("%H:%M", time.localtime(msg.ts))
        lines.append(f"[{t} {msg.channel}] {msg.content}")
    return "\n".join(lines)


def _send_keys_with_timeout(
    pane_id: str, args: list[str], timeout: float = SEND_KEYS_TIMEOUT
) -> None:
    """Run ``tmux send-keys`` as a subprocess with a timeout.

    Using subprocess directly (instead of libtmux) gives us control over
    the timeout.  If the process exceeds *timeout* seconds it is killed
    and InjectionTimeout is raised.
    """
    cmd = ["tmux", "send-keys", "-t", pane_id, *args]
    proc = subprocess.run(cmd, timeout=timeout, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"tmux send-keys failed (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )


def inject(pane: libtmux.Pane, text: str) -> None:
    """Inject text into the tmux pane, followed by Enter.

    Uses -l flag so no shell interpretation occurs.
    Enter is sent as a separate command (SP-04: combining sends literal Enter).

    Raises InjectionTimeout if tmux hangs.
    """
    pane_id = getattr(pane, "pane_id", None)
    if pane_id is None:
        # Fallback for mock/fake panes in tests.
        pane.send_keys(text, enter=False, literal=True)
        pane.send_keys("", enter=True)
        return
    try:
        _send_keys_with_timeout(pane_id, ["-l", text])
        _send_keys_with_timeout(pane_id, ["Enter"])
    except subprocess.TimeoutExpired as exc:
        raise InjectionTimeout(
            f"tmux send-keys timed out after {SEND_KEYS_TIMEOUT}s"
        ) from exc


def inject_messages(pane: libtmux.Pane, messages: list[Message]) -> None:
    """Format and inject a list of queued messages (synchronous)."""
    if not messages:
        return
    text = format_messages(messages)
    inject(pane, text)


async def async_inject_messages(
    pane: libtmux.Pane, messages: list[Message]
) -> None:
    """Non-blocking wrapper: run inject_messages in a thread executor.

    This prevents a hung tmux send-keys from freezing the asyncio event
    loop.  On timeout, InjectionTimeout propagates to the caller.
    """
    if not messages:
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, inject_messages, pane, messages)
