"""Lightweight FIFO-to-tmux injector for Zulip project instances.

Reads messages from a named FIFO, waits for Claude to be ready (injection gate),
then injects via tmux send-keys. Runs as a subprocess per instance.

Simpler than the main ccmux daemon — no output broadcast, no control.sock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Injection gate settings
IDLE_THRESHOLD = 5.0  # seconds of terminal idle before injecting
POLL_INTERVAL = 1.0  # seconds between ready-state checks
SEND_KEYS_TIMEOUT = 10  # seconds

# Claude's TUI shows the ❯ prompt before its input handler is fully
# initialized.  If we inject during that window the text is silently
# dropped.  Wait at least this many seconds after start before the
# first injection attempt.
FIRST_INJECT_SETTLE = 5.0

# tmux send-keys has a text length limit (~2KB safe). Beyond this, use
# load-buffer + paste-buffer which has no practical limit.
SEND_KEYS_MAX_BYTES = 2048

# Maximum consecutive injection failures before discarding the message batch.
MAX_INJECT_RETRIES = 5

# Delivery verification: after injection, poll transcript for confirmation.
DELIVERY_VERIFY_TIMEOUT = 15.0  # seconds to wait for transcript confirmation
DELIVERY_VERIFY_POLL = 2.0  # seconds between transcript checks

# Claude Code prompt pattern — bare ❯ on a line by itself (with optional
# whitespace).  This avoids false positives from "❯ Press up to edit queued
# messages" or "❯ [queued text]" which appear when Claude is processing.
CLAUDE_PROMPT_RE = re.compile(r"^\s*❯\s*$", re.MULTILINE)
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
    """Inject text into tmux session. Returns True on success.

    Short text uses ``send-keys -l`` directly.  Long text (> SEND_KEYS_MAX_BYTES)
    is written to a temp file and pasted via ``load-buffer`` + ``paste-buffer``,
    which bypasses tmux's send-keys argument length limit.

    A short delay between text and Enter prevents the TUI from dropping
    the Enter key when still processing a large or multi-line paste.
    """
    if len(text.encode("utf-8")) > SEND_KEYS_MAX_BYTES:
        return _inject_text_via_buffer(session, text)
    return _inject_text_via_sendkeys(session, text)


def _inject_text_via_sendkeys(session: str, text: str) -> bool:
    """Inject short text via tmux send-keys."""
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
        time.sleep(0.15)
        return _send_enter(session)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("send-keys failed: %s", e)
        return False


def _inject_text_via_buffer(session: str, text: str) -> bool:
    """Inject long text via tmux load-buffer + paste-buffer."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="ccmux_inject_", suffix=".txt")
        os.write(fd, text.encode("utf-8"))
        os.close(fd)

        r1 = subprocess.run(
            ["tmux", "load-buffer", tmp_path],
            capture_output=True,
            timeout=SEND_KEYS_TIMEOUT,
        )
        if r1.returncode != 0:
            log.error("load-buffer failed (rc=%d): %s", r1.returncode,
                       r1.stderr.decode(errors="replace").strip())
            return False

        r2 = subprocess.run(
            ["tmux", "paste-buffer", "-t", session],
            capture_output=True,
            timeout=SEND_KEYS_TIMEOUT,
        )
        if r2.returncode != 0:
            log.error("paste-buffer failed (rc=%d): %s", r2.returncode,
                       r2.stderr.decode(errors="replace").strip())
            return False

        time.sleep(0.15)
        return _send_enter(session)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.error("buffer inject failed: %s", e)
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _send_enter(session: str) -> bool:
    """Send Enter key to tmux session."""
    try:
        r = subprocess.run(
            ["tmux", "send-keys", "-t", session, "Enter"],
            capture_output=True,
            timeout=SEND_KEYS_TIMEOUT,
        )
        if r.returncode != 0:
            log.error("send-keys Enter failed (rc=%d): %s", r.returncode,
                       r.stderr.decode(errors="replace").strip())
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("send-keys Enter failed: %s", e)
        return False


