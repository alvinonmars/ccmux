"""Inject messages into the Claude Code tmux pane via send-keys.

SP-04 verified: use -l flag + separate Enter; special characters are lossless.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import libtmux

from ccmux.config import Config


@dataclass
class Message:
    channel: str
    content: str
    ts: int  # unix timestamp


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


def inject(pane: libtmux.Pane, text: str) -> None:
    """Inject text into the tmux pane, followed by Enter.

    Uses -l flag so no shell interpretation occurs.
    Enter is sent as a separate command (SP-04: combining sends literal Enter).
    """
    pane.send_keys(text, enter=False, literal=True)
    pane.send_keys("", enter=True)


def inject_messages(pane: libtmux.Pane, messages: list[Message]) -> None:
    """Format and inject a list of queued messages."""
    if not messages:
        return
    text = format_messages(messages)
    inject(pane, text)
