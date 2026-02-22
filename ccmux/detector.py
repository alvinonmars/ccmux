"""Claude Code ready state detection.

SP-02 verified:
- Primary: 3s stdout silence (pipe-pane -O writes to stdout.log)
- Auxiliary: capture-pane last line contains ❯ (READY) or spinner char (GENERATING)
- Permission prompt: capture-pane keyword search for Yes/No/allow/y/n
"""
from __future__ import annotations

import asyncio
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


_PERMISSION_KEYWORDS = ("Yes/No", "yes/no", "y/n", "Allow once", "Allow always")

# SP-02 verified spinner chars. Excludes * and · (appear in normal text).
_SPINNER_CHARS = frozenset("✻✶✽✢●")


def _check_permission_prompt(capture: str) -> bool:
    """Return True if capture-pane shows an *active* permission prompt.

    Only the last 5 non-empty lines are checked, and the Claude prompt (❯)
    must NOT be present on the last line.  This prevents false positives from
    resolved permission prompts that remain visible in terminal scrollback.
    """
    lines = [l for l in capture.splitlines() if l.strip()]
    if not lines:
        return False
    # ❯ at the end means Claude is back in ready state — prompt is resolved
    if "❯" in lines[-1]:
        return False
    recent = lines[-5:]
    return any(kw in "\n".join(recent) for kw in _PERMISSION_KEYWORDS)


def _check_generating(capture: str) -> bool:
    """Return True if the last visible line contains spinner characters (Claude is generating)."""
    lines = [l for l in capture.splitlines() if l.strip()]
    if not lines:
        return False
    return any(ch in lines[-1] for ch in _SPINNER_CHARS)


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
        max_bytes: int = 0,
        on_truncate: Callable[[], None] | None = None,
    ) -> None:
        self.stdout_log = stdout_log
        self.silence_timeout = silence_timeout
        self.on_ready = on_ready
        self.poll_interval = poll_interval
        self.max_bytes = max_bytes  # 0 = no limit
        self.on_truncate = on_truncate
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
        """Reset the silence timer (called when generation starts).

        Also resets _last_mtime so the next poll treats the current file mtime
        as new activity and restarts the silence countdown. Without this, if the
        file is not written between two consecutive turns, the silence timer would
        never restart and on_ready would not fire again.
        """
        self._fired = False
        self._silence_start = None
        self._last_mtime = 0.0

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                st = self.stdout_log.stat()
            except FileNotFoundError:
                continue

            # Size-based truncation: clear file and re-mount pipe-pane
            if self.max_bytes > 0 and st.st_size > self.max_bytes:
                try:
                    self.stdout_log.write_bytes(b"")
                except OSError:
                    pass
                if self.on_truncate:
                    self.on_truncate()
                continue

            mtime = st.st_mtime
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
        if _check_generating(capture_text):
            return State.GENERATING
        if _check_prompt_present(capture_text):
            return State.READY
        return State.UNKNOWN

    def is_permission_prompt(self) -> bool:
        return self.get_state() == State.PERMISSION
