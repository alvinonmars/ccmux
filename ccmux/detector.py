"""Claude Code ready state detection.

SP-02 verified:
- Primary: 3s stdout silence (pipe-pane -O writes to stdout.log)
- Auxiliary: capture-pane last line contains ❯
- Permission prompt: capture-pane keyword search for Yes/No/allow/y/n
"""
from __future__ import annotations

import asyncio
import os
import time
from enum import Enum
from pathlib import Path
from typing import Callable

import libtmux


class State(Enum):
    UNKNOWN = "unknown"
    GENERATING = "generating"
    READY = "ready"
    PERMISSION = "permission"


_PERMISSION_KEYWORDS = ("Yes", "No", "allow", "y/n", "Allow", "yes/no")


def _check_permission_prompt(capture: str) -> bool:
    """Return True if capture-pane output contains a permission prompt."""
    return any(kw in capture for kw in _PERMISSION_KEYWORDS)


def _check_prompt_present(capture: str) -> bool:
    """Return True if the last non-empty line looks like a Claude prompt."""
    lines = [l for l in capture.splitlines() if l.strip()]
    if not lines:
        return False
    last = lines[-1]
    return "❯" in last


class StdoutMonitor:
    """Monitor stdout.log mtime to detect Claude's ready state.

    pipe-pane -O writes Claude's stdout to stdout.log. When stdout is
    silent for silence_timeout seconds, Claude is considered ready.
    """

    def __init__(
        self,
        stdout_log: Path,
        silence_timeout: float,
        on_ready: Callable[[], None],
        poll_interval: float = 0.3,
    ) -> None:
        self.stdout_log = stdout_log
        self.silence_timeout = silence_timeout
        self.on_ready = on_ready
        self.poll_interval = poll_interval
        self._last_mtime: float = 0.0
        self._silence_start: float | None = None
        self._fired: bool = False  # has on_ready been called since last reset?
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def reset(self) -> None:
        """Reset the silence timer (called when generation starts)."""
        self._fired = False
        self._silence_start = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                mtime = self.stdout_log.stat().st_mtime
            except FileNotFoundError:
                continue

            now = time.time()
            if mtime != self._last_mtime:
                # stdout activity detected
                self._last_mtime = mtime
                self._silence_start = now
                self._fired = False
            elif self._silence_start is not None and not self._fired:
                elapsed = now - self._silence_start
                if elapsed >= self.silence_timeout:
                    self._fired = True
                    self.on_ready()


class ReadyDetector:
    """Determine Claude's state using capture-pane + stdout silence."""

    def __init__(self, pane: libtmux.Pane, silence_timeout: float) -> None:
        self.pane = pane
        self.silence_timeout = silence_timeout

    def get_state(self) -> State:
        """Snapshot the current state using capture-pane (synchronous)."""
        try:
            capture = self.pane.cmd("capture-pane", "-p").stdout
            capture_text = "\n".join(capture) if isinstance(capture, list) else capture
        except Exception:
            return State.UNKNOWN

        if _check_permission_prompt(capture_text):
            return State.PERMISSION
        if _check_prompt_present(capture_text):
            return State.READY
        return State.UNKNOWN

    def is_permission_prompt(self) -> bool:
        return self.get_state() == State.PERMISSION
