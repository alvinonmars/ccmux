#!/usr/bin/env python3
"""Hook script called by Claude Code on hook events.

This script is invoked by the Claude binary for each registered hook event.
It reads hook data from stdin, processes the event, and notifies the daemon
via the control socket. Must be self-contained (stdlib only) to guarantee
portability regardless of venv state.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None  # type: ignore[assignment]  # Python < 3.11

_HOOK_ERROR_LOG_MAX_BYTES = 100_000  # 100KB self-truncation guard


def _log_error(sock_path: str, exc: Exception, payload: dict) -> None:
    """Log hook delivery failure to <runtime_dir>/hook_errors.log and stderr.

    Structured JSONL format; self-truncates at 100KB. stdlib only.
    """
    try:
        runtime_dir = Path(sock_path).parent
        log_path = runtime_dir / "hook_errors.log"

        entry = {
            "ts": int(time.time()),
            "error": str(exc),
            "type": type(exc).__name__,
            "sock_path": sock_path,
            "payload_type": payload.get("type", ""),
        }
        line = json.dumps(entry) + "\n"

        # Self-truncation: if existing file exceeds limit, overwrite
        try:
            if log_path.exists() and log_path.stat().st_size > _HOOK_ERROR_LOG_MAX_BYTES:
                log_path.write_text(line)
            else:
                with open(log_path, "a") as f:
                    f.write(line)
        except OSError:
            pass  # filesystem error — best effort

        # Also print to stderr for environments that capture it
        print(f"ccmux hook: {type(exc).__name__}: {exc}", file=sys.stderr)
    except Exception:
        pass  # _log_error must never raise


def _get_control_sock(cwd: str) -> str:
    """Resolve control socket path.

    Priority:
    1. CCMUX_CONTROL_SOCK environment variable (for testing and explicit override)
    2. ccmux.toml in cwd
    3. Default: /tmp/ccmux/control.sock
    """
    env_override = os.environ.get("CCMUX_CONTROL_SOCK")
    if env_override:
        return env_override
    try:
        toml_path = Path(cwd) / "ccmux.toml"
        if toml_path.exists():
            if tomllib is not None:
                with open(toml_path, "rb") as f:
                    data = tomllib.load(f)
                runtime_dir = data.get("runtime", {}).get("dir", "/tmp/ccmux")
            else:
                # Fallback for Python < 3.11: regex extraction
                text = toml_path.read_text()
                m = re.search(r'^\s*dir\s*=\s*"([^"]+)"', text, re.MULTILINE)
                runtime_dir = m.group(1) if m else "/tmp/ccmux"
            return str(Path(runtime_dir) / "control.sock")
    except Exception:
        pass
    return "/tmp/ccmux/control.sock"


def _read_last_assistant_turn(transcript_path: str) -> list[dict] | None:
    """Read the last assistant turn from the transcript JSONL file."""
    try:
        path = Path(transcript_path).expanduser()
        if not path.exists():
            return None
        last_turn: list[dict] | None = None
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    msg = record.get("message", {})
                    if msg.get("role") == "assistant":
                        last_turn = msg.get("content", [])
                except json.JSONDecodeError:
                    continue
        return last_turn
    except Exception:
        return None


def _send_to_control(sock_path: str, payload: dict) -> None:
    """Send a JSON payload to the daemon's control socket (best-effort)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock_path)
            s.sendall(json.dumps(payload).encode() + b"\n")
    except Exception as exc:
        _log_error(sock_path, exc, payload)  # Daemon may not be running; hook must never block Claude


def main() -> None:
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return

    event = hook_data.get("hook_event_name", "")
    session_id = hook_data.get("session_id", "")
    cwd = hook_data.get("cwd", os.getcwd())
    control_sock = _get_control_sock(cwd)

    if event == "Stop":
        transcript_path = hook_data.get("transcript_path", "")
        turn = _read_last_assistant_turn(transcript_path)
        if turn is None:
            # Fallback: use last_assistant_message plain text
            last_msg = hook_data.get("last_assistant_message", "")
            turn = [{"type": "text", "text": last_msg}]
        payload = {
            "type": "broadcast",
            "session": session_id,
            "turn": turn,
            "ts": int(__import__("time").time()),
        }
        _send_to_control(control_sock, payload)

    elif event == "PreCompact":
        # Context compaction is about to happen — inject recovery context
        # so Claude can restore operational state after the summary.
        # Runs the lightweight context recovery script in background.
        _trigger_context_recovery(cwd)
        payload = {
            "type": "event",
            "event": event,
            "session": session_id,
            "data": hook_data,
        }
        _send_to_control(control_sock, payload)

    elif event in (
        "SessionStart",
        "SubagentStart",
        "SubagentStop",
        "SessionEnd",
        "PermissionRequest",
        "UserPromptSubmit",
        "Notification",
    ):
        payload = {
            "type": "event",
            "event": event,
            "session": session_id,
            "data": hook_data,
        }
        _send_to_control(control_sock, payload)


def _trigger_context_recovery(cwd: str) -> None:
    """Run the context recovery script to re-inject state after compaction.

    Spawns startup_selfcheck.py as a subprocess. The script writes the
    recovery report and pushes it to the FIFO, so Claude's first message
    after compaction includes full operational context.

    stdlib only — uses subprocess.Popen (fire-and-forget, no waiting).
    """
    import subprocess

    script = Path(cwd) / "scripts" / "startup_selfcheck.py"
    venv_python = Path(cwd) / ".venv" / "bin" / "python3"

    if not script.exists():
        return

    python = str(venv_python) if venv_python.exists() else sys.executable

    try:
        subprocess.Popen(
            [python, str(script)],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # hook must never block Claude


if __name__ == "__main__":
    main()
