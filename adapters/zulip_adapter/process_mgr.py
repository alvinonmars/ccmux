"""Instance lifecycle manager for the Zulip adapter.

Handles lazy creation, liveness checks, and cleanup for per-topic
Claude Code instances. Each stream+topic pair maps to:
  - A tmux session (Claude Code running inside)
  - A FIFO reader (injector)
  - A PID file for liveness tracking

Runtime layout:
  /tmp/ccmux/{stream}/{topic}/
    in.zulip     ← FIFO
    pid          ← pane PID
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import signal
import subprocess
from pathlib import Path

from .config import StreamConfig, ZulipAdapterConfig
from .injector import Injector

log = logging.getLogger(__name__)

CCMUX_INIT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "ccmux_init.py"
PYTHON = Path(__file__).resolve().parent.parent.parent / ".venv" / "bin" / "python3"

# Characters safe for tmux session names and directory names
_UNSAFE_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_name(name: str) -> str:
    """Sanitize a stream or topic name for tmux sessions and directory paths.

    Zulip topics can contain ':', '.', spaces, '()', '#', etc.
    tmux interprets ':' as window separator and '.' as pane separator.
    Replace unsafe chars with underscore.
    """
    return _UNSAFE_CHARS_RE.sub("_", name)


def _tmux_session_name(stream: str, topic: str) -> str:
    """Build tmux session name from stream+topic (sanitized)."""
    return f"{_sanitize_name(stream)}--{_sanitize_name(topic)}"


def _runtime_dir(cfg: ZulipAdapterConfig, stream: str, topic: str) -> Path:
    return cfg.runtime_dir / _sanitize_name(stream) / _sanitize_name(topic)


def _pid_file(cfg: ZulipAdapterConfig, stream: str, topic: str) -> Path:
    return _runtime_dir(cfg, stream, topic) / "pid"


def _fifo_path(cfg: ZulipAdapterConfig, stream: str, topic: str) -> Path:
    return _runtime_dir(cfg, stream, topic) / "in.zulip"


def _is_process_alive(pid: int) -> bool:
    """Check if a process is alive using kill -0."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _tmux_has_session(session: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _tmux_kill_session(session: str) -> None:
    """Kill an existing tmux session."""
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _parse_env_template(template_path: Path) -> dict[str, str]:
    """Parse env_template.sh and return env var dict.

    Reads lines of the form: export KEY=VALUE
    Skips ${VARIABLE} placeholders — those are substituted by the caller.
    """
    env: dict[str, str] = {}
    if not template_path.exists():
        log.warning("env_template not found: %s", template_path)
        return env

    for line in template_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes (shell-standard quoting)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        # Skip template placeholders like ${STREAM_NAME}
        if "${" in value:
            continue
        env[key] = value

    return env


