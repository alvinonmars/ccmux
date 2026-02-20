"""Unit tests for ccmux.hook — transcript reading and control socket messaging."""
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest


def _run_hook(
    hook_script: Path,
    data: dict,
    control_sock: Path | None = None,
) -> tuple[int, bytes, bytes]:
    """Run hook.py with given data as stdin. Returns (returncode, stdout, stderr)."""
    import subprocess
    env = None
    if control_sock is not None:
        env = {**os.environ, "CCMUX_CONTROL_SOCK": str(control_sock)}
    result = subprocess.run(
        [sys.executable, str(hook_script)],
        input=json.dumps(data).encode(),
        capture_output=True,
        timeout=5.0,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


HOOK_SCRIPT = Path(__file__).parent.parent.parent / "ccmux" / "hook.py"


def test_hook_handles_invalid_json():
    """hook.py must not crash on invalid stdin."""
    import subprocess
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=b"{invalid",
        capture_output=True,
        timeout=5.0,
    )
    assert result.returncode == 0


def test_hook_reads_transcript_and_sends_broadcast(tmp_path):
    """Stop hook: reads transcript, sends broadcast to control socket."""
    # Write a minimal transcript
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello from assistant"}],
            },
            "ts": 1700000000,
        }) + "\n"
    )

    sock_path = tmp_path / "control.sock"
    received: list[dict] = []

    def server():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(str(sock_path))
            s.listen(1)
            conn, _ = s.accept()
            with conn:
                data = conn.recv(4096)
                if data:
                    received.append(json.loads(data.strip()))

    t = threading.Thread(target=server, daemon=True)
    t.start()
    time.sleep(0.1)  # let server bind

    data = {
        "hook_event_name": "Stop",
        "session_id": "sess123",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "permission_mode": "default",
    }
    rc, _, _ = _run_hook(HOOK_SCRIPT, data, control_sock=sock_path)
    assert rc == 0

    t.join(timeout=3.0)
    assert len(received) == 1
    msg = received[0]
    assert msg["type"] == "broadcast"
    assert msg["session"] == "sess123"
    assert any(b.get("text") == "hello from assistant" for b in msg["turn"])


def test_hook_fallback_when_transcript_missing(tmp_path):
    """When transcript file doesn't exist, falls back to last_assistant_message."""
    sock_path = tmp_path / "control.sock"
    received: list[dict] = []

    def server():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(str(sock_path))
            s.listen(1)
            conn, _ = s.accept()
            with conn:
                data = conn.recv(4096)
                if data:
                    received.append(json.loads(data.strip()))

    t = threading.Thread(target=server, daemon=True)
    t.start()
    time.sleep(0.1)

    data = {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "transcript_path": str(tmp_path / "nonexistent.jsonl"),
        "last_assistant_message": "fallback text",
        "cwd": str(tmp_path),
        "permission_mode": "default",
    }
    rc, _, _ = _run_hook(HOOK_SCRIPT, data, control_sock=sock_path)
    assert rc == 0

    t.join(timeout=3.0)
    assert len(received) == 1
    assert received[0]["turn"][0]["text"] == "fallback text"


def test_hook_sends_event_for_permission_request(tmp_path):
    """PermissionRequest event is forwarded to control socket."""
    sock_path = tmp_path / "control.sock"
    received: list[dict] = []

    def server():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(str(sock_path))
            s.listen(1)
            conn, _ = s.accept()
            with conn:
                data = conn.recv(4096)
                if data:
                    received.append(json.loads(data.strip()))

    t = threading.Thread(target=server, daemon=True)
    t.start()
    time.sleep(0.1)

    data = {
        "hook_event_name": "PermissionRequest",
        "session_id": "s2",
        "cwd": str(tmp_path),
        "permission_mode": "default",
    }
    rc, _, _ = _run_hook(HOOK_SCRIPT, data, control_sock=sock_path)
    assert rc == 0

    t.join(timeout=3.0)
    assert len(received) == 1
    msg = received[0]
    assert msg["type"] == "event"
    assert msg["event"] == "PermissionRequest"


def test_hook_silently_succeeds_when_daemon_not_running(tmp_path):
    """When control socket doesn't exist, hook exits cleanly (no crash)."""
    data = {
        "hook_event_name": "Stop",
        "session_id": "s3",
        "transcript_path": str(tmp_path / "none.jsonl"),
        "last_assistant_message": "hi",
        "cwd": str(tmp_path),  # no ccmux.toml here → defaults to /tmp/ccmux/control.sock
        "permission_mode": "default",
    }
    rc, stdout, stderr = _run_hook(HOOK_SCRIPT, data)
    assert rc == 0  # must not crash
