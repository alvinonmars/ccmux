#!/usr/bin/env python3
"""Hook script called by Claude Code on hook events.

This script is invoked by the Claude binary for each registered hook event.
It reads hook data from stdin, processes the event, and notifies the daemon
via the control socket. Must be self-contained (stdlib only) to guarantee
portability regardless of venv state.
"""
import json
import os
import socket
import sys
import tomllib
from pathlib import Path


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
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            runtime_dir = data.get("runtime", {}).get("dir", "/tmp/ccmux")
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
    except Exception:
        pass  # Daemon may not be running; hook must never block Claude


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


if __name__ == "__main__":
    main()