class ProcessManager:
    """Manages per-topic Claude Code instances."""

    def __init__(self, cfg: ZulipAdapterConfig):
        self.cfg = cfg
        self._injectors: dict[str, Injector] = {}  # "stream/topic" → Injector
        self._injector_tasks: dict[str, asyncio.Task] = {}
        self._sentinel_fds: dict[str, int] = {}  # "stream/topic" → fd

    def clean_stale_pids(self) -> int:
        """Delete all PID files under runtime_dir. Returns count removed."""
        count = 0
        if not self.cfg.runtime_dir.exists():
            return 0

        for pid_file in self.cfg.runtime_dir.rglob("pid"):
            try:
                pid_file.unlink()
                count += 1
                log.info("Cleaned stale PID file: %s", pid_file)
            except OSError as e:
                log.warning("Failed to remove %s: %s", pid_file, e)

        return count

    def is_alive(self, stream: str, topic: str) -> bool:
        """Check if an instance is alive (PID file + process + tmux session)."""
        pf = _pid_file(self.cfg, stream, topic)
        if not pf.exists():
            return False

        try:
            pid = int(pf.read_text().strip())
        except (ValueError, OSError):
            return False

        session = _tmux_session_name(stream, topic)
        return _is_process_alive(pid) and _tmux_has_session(session)

    def get_fifo(self, stream: str, topic: str) -> Path:
        """Return the FIFO path for a given stream+topic."""
        return _fifo_path(self.cfg, stream, topic)

    async def ensure_instance(
        self, stream: str, topic: str, stream_cfg: StreamConfig
    ) -> tuple[Path, bool]:
        """Ensure instance is running. Lazy creates if needed.

        Returns (fifo_path, created) where created is True if a new instance
        was spawned (so the caller can send a "Session started" notification).
        """
        fifo = _fifo_path(self.cfg, stream, topic)
        key = f"{stream}/{topic}"

        # Check if injector task has crashed (even if PID/tmux are alive)
        injector_dead = (
            key in self._injector_tasks
            and self._injector_tasks[key].done()
        )

        if self.is_alive(stream, topic) and fifo.exists() and not injector_dead:
            return fifo, False

        # Fallback: injector task running + tmux alive = instance is OK
        # (covers case where PID file write failed in _lazy_create step 7)
        if (
            not injector_dead
            and key in self._injector_tasks
            and fifo.exists()
            and _tmux_has_session(_tmux_session_name(stream, topic))
        ):
            log.debug("PID file missing but injector+tmux alive for %s", key)
            return fifo, False

        if injector_dead:
            log.warning("Injector task died for %s, recreating instance", key)

        result = await self._lazy_create(stream, topic, stream_cfg)
        if result is None:
            # tmux creation failed — return FIFO path but not "created"
            # so caller doesn't send a false "Session started" notification
            return fifo, False
        return result, True

    async def _lazy_create(
        self, stream: str, topic: str, stream_cfg: StreamConfig
    ) -> Path | None:
        """Create a new instance: instance.toml, dirs, FIFO, tmux, injector, PID.

        Returns FIFO path on success, None on failure (e.g. tmux creation failed).
        """
        session = _tmux_session_name(stream, topic)
        runtime = _runtime_dir(self.cfg, stream, topic)
        fifo = _fifo_path(self.cfg, stream, topic)
        pf = _pid_file(self.cfg, stream, topic)

        log.info(
            "Lazy creating instance: stream=%s topic=%s session=%s",
            stream, topic, session,
        )

        # 1. Create instance.toml in config directory (sanitized to prevent path traversal)
        instance_dir = self.cfg.streams_dir / _sanitize_name(stream) / _sanitize_name(topic)
        instance_dir.mkdir(parents=True, exist_ok=True)
        instance_toml = instance_dir / "instance.toml"
        if not instance_toml.exists():
            instance_toml.write_text(
                f'created_at = "{datetime.datetime.now().astimezone().isoformat()}"\n'
            )

        # 2. Create runtime directory + FIFO
        runtime.mkdir(parents=True, exist_ok=True)
        if fifo.exists():
            fifo.unlink()
        os.mkfifo(str(fifo))

        # Open sentinel fd to keep the FIFO pipe buffer alive.
        # Without this, adapter writes (O_WRONLY) fail with ENXIO before the
        # injector asyncio task starts. The sentinel acts as a reader so the
        # kernel accepts writes immediately. Data waits in the buffer until
        # the injector reads it.
        key = f"{stream}/{topic}"
        if key in self._sentinel_fds:
            try:
                os.close(self._sentinel_fds[key])
            except OSError:
                pass
            del self._sentinel_fds[key]
        try:
            self._sentinel_fds[key] = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            log.error("Failed to open sentinel fd for %s: %s", fifo, e)
            raise
        log.info("Created FIFO: %s (sentinel fd open)", fifo)

        # 3. Run ccmux-init on the project directory (idempotent)
        project_path = stream_cfg.project_path
        if project_path.is_dir() and CCMUX_INIT_SCRIPT.exists() and PYTHON.exists():
            caps_json = json.dumps(stream_cfg.capabilities) if stream_cfg.capabilities else "{}"
            try:
                result = subprocess.run(
                    [
                        str(PYTHON),
                        str(CCMUX_INIT_SCRIPT),
                        str(project_path),
                        "--capabilities",
                        caps_json,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    log.info("ccmux-init OK: %s", result.stdout.strip())
                else:
                    log.warning("ccmux-init failed: %s", result.stderr.strip())
            except subprocess.TimeoutExpired:
                log.warning("ccmux-init timed out for %s", project_path)

        # 4. Build env vars for the tmux session
        env_vars = _parse_env_template(self.cfg.env_template)
        # Set per-instance values
        env_vars["ZULIP_STREAM"] = stream
        env_vars["ZULIP_TOPIC"] = topic

        # 5. Kill old tmux session if it exists (lazy create guard)
        if _tmux_has_session(session):
            log.warning("Killing existing tmux session: %s", session)
            _tmux_kill_session(session)

        # 6. Create tmux session with env vars and Claude Code
        tmux_cmd = [
            "tmux", "new-session", "-d", "-s", session,
            "-c", str(project_path),
        ]
        for k, v in env_vars.items():
            tmux_cmd.extend(["-e", f"{k}={v}"])
        tmux_cmd.append("claude --dangerously-skip-permissions")

        try:
            result = subprocess.run(tmux_cmd, capture_output=True, timeout=10)
            if result.returncode != 0:
                log.error(
                    "tmux new-session failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.decode(errors="replace").strip(),
                )
                return None
        except subprocess.TimeoutExpired:
            log.error("tmux new-session timed out for %s", session)
            return None

        # 7. Get pane PID and write to PID file
        try:
            result = subprocess.run(
                [
                    "tmux", "display-message", "-t", session,
                    "-p", "#{pane_pid}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pid_str = result.stdout.strip()
            if pid_str:
                pf.write_text(pid_str)
                log.info("Instance PID: %s (session=%s)", pid_str, session)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("Failed to get pane PID: %s", e)

        # 8. Start FIFO injector as an asyncio task
        key = f"{stream}/{topic}"
        # Stop existing injector if any — clear pid_file first to prevent
        # the old injector's finally block from deleting the new PID file
        if key in self._injectors:
            self._injectors[key].pid_file = None  # Prevent stale cleanup
            self._injectors[key].stop()
        if key in self._injector_tasks:
            self._injector_tasks[key].cancel()

        injector = Injector(str(fifo), session, pid_file=str(pf))
        self._injectors[key] = injector
        self._injector_tasks[key] = asyncio.create_task(
            injector.run(), name=f"injector-{key}"
        )

        log.info("Instance ready: stream=%s topic=%s", stream, topic)
        return fifo

    def stop_all(self) -> None:
        """Stop all running injectors and close sentinel fds."""
        for key, injector in self._injectors.items():
            log.info("Stopping injector: %s", key)
            injector.stop()
        for key, task in self._injector_tasks.items():
            task.cancel()
        for key, fd in self._sentinel_fds.items():
            try:
                os.close(fd)
            except OSError:
                pass
        self._injectors.clear()
        self._injector_tasks.clear()
        self._sentinel_fds.clear()
