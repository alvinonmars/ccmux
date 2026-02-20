"""Claude Code process lifecycle manager.

Monitors the Claude process running in the tmux pane and restarts it
with exponential backoff on crash.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import libtmux

from ccmux.config import Config

log = logging.getLogger(__name__)

CLAUDE_START_CMD = "claude --dangerously-skip-permissions"
CLAUDE_CONTINUE_CMD = "claude --dangerously-skip-permissions --continue"


class LifecycleManager:
    """Monitor the Claude pane and restart on crash.

    Backoff: initial → initial*2 → ... → cap (exponential with cap).
    """

    def __init__(
        self,
        config: Config,
        pane: libtmux.Pane,
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.pane = pane
        self.on_restart = on_restart
        self._restart_count = 0
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._running = True
        self._task = asyncio.get_event_loop().create_task(self._monitor())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    @property
    def restart_count(self) -> int:
        return self._restart_count

    def _get_claude_pid(self) -> int | None:
        """Get the PID of the claude process running in the pane, or None."""
        try:
            # libtmux pane can give us the current command / pid
            pane_pid = self.pane.pid
            if pane_pid is None:
                return None
            # Check if the child process in the pane is claude
            # Use pgrep to find claude child of pane shell
            import subprocess
            result = subprocess.run(
                ["pgrep", "-P", str(pane_pid), "claude"],
                capture_output=True, text=True, timeout=2.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split()[0])
        except Exception:
            pass
        return None

    def _is_claude_running(self) -> bool:
        """Return True if claude is still running in the pane."""
        pid = self._get_claude_pid()
        if pid is not None:
            return True
        # Fallback: check capture-pane for claude prompt vs shell prompt
        try:
            capture = self.pane.cmd("capture-pane", "-p").stdout
            text = "\n".join(capture) if isinstance(capture, list) else capture
            # If pane shows a shell prompt ($ or %) without ❯, claude has exited
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines:
                return False
            last = lines[-1]
            # claude prompt contains ❯; shell prompt ends with $ or %
            if "❯" in last:
                return True
            if last.endswith(("$", "%", "#")):
                return False
        except Exception:
            pass
        return True  # assume running if we can't determine

    async def _monitor(self) -> None:
        poll_interval = 2.0  # seconds between checks
        while self._running:
            await asyncio.sleep(poll_interval)
            if not self._is_claude_running():
                log.warning(
                    "claude process died, restarting",
                    extra={"restart_count": self._restart_count},
                )
                await self._restart()

    async def _restart(self) -> None:
        backoff = min(
            self.config.backoff_initial * (2 ** self._restart_count),
            self.config.backoff_cap,
        )
        self._restart_count += 1

        log.info(
            "restarting claude",
            extra={
                "restart_count": self._restart_count,
                "backoff_seconds": backoff,
            },
        )

        await asyncio.sleep(backoff)

        try:
            # Use --continue to preserve conversation context
            cmd = CLAUDE_CONTINUE_CMD if self._restart_count > 1 else CLAUDE_START_CMD
            self.pane.send_keys(cmd, enter=True)
        except Exception as e:
            log.error("failed to restart claude", extra={"error": str(e)})
            return

        if self.on_restart:
            self.on_restart()
