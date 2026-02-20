"""ccmux daemon — main orchestrator.

Startup sequence (per spec):
1. Environment check (HTTP proxy warning)
2. Hook installation
3. MCP server start
4. MCP config write (~/.claude.json)
5. tmux session handling
6. pipe-pane mount
7. Directory watcher start
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Optional

import libtmux
import structlog

from ccmux import config as config_module
from ccmux.config import Config
from ccmux.detector import ReadyDetector, State, StdoutMonitor
from ccmux.fifo import FifoManager, Message
from ccmux.hooks_manager import install as install_hooks
from ccmux.injector import inject_messages
from ccmux.lifecycle import LifecycleManager
from ccmux.mcp_server import create_server, run_server
from ccmux.pubsub import ControlServer, OutputBroadcaster
from ccmux.watcher import DirectoryWatcher

log = structlog.get_logger(__name__)


def _configure_logging(runtime_dir: Path) -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


class Daemon:
    """The ccmux daemon orchestrates all components."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._message_queue: list[Message] = []
        self._last_terminal_input: float = 0.0
        self._permission_detected: bool = False
        self._current_session_id: Optional[str] = None
        self._pane: Optional[libtmux.Pane] = None
        self._running = False

        # Components (initialized in start())
        self._broadcaster: Optional[OutputBroadcaster] = None
        self._control: Optional[ControlServer] = None
        self._fifo_mgr: Optional[FifoManager] = None
        self._watcher: Optional[DirectoryWatcher] = None
        self._lifecycle: Optional[LifecycleManager] = None
        self._stdout_monitor: Optional[StdoutMonitor] = None
        self._detector: Optional[ReadyDetector] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize all components and start background tasks."""
        _configure_logging(self.cfg.runtime_dir)
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)

        _warn_proxy()

        install_hooks(self.cfg)
        log.info("hooks installed", hook_script=str(self.cfg.hook_script))

        _write_mcp_config(self.cfg)
        log.info("MCP config written", url=self.cfg.mcp_url)

        # Start pub/sub servers
        self._broadcaster = OutputBroadcaster(self.cfg.output_sock)
        await self._broadcaster.start()

        self._control = ControlServer(
            self.cfg.control_sock,
            on_broadcast=self._on_broadcast,
            on_event=self._on_event,
        )
        await self._control.start()

        # Start FIFO manager
        self._fifo_mgr = FifoManager(callback=self._on_message)
        self._fifo_mgr.start(asyncio.get_event_loop())

        # Start directory watcher
        loop = asyncio.get_event_loop()
        self._watcher = DirectoryWatcher(
            self.cfg.runtime_dir,
            loop,
            on_input_add=self._on_fifo_add,
            on_input_remove=self._on_fifo_remove,
        )
        self._watcher.start()
        self._watcher.scan_existing()

        # Ensure default input FIFO exists
        default_in = self.cfg.runtime_dir / "in"
        if not default_in.exists():
            os.mkfifo(str(default_in))
        self._fifo_mgr.add(default_in)

        # Setup tmux + Claude
        await self._setup_tmux()

        # Start stdout silence monitor
        if self._pane is not None:
            self._stdout_monitor = StdoutMonitor(
                stdout_log=self.cfg.stdout_log,
                silence_timeout=self.cfg.silence_timeout,
                on_ready=self._on_silence_ready,
            )
            self._stdout_monitor.start()

        self._running = True
        log.info("daemon started", session=self.cfg.tmux_session)

    async def run(self) -> None:
        """Start daemon and run until SIGTERM/SIGINT."""
        await self.start()

        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT, stop_event.set)

        await stop_event.wait()
        await self.stop()

    async def stop(self) -> None:
        """Graceful shutdown: stop all components, clean up sockets."""
        self._running = False
        log.info("daemon stopping")

        if self._lifecycle:
            self._lifecycle.stop()
        if self._stdout_monitor:
            self._stdout_monitor.stop()
        if self._watcher:
            self._watcher.stop()
        if self._fifo_mgr:
            self._fifo_mgr.stop_all()
        if self._control:
            await self._control.stop()
        if self._broadcaster:
            await self._broadcaster.stop()

        log.info("daemon stopped")

    # ------------------------------------------------------------------
    # tmux setup
    # ------------------------------------------------------------------

    async def _setup_tmux(self) -> None:
        server = libtmux.Server()
        session = server.find_where({"session_name": self.cfg.tmux_session})

        if session is None:
            log.info("creating new tmux session", session=self.cfg.tmux_session)
            env = {
                **os.environ,
                "CCMUX_CONTROL_SOCK": str(self.cfg.control_sock),
            }
            session = server.new_session(
                session_name=self.cfg.tmux_session,
                window_name="claude",
            )
            pane = session.active_window.active_pane
            pane.send_keys(
                f"CCMUX_CONTROL_SOCK={self.cfg.control_sock} "
                f"claude --dangerously-skip-permissions",
                enter=True,
            )
        else:
            log.info("attaching to existing tmux session", session=self.cfg.tmux_session)
            session = session

        self._pane = session.active_window.active_pane
        await asyncio.sleep(0.5)  # let pane initialize

        # Mount pipe-pane for stdout/stdin monitoring
        self._mount_pipe_pane()

        # Detect current state
        if self._detector is None and self._pane is not None:
            self._detector = ReadyDetector(self._pane, self.cfg.silence_timeout)

        state = self._detector.get_state() if self._detector else State.UNKNOWN
        if state == State.PERMISSION:
            self._permission_detected = True
            log.info("detected permission prompt on attach")

        # Start lifecycle manager
        if self._pane is not None:
            self._lifecycle = LifecycleManager(
                self.cfg, self._pane, on_restart=self._on_claude_restart
            )
            self._lifecycle.start()

    def _mount_pipe_pane(self) -> None:
        """Mount pipe-pane -O/-I for stdout/stdin monitoring."""
        if self._pane is None:
            return
        stdout_log = self.cfg.stdout_log
        stdin_log = self.cfg.stdin_log
        try:
            self._pane.cmd(
                "pipe-pane",
                "-O",
                f"cat >> {stdout_log}",
            )
            self._pane.cmd(
                "pipe-pane",
                "-I",
                f"cat >> {stdin_log}",
            )
        except Exception as e:
            log.warning("pipe-pane failed", error=str(e))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_broadcast(self, msg: dict) -> None:
        """Called when hook.py sends a broadcast (Stop hook fired)."""
        session = msg.get("session", "")
        turn = msg.get("turn", [])
        ts = msg.get("ts", int(time.time()))
        self._current_session_id = session

        log.info("stop hook received", session=session)

        # Broadcast to output.sock subscribers (fire-and-forget)
        payload = {"ts": ts, "session": session, "turn": turn}
        asyncio.get_event_loop().create_task(self._broadcast(payload))

        # Reset silence monitor
        if self._stdout_monitor:
            self._stdout_monitor.reset()

        # Check if we should inject
        asyncio.get_event_loop().create_task(self._maybe_inject())

    def _on_event(self, msg: dict) -> None:
        """Called when hook.py sends a non-broadcast event."""
        event = msg.get("event", "")
        session = msg.get("session", "")
        data = msg.get("data", {})

        log.info("hook event received", event=event, session=session)

        if event == "SessionStart":
            self._current_session_id = session

        elif event == "PermissionRequest":
            self._permission_detected = True
            log.info("permission prompt detected via hook")

        elif event == "SessionEnd":
            log.info("claude session ended")

    def _on_message(self, msg: Message) -> None:
        """Called when a message arrives on any in.* FIFO."""
        self._message_queue.append(msg)
        log.info(
            "message received",
            channel=msg.channel,
            content_len=len(msg.content),
        )

    def _on_fifo_add(self, path: Path) -> None:
        """Called when a new input FIFO is detected by the directory watcher."""
        log.info("FIFO registered", path=str(path))
        if self._fifo_mgr:
            self._fifo_mgr.add(path)

    def _on_fifo_remove(self, path: Path) -> None:
        """Called when an input FIFO is removed."""
        log.info("FIFO deregistered", path=str(path))
        if self._fifo_mgr:
            self._fifo_mgr.remove(path)

    def _on_silence_ready(self) -> None:
        """Called when stdout silence detector fires (fallback ready detection)."""
        log.info("ready detected", method="timeout")
        asyncio.get_event_loop().create_task(self._maybe_inject())

    def _on_claude_restart(self) -> None:
        """Called after lifecycle manager restarts Claude."""
        self._permission_detected = False
        if self._stdout_monitor:
            self._stdout_monitor.reset()
        self._mount_pipe_pane()

    # ------------------------------------------------------------------
    # Injection logic
    # ------------------------------------------------------------------

    async def _maybe_inject(self) -> None:
        """Inject queued messages if terminal is idle and Claude is ready."""
        if not self._message_queue:
            return
        if self._pane is None:
            return

        # Check terminal activity
        if self._is_terminal_active():
            log.info("injection suppressed: terminal active")
            return

        # Check permission prompt
        if self._permission_detected:
            log.info("injection suppressed: permission prompt")
            return

        # Check capture-pane state
        if self._detector:
            state = self._detector.get_state()
            if state == State.PERMISSION:
                self._permission_detected = True
                log.info("injection suppressed: permission prompt detected via capture-pane")
                return
            if state == State.GENERATING:
                log.info("injection suppressed: Claude is generating")
                return

        messages = self._message_queue[:]
        self._message_queue.clear()

        log.info("injecting messages", message_count=len(messages))
        try:
            inject_messages(self._pane, messages)
        except Exception as e:
            log.error("injection failed", error=str(e))
            # Put messages back
            self._message_queue[:0] = messages

    def _is_terminal_active(self) -> bool:
        """Return True if a human used the terminal within idle_threshold."""
        if self._last_terminal_input == 0.0:
            return False
        elapsed = time.time() - self._last_terminal_input
        return elapsed < self.cfg.idle_threshold

    def _update_terminal_input_time(self) -> None:
        """Update last_terminal_input (called by stdin monitor)."""
        self._last_terminal_input = time.time()

    async def _broadcast(self, payload: dict) -> None:
        if self._broadcaster:
            count = await self._broadcaster.broadcast(payload)
            log.info("broadcast sent", subscriber_count=count)

    # ------------------------------------------------------------------
    # Properties for testing
    # ------------------------------------------------------------------

    @property
    def message_queue(self) -> list[Message]:
        return self._message_queue

    @property
    def permission_detected(self) -> bool:
        return self._permission_detected

    @permission_detected.setter
    def permission_detected(self, value: bool) -> None:
        self._permission_detected = value

    @property
    def last_terminal_input(self) -> float:
        return self._last_terminal_input

    @last_terminal_input.setter
    def last_terminal_input(self, value: float) -> None:
        self._last_terminal_input = value

    @property
    def broadcaster(self) -> Optional[OutputBroadcaster]:
        return self._broadcaster


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _warn_proxy() -> None:
    """Warn if HTTP proxy env vars are not set."""
    if not (os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")):
        log.warning("HTTP_PROXY/HTTPS_PROXY not set; Claude may not connect")


def _write_mcp_config(cfg: Config) -> None:
    """Write MCP server address into ~/.claude.json."""
    claude_json = Path.home() / ".claude.json"
    data: dict = {}
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    data.setdefault("mcpServers", {})["ccmux"] = {
        "type": "sse",
        "url": cfg.mcp_url,
    }
    claude_json.write_text(json.dumps(data, indent=2) + "\n")


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------


async def _run_daemon_and_mcp(cfg: Config) -> None:
    """Run the daemon alongside the MCP server."""
    daemon = Daemon(cfg)
    mcp = create_server(cfg.runtime_dir)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(daemon.run())
        tg.create_task(run_server(mcp, host="127.0.0.1", port=cfg.mcp_port))


def main() -> None:
    """CLI entrypoint: ccmux start"""
    cfg = config_module.load()
    asyncio.run(_run_daemon_and_mcp(cfg))


if __name__ == "__main__":
    main()
