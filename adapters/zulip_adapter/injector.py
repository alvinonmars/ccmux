"""Lightweight FIFO-to-tmux injector for Zulip project instances.

Reads messages from a named FIFO, waits for Claude to be ready (injection gate),
then injects via tmux send-keys. Runs as a subprocess per instance.

Simpler than the main ccmux daemon — no output broadcast, no control.sock.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time

log = logging.getLogger(__name__)

# Injection gate settings
IDLE_THRESHOLD = 5.0  # seconds of terminal idle before injecting
POLL_INTERVAL = 1.0  # seconds between ready-state checks
SEND_KEYS_TIMEOUT = 10  # seconds

# Claude Code prompt pattern (❯ character)
CLAUDE_PROMPT_RE = re.compile(r"❯\s*$", re.MULTILINE)
# Shell prompt patterns (Claude has exited)
SHELL_PROMPT_RE = re.compile(r"[\$#]\s*$", re.MULTILINE)


def _tmux_capture(session: str) -> str:
    """Capture the visible tmux pane content."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _tmux_client_activity(session: str) -> float:
    """Get the client_activity timestamp from tmux."""
    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-t",
                session,
                "-p",
                "#{client_activity}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return float(result.stdout.strip()) if result.stdout.strip() else 0.0
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        return 0.0


def _tmux_has_session(session: str) -> bool:
    """Check if tmux session exists."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _inject_text(session: str, text: str) -> bool:
    """Inject text into tmux session via send-keys. Returns True on success."""
    try:
        r1 = subprocess.run(
            ["tmux", "send-keys", "-t", session, "-l", text],
            capture_output=True,
            timeout=SEND_KEYS_TIMEOUT,
        )
        if r1.returncode != 0:
            log.error("send-keys text failed (rc=%d): %s", r1.returncode,
                       r1.stderr.decode(errors="replace").strip())
            return False
        r2 = subprocess.run(
            ["tmux", "send-keys", "-t", session, "Enter"],
            capture_output=True,
            timeout=SEND_KEYS_TIMEOUT,
        )
        if r2.returncode != 0:
            log.error("send-keys Enter failed (rc=%d): %s", r2.returncode,
                       r2.stderr.decode(errors="replace").strip())
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("send-keys failed: %s", e)
        return False


class InjectionGate:
    """Check if Claude Code is ready to receive input."""

    def __init__(self, session: str):
        self.session = session

    def is_ready(self) -> bool:
        """Return True if Claude is ready (prompt visible, terminal idle)."""
        # Check terminal idle
        activity_ts = _tmux_client_activity(self.session)
        if activity_ts > 0 and (time.time() - activity_ts) < IDLE_THRESHOLD:
            return False  # Human typing

        # Check Claude state
        pane = _tmux_capture(self.session)
        if not pane:
            return False

        # Check for Claude prompt
        if CLAUDE_PROMPT_RE.search(pane):
            return True

        return False

    def is_claude_dead(self) -> bool:
        """Return True if Claude has exited (shell prompt visible)."""
        pane = _tmux_capture(self.session)
        if not pane:
            return False

        # Shell prompt without Claude prompt = Claude exited
        if SHELL_PROMPT_RE.search(pane) and not CLAUDE_PROMPT_RE.search(pane):
            return True
        return False


class Injector:
    """FIFO reader + injection gate for a single instance."""

    def __init__(self, fifo_path: str, tmux_session: str, pid_file: str | None = None):
        self.fifo_path = fifo_path
        self.tmux_session = tmux_session
        self.pid_file = pid_file  # Cleaned up on exit to signal dead instance
        self.gate = InjectionGate(tmux_session)
        self._queue: list[str] = []
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main loop: read FIFO, gate, inject."""
        log.info(
            "Injector started: fifo=%s session=%s",
            self.fifo_path,
            self.tmux_session,
        )

        # Open FIFO with O_RDWR | O_NONBLOCK to avoid EOF when no writer
        fd = os.open(self.fifo_path, os.O_RDWR | os.O_NONBLOCK)
        buffer = b""

        try:
            while self._running:
                # Check tmux session still exists
                if not _tmux_has_session(self.tmux_session):
                    log.warning("tmux session %s gone, exiting", self.tmux_session)
                    break

                # Check if Claude has exited
                if self.gate.is_claude_dead():
                    log.warning(
                        "Claude exited in session %s (shell prompt detected), exiting",
                        self.tmux_session,
                    )
                    break

                # Read from FIFO (non-blocking)
                try:
                    chunk = os.read(fd, 4096)
                    if chunk:
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            msg = line.decode("utf-8", errors="replace").strip()
                            if msg:
                                self._queue.append(msg)
                except BlockingIOError:
                    pass  # No data available

                # Try to inject queued messages
                if self._queue and self.gate.is_ready():
                    text = "\n".join(self._queue)
                    if _inject_text(self.tmux_session, text):
                        log.info(
                            "Injected %d message(s) into %s",
                            len(self._queue),
                            self.tmux_session,
                        )
                        self._queue.clear()

                await asyncio.sleep(POLL_INTERVAL)
        finally:
            os.close(fd)
            # Clean up PID file so is_alive() returns False → next message triggers lazy create
            if self.pid_file:
                try:
                    os.unlink(self.pid_file)
                    log.info("Cleaned PID file: %s", self.pid_file)
                except OSError:
                    pass
            log.info("Injector stopped: %s", self.tmux_session)
