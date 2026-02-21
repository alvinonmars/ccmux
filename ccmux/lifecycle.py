"""Claude Code process lifecycle manager.

Monitors the Claude process running in the tmux pane and restarts it
with exponential backoff on crash.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

import libtmux

from ccmux.config import Config

log = logging.getLogger(__name__)

_CLAUDE_BASE_CMD = "claude --dangerously-skip-permissions --continue"


class LifecycleManager:
    """Monitor the Claude pane and restart on crash.

    Backoff: initial → initial*2 → ... → cap (exponential with cap).
    """

    def __init__(
        self,
        config: Config,
        pane: libtmux.Pane,
        on_restart: Callable[[], None] | None = None,
        poll_interval: float = 2.0,
        startup_grace: float = 10.0,
    ) -> None:
        self.config = config
        self.pane = pane
        self.on_restart = on_restart
        self._poll_interval = poll_interval
        self._startup_grace = startup_grace
        # Restart count grows monotonically and is never reset.
        # If Claude ran stably for days then crashes again, the next backoff
        # immediately caps at backoff_cap. This is intentional: conservative
        # recovery for a 24/7 daemon avoids rapid restart storms.
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

    def _build_restart_cmd(self) -> str:
        """Build the full restart command including env vars.

        Mirrors the fresh-start command in daemon.py:193-196 but adds --continue.
        """
        parts: list[str] = []
        proxy = self.config.claude_proxy
        if proxy:
            parts.append(f"HTTP_PROXY={proxy} HTTPS_PROXY={proxy}")
        parts.append(f"CCMUX_CONTROL_SOCK={self.config.control_sock}")
        parts.append(_CLAUDE_BASE_CMD)
        return " ".join(parts)

    def _get_claude_pid(self) -> int | None:
        """Get the PID of the claude process running in the pane, or None."""
        try:
            # pane.pid returns the tmux server PID, not the shell PID.
            # Use #{pane_pid} format variable to get the actual shell PID.
            result = self.pane.cmd("display-message", "-p", "#{pane_pid}")
            pane_pid = int(result.stdout[0]) if result.stdout else None
            if pane_pid is None:
                return None
            # Check if the child process in the pane is claude
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
        except Exception as e:
            log.warning("capture-pane detection failed", extra={"error": str(e)})
        return False  # fail-safe: triggers restart check (has exponential backoff)

    async def _monitor(self) -> None:
        # Grace period: Claude takes several seconds to start. Skip checks
        # during this window to avoid false crash detection on first launch.
        await asyncio.sleep(self._startup_grace)
        while self._running:
            await asyncio.sleep(self._poll_interval)
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
            # Always use --continue to preserve conversation history on restart.
            # Include the same env vars as the initial launch (daemon.py:193-196):
            # proxy for network access, CCMUX_CONTROL_SOCK for hook.py.
            cmd = self._build_restart_cmd()
            self.pane.send_keys(cmd, enter=True)
        except Exception as e:
            log.error("failed to restart claude", extra={"error": str(e)})
            return

        if self.on_restart:
            self.on_restart()
