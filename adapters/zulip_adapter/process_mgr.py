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
import enum
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import uuid
from pathlib import Path

from .config import StreamConfig, ZulipAdapterConfig
from .injector import Injector
from .transcript_watcher import TranscriptWatcher, ZulipPoster, discover_transcript

log = logging.getLogger(__name__)

CCMUX_INIT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "ccmux_init.py"
PYTHON = Path(__file__).resolve().parent.parent.parent / ".venv" / "bin" / "python3"

# Characters safe for tmux session names and directory names
_UNSAFE_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]")


class CreateMode(enum.Enum):
    """Result of instance creation/resumption."""

    NONE = "none"              # Instance already alive, no action taken
    FIRST_TIME = "first_time"  # Brand new instance, no prior session
    RESUMED = "resumed"        # Resumed from existing session JSONL
    FALLBACK = "fallback"      # Session JSONL missing, fresh start with history


def _read_instance_toml(path: Path) -> dict:
    """Parse instance.toml and return key-value dict.

    Returns empty dict on any parse error (corrupt file, missing file).
    """
    if not path.exists():
        return {}
    try:
        import sys
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            try:
                import tomllib  # type: ignore[import]
            except ModuleNotFoundError:
                import tomli as tomllib  # type: ignore[no-redef]
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        log.warning("Failed to parse instance.toml: %s", path)
        return {}


def _write_instance_toml(path: Path, data: dict) -> None:
    """Write instance.toml as simple key = value pairs."""
    lines = []
    for k, v in data.items():
        if isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    path.write_text("\n".join(lines) + "\n")


def _claude_session_dir(project_path: Path) -> Path:
    """Derive Claude Code's session storage directory for a project.

    Claude Code stores sessions at ~/.claude/projects/<hashed-path>/
    where <hashed-path> is the absolute project path with '/' replaced by '-'.
    """
    abs_path = str(project_path.resolve())
    hashed = abs_path.replace("/", "-")
    return Path.home() / ".claude" / "projects" / hashed


def _session_jsonl_exists(session_id: str, project_path: Path) -> bool:
    """Check if a Claude Code session JSONL file exists."""
    session_dir = _claude_session_dir(project_path)
    return (session_dir / f"{session_id}.jsonl").exists()


def _sanitize_name(name: str) -> str:
    """Sanitize a stream or topic name for tmux sessions and directory paths.

    Zulip topics can contain ':', '.', spaces, '()', '#', etc.
    tmux interprets ':' as window separator and '.' as pane separator.
    Replace unsafe chars with underscore, and append a hash suffix when
    characters were replaced to preserve uniqueness (e.g. Chinese topic names
    that would otherwise all collapse to underscores).
    """
    safe = _UNSAFE_CHARS_RE.sub("_", name)
    if safe != name:
        suffix = hashlib.sha256(name.encode()).hexdigest()[:8]
        safe = f"{safe}_{suffix}"
    return safe


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