class InjectionGate:
    """Check if Claude Code is ready to receive input."""

    def __init__(self, session: str):
        self.session = session

    def is_ready(self) -> bool:
        """Return True if Claude is ready (prompt visible on last lines, terminal idle).

        Only checks the last few non-empty lines for the ❯ prompt to avoid
        false positives from old prompts still visible in scrollback while
        Claude is actively generating output.
        """
        # Check terminal idle
        activity_ts = _tmux_client_activity(self.session)
        if activity_ts > 0 and (time.time() - activity_ts) < IDLE_THRESHOLD:
            return False  # Human typing

        # Check Claude state
        pane = _tmux_capture(self.session)
        if not pane:
            return False

        # Only check the last 6 non-empty lines for the prompt.
        # Claude's TUI status bar can be up to 4 lines (separator + info),
        # pushing ❯ to position -4 or -5.  6 lines catches this while still
        # avoiding old prompts in scrollback (typically 10+ lines up).
        lines = [ln for ln in pane.splitlines() if ln.strip()]
        tail = "\n".join(lines[-6:]) if lines else ""
        if CLAUDE_PROMPT_RE.search(tail):
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

    # Grace period before checking is_claude_dead() — Claude needs ~2-3s to start
    STARTUP_GRACE = 5.0

    # Log a warning if queued messages haven't been injected for this long
    QUEUE_STALL_WARN_SECS = 30.0

    def __init__(
        self,
        fifo_path: str,
        tmux_session: str,
        pid_file: str | None = None,
        transcript_path: str | None = None,
    ):
        self.fifo_path = fifo_path
        self.tmux_session = tmux_session
        self.pid_file = pid_file  # Cleaned up on exit to signal dead instance
        self.transcript_path = transcript_path  # For delivery verification
        self.gate = InjectionGate(tmux_session)
        self._queue: list[str] = []
        self._running = True
        self._start_time = time.monotonic()
        self._inject_failures = 0  # Consecutive injection failure count
        self._queue_first_seen: float = 0.0  # When queue first became non-empty
        self._stall_warned: bool = False  # Whether we've logged a stall warning
        self._first_inject_done: bool = False  # Whether we've successfully injected once
        # Delivery verification state
        self._verify_pending: bool = False  # Waiting for transcript confirmation
        self._verify_start: float = 0.0  # When verification started
        self._pre_inject_size: int = 0  # Transcript file size before injection
        self._pending_count: int = 0  # Number of messages pending verification

    def stop(self) -> None:
        self._running = False

    def _transcript_size(self) -> int:
        """Get current transcript file size, or 0 if unavailable."""
        if not self.transcript_path:
            return 0
        try:
            return Path(self.transcript_path).stat().st_size
        except OSError:
            return 0

    def _check_transcript_for_user_message(self) -> bool:
        """Check if a new user message appeared in transcript since injection.

        Reads transcript from the pre-injection file position and looks for
        any user message with text content (not tool_result).  A new user
        message proves Claude's TUI received and processed our injection.
        """
        if not self.transcript_path:
            return True  # No transcript → trust injection
        path = Path(self.transcript_path)
        if not path.exists():
            return False
        try:
            current_size = path.stat().st_size
            if current_size <= self._pre_inject_size:
                return False
            with open(path, "r") as f:
                f.seek(self._pre_inject_size)
                new_data = f.read()
        except OSError:
            return False

        for line in new_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = record.get("message", {})
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            # String content = direct user input
            if isinstance(content, str) and content.strip():
                return True
            # List content: look for text blocks (not tool_result)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return True
        return False

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
                    if self._queue:
                        log.warning(
                            "tmux session %s gone, dropping %d pending message(s)",
                            self.tmux_session, len(self._queue),
                        )
                    else:
                        log.warning("tmux session %s gone, exiting", self.tmux_session)
                    break

                # Check if Claude has exited (skip during startup grace period)
                if (time.monotonic() - self._start_time) >= self.STARTUP_GRACE:
                    if self.gate.is_claude_dead():
                        if self._queue:
                            log.warning(
                                "Claude exited in session %s (shell prompt detected), "
                                "dropping %d pending message(s)",
                                self.tmux_session, len(self._queue),
                            )
                        else:
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
                        while b"\0" in buffer:
                            frame, buffer = buffer.split(b"\0", 1)
                            msg = frame.decode("utf-8", errors="replace").strip()
                            if msg:
                                self._queue.append(msg)
                except BlockingIOError:
                    pass  # No data available

                # --- Delivery verification phase ---
                # After injection, poll the transcript for confirmation
                # before clearing the queue.
                if self._verify_pending:
                    if self._check_transcript_for_user_message():
                        log.info(
                            "Delivery VERIFIED for %d message(s) in %s",
                            self._pending_count, self.tmux_session,
                        )
                        self._verify_pending = False
                        self._first_inject_done = True
                        self._queue.clear()
                        self._inject_failures = 0
                        self._queue_first_seen = 0.0
                        self._stall_warned = False
                    elif (time.monotonic() - self._verify_start) > DELIVERY_VERIFY_TIMEOUT:
                        self._inject_failures += 1
                        self._verify_pending = False
                        log.warning(
                            "Delivery NOT verified after %.0fs for %s "
                            "(attempt %d/%d), will retry",
                            DELIVERY_VERIFY_TIMEOUT,
                            self.tmux_session,
                            self._inject_failures,
                            MAX_INJECT_RETRIES,
                        )
                        if self._inject_failures >= MAX_INJECT_RETRIES:
                            log.error(
                                "Dropping %d message(s) after %d unverified "
                                "injections in %s",
                                len(self._queue),
                                self._inject_failures,
                                self.tmux_session,
                            )
                            self._queue.clear()
                            self._inject_failures = 0
                            self._queue_first_seen = 0.0
                            self._stall_warned = False
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # --- Injection phase ---
                if self._queue:
                    # Track how long messages have been waiting
                    if self._queue_first_seen == 0.0:
                        self._queue_first_seen = time.monotonic()
                        self._stall_warned = False

                    if self.gate.is_ready():
                        # Guard against injecting before Claude's TUI is
                        # fully initialized.  The prompt appears before the
                        # input handler is ready; text injected during that
                        # window is silently dropped.
                        if not self._first_inject_done:
                            elapsed = time.monotonic() - self._start_time
                            if elapsed < FIRST_INJECT_SETTLE:
                                log.debug(
                                    "Prompt visible but waiting for TUI settle "
                                    "(%.1fs / %.1fs) in %s",
                                    elapsed, FIRST_INJECT_SETTLE,
                                    self.tmux_session,
                                )
                                await asyncio.sleep(POLL_INTERVAL)
                                continue

                        text = "\n---\n".join(self._queue)
                        # Snapshot transcript size BEFORE injection
                        self._pre_inject_size = self._transcript_size()
                        if _inject_text(self.tmux_session, text):
                            log.info(
                                "Injected %d message(s) (%d bytes) into %s, "
                                "awaiting transcript verification",
                                len(self._queue),
                                len(text.encode("utf-8")),
                                self.tmux_session,
                            )
                            if self.transcript_path:
                                # Enter verification phase
                                self._verify_pending = True
                                self._verify_start = time.monotonic()
                                self._pending_count = len(self._queue)
                            else:
                                # No transcript → trust injection
                                self._first_inject_done = True
                                self._queue.clear()
                                self._inject_failures = 0
                                self._queue_first_seen = 0.0
                                self._stall_warned = False
                        else:
                            self._inject_failures += 1
                            if self._inject_failures >= MAX_INJECT_RETRIES:
                                log.error(
                                    "Dropping %d message(s) after %d injection "
                                    "failures in %s (total %d bytes)",
                                    len(self._queue),
                                    self._inject_failures,
                                    self.tmux_session,
                                    len(text.encode("utf-8")),
                                )
                                self._queue.clear()
                                self._inject_failures = 0
                                self._queue_first_seen = 0.0
                                self._stall_warned = False
                    else:
                        # Gate not ready — log stall warning if waiting too long
                        wait_secs = time.monotonic() - self._queue_first_seen
                        if wait_secs >= self.QUEUE_STALL_WARN_SECS and not self._stall_warned:
                            log.warning(
                                "Injection queue stalled: %d message(s) waiting %.0fs "
                                "for Claude prompt in %s (gate not ready)",
                                len(self._queue), wait_secs, self.tmux_session,
                            )
                            self._stall_warned = True

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