def _claude_alive_in_session(session: str) -> bool:
    """Check if a Claude Code process is running inside the tmux session."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        pane_pid = result.stdout.strip()
        if not pane_pid:
            return False
        # Check if a `claude` process is a child of the pane shell
        pstree = subprocess.run(
            ["pstree", "-p", pane_pid],
            capture_output=True, text=True, timeout=5,
        )
        return "claude(" in pstree.stdout
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
        # Expand ~ to absolute home path — tmux -e does not expand tildes,
        # and subprocess environments may lack HOME, breaking expanduser()
        # inside hooks.
        if value.startswith("~/"):
            value = os.path.expanduser(value)
        env[key] = value

    return env


class ProcessManager:
    """Manages per-topic Claude Code instances."""

    def __init__(self, cfg: ZulipAdapterConfig):
        self.cfg = cfg
        self._injectors: dict[str, Injector] = {}  # "stream/topic" → Injector
        self._injector_tasks: dict[str, asyncio.Task] = {}
        self._watchers: dict[str, TranscriptWatcher] = {}  # "stream/topic" → watcher
        self._watcher_tasks: dict[str, asyncio.Task] = {}
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
    ) -> tuple[Path, CreateMode]:
        """Ensure instance is running. Lazy creates if needed.

        Returns (fifo_path, create_mode) where create_mode indicates what
        happened (NONE if already alive, or FIRST_TIME/RESUMED/FALLBACK).
        """
        fifo = _fifo_path(self.cfg, stream, topic)
        key = f"{stream}/{topic}"

        # Check if injector task has crashed (even if PID/tmux are alive)
        injector_dead = (
            key in self._injector_tasks
            and self._injector_tasks[key].done()
        )

        if self.is_alive(stream, topic) and fifo.exists() and not injector_dead:
            return fifo, CreateMode.NONE

        # Fallback: injector task running + tmux alive = instance is OK
        # (covers case where PID file write failed in _lazy_create step 7)
        if (
            not injector_dead
            and key in self._injector_tasks
            and fifo.exists()
            and _tmux_has_session(_tmux_session_name(stream, topic))
        ):
            log.debug("PID file missing but injector+tmux alive for %s", key)
            return fifo, CreateMode.NONE

        if injector_dead:
            log.warning("Injector task died for %s, recreating instance", key)

        result = await self._lazy_create(stream, topic, stream_cfg)
        if result is None:
            # tmux creation failed — return FIFO path but CreateMode.NONE
            # so caller doesn't send a false notification
            return fifo, CreateMode.NONE
        return result

    async def _lazy_create(
        self, stream: str, topic: str, stream_cfg: StreamConfig
    ) -> tuple[Path, CreateMode] | None:
        """Create a new instance: instance.toml, dirs, FIFO, tmux, injector, PID.

        Returns (fifo_path, create_mode) on success, None on failure.
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

        # Determine session mode: resume, fallback, or first-time
        toml_data = _read_instance_toml(instance_toml)
        existing_session_id = toml_data.get("session_id", "")

        if existing_session_id and _session_jsonl_exists(
            existing_session_id, stream_cfg.project_path
        ):
            create_mode = CreateMode.RESUMED
            session_id = existing_session_id
            log.info("Resuming session %s for %s/%s", session_id, stream, topic)
        elif existing_session_id:
            create_mode = CreateMode.FALLBACK
            session_id = str(uuid.uuid4())
            log.info(
                "Session JSONL missing for %s, fallback with new session %s",
                existing_session_id, session_id,
            )
        else:
            create_mode = CreateMode.FIRST_TIME
            session_id = str(uuid.uuid4())
            log.info("First-time session %s for %s/%s", session_id, stream, topic)

        # Write/update instance.toml with session_id
        toml_data["session_id"] = session_id
        if "created_at" not in toml_data:
            toml_data["created_at"] = datetime.datetime.now().astimezone().isoformat()
        _write_instance_toml(instance_toml, toml_data)

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
        env_vars["ZULIP_PROJECT_PATH"] = str(project_path)
        env_vars["ZULIP_SESSION_ID"] = session_id

        # 5. Handle existing tmux session
        # If Claude is alive inside an existing session, reuse it.
        # Killing + recreating triggers `claude --resume` which can compact
        # the conversation, causing Claude to stop writing to the original
        # JSONL file — breaking both transcript_watcher and Stop hook.
        reuse_existing = False
        if _tmux_has_session(session):
            if _claude_alive_in_session(session):
                log.info(
                    "Reusing existing tmux session: %s (Claude alive)", session
                )
                reuse_existing = True
                create_mode = CreateMode.NONE  # No user-visible change
            else:
                log.warning("Killing existing tmux session: %s", session)
                _tmux_kill_session(session)

        if not reuse_existing:
            # 6. Create tmux session with a shell, then send-keys to start Claude.
            # Running claude as the tmux session command causes it to exit
            # immediately (no interactive TTY from shell). Using send-keys
            # gives claude a proper interactive shell environment.
            tmux_cmd = [
                "tmux", "new-session", "-d", "-s", session,
                "-c", str(project_path),
            ]
            for k, v in env_vars.items():
                tmux_cmd.extend(["-e", f"{k}={v}"])

            try:
                result = subprocess.run(tmux_cmd, capture_output=True, timeout=10)
                if result.returncode != 0:
                    log.error(
                        "tmux new-session failed (rc=%d): %s",
                        result.returncode,
                        result.stderr.decode(errors="replace").strip(),
                    )
                    self._close_sentinel(key)
                    return None
            except subprocess.TimeoutExpired:
                log.error("tmux new-session timed out for %s", session)
                self._close_sentinel(key)
                return None

            # 6b. Send claude command via send-keys (resume or new session)
            # --strict-mcp-config: ignore project .mcp.json so Zulip instances
            # don't inherit WhatsApp/other MCP servers from the shared project dir.
            if create_mode == CreateMode.RESUMED:
                claude_cmd = f"claude --resume {session_id} --dangerously-skip-permissions --strict-mcp-config"
            else:
                claude_cmd = f"claude --session-id {session_id} --dangerously-skip-permissions --strict-mcp-config"
            try:
                subprocess.run(
                    ["tmux", "send-keys", "-t", session, claude_cmd, "Enter"],
                    capture_output=True,
                    timeout=5,
                )
            except subprocess.TimeoutExpired:
                log.warning("tmux send-keys timed out for %s", session)

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

        # 8. Discover transcript path (used by both injector and watcher)
        transcript_path = discover_transcript(project_path, session_id)

        # 9. Start FIFO injector as an asyncio task
        key = f"{stream}/{topic}"
        # Stop existing injector if any — clear pid_file first to prevent
        # the old injector's finally block from deleting the new PID file
        if key in self._injectors:
            self._injectors[key].pid_file = None  # Prevent stale cleanup
            self._injectors[key].stop()
        if key in self._injector_tasks:
            self._injector_tasks[key].cancel()

        injector = Injector(
            str(fifo), session, pid_file=str(pf),
            transcript_path=str(transcript_path) if transcript_path else None,
        )
        self._injectors[key] = injector
        self._injector_tasks[key] = asyncio.create_task(
            injector.run(), name=f"injector-{key}"
        )

        # 10. Start transcript watcher for real-time Zulip status updates
        if key in self._watchers:
            self._watchers[key].stop()
        if key in self._watcher_tasks:
            self._watcher_tasks[key].cancel()
        if transcript_path:
            poster = ZulipPoster(
                site=env_vars.get("ZULIP_SITE", ""),
                email=env_vars.get("ZULIP_BOT_EMAIL", ""),
                api_key=self._read_api_key(),
                stream=stream,
                topic=topic,
            )
            if poster.site:
                watcher = TranscriptWatcher(
                    transcript_path, poster,
                    project_path=project_path,
                    session_id=session_id,
                )
                self._watchers[key] = watcher
                self._watcher_tasks[key] = asyncio.create_task(
                    watcher.run(), name=f"watcher-{key}"
                )
                log.info("TranscriptWatcher started for %s (path=%s)", key, transcript_path)

        log.info("Instance ready: stream=%s topic=%s mode=%s", stream, topic, create_mode.value)
        return fifo, create_mode

    def _read_api_key(self) -> str:
        """Read Zulip bot API key from the credentials file."""
        cred_path = self.cfg.bot_credentials
        if not cred_path.exists():
            return ""
        try:
            for line in cred_path.read_text().splitlines():
                if line.startswith("ZULIP_BOT_API_KEY="):
                    value = line.split("=", 1)[1].strip()
                    if (
                        len(value) >= 2
                        and value[0] == value[-1]
                        and value[0] in ('"', "'")
                    ):
                        value = value[1:-1]
                    return value
        except OSError:
            pass
        return ""

    def _close_sentinel(self, key: str) -> None:
        """Close and remove sentinel fd for a key."""
        if key in self._sentinel_fds:
            try:
                os.close(self._sentinel_fds[key])
            except OSError:
                pass
            del self._sentinel_fds[key]

    def stop_all(self) -> None:
        """Stop all running injectors, watchers, and close sentinel fds."""
        for key, watcher in self._watchers.items():
            log.info("Stopping watcher: %s", key)
            watcher.stop()
        for key, task in self._watcher_tasks.items():
            task.cancel()
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
        self._watchers.clear()
        self._watcher_tasks.clear()
        self._injectors.clear()
        self._injector_tasks.clear()
        self._sentinel_fds.clear()
