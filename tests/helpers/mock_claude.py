#!/usr/bin/env python3
"""Mock Claude Code process for Zulip integration tests.

Runs inside tmux, simulating Claude Code's observable behavior:
- Startup: optional delay, then prints ❯ prompt
- On input: waits, prints reply, calls relay hook, reprints prompt
- Crash simulation: exits after N turns
- Hang simulation: never responds

Environment variables:
  MOCK_CLAUDE_STARTUP_DELAY  Seconds before showing first prompt (default: 0.2)
  MOCK_CLAUDE_DELAY          Seconds before outputting reply (default: 0.1)
  MOCK_CLAUDE_REPLY          Reply text per turn (default: "mock reply")
  MOCK_CLAUDE_CRASH_AFTER    Exit after N turns (default: 0 = never crash)
  MOCK_CLAUDE_NO_REPLY       If "1", never respond (hang simulation)
  MOCK_HOOK_SCRIPT           Path to relay hook script (called with Stop hook JSON)
  MOCK_TRANSCRIPT            Path to transcript file (appends JSONL per turn)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def call_relay_hook(reply: str) -> None:
    """Call the relay hook script with Stop hook JSON, mimicking Claude Code."""
    hook_script = _env("MOCK_HOOK_SCRIPT")
    if not hook_script:
        return

    data = {
        "hook_event_name": "Stop",
        "session_id": f"mock-{os.getpid()}",
        "transcript_path": _env("MOCK_TRANSCRIPT", ""),
        "cwd": os.getcwd(),
        "permission_mode": "default",
        "last_assistant_message": reply,
    }
    try:
        subprocess.run(
            [sys.executable, hook_script],
            input=json.dumps(data).encode(),
            timeout=10.0,
            capture_output=True,
        )
    except Exception as e:
        print(f"mock_claude: hook call failed: {e}", file=sys.stderr)


def append_transcript(reply: str) -> None:
    """Append a JSONL line to the transcript file."""
    transcript_path = _env("MOCK_TRANSCRIPT")
    if not transcript_path:
        return
    record = {
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": reply}],
        },
        "ts": int(time.time()),
    }
    try:
        with open(transcript_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main() -> None:
    startup_delay = _env_float("MOCK_CLAUDE_STARTUP_DELAY", 0.2)
    reply_delay = _env_float("MOCK_CLAUDE_DELAY", 0.1)
    reply_text = _env("MOCK_CLAUDE_REPLY", "mock reply")
    crash_after = _env_int("MOCK_CLAUDE_CRASH_AFTER", 0)
    no_reply = _env("MOCK_CLAUDE_NO_REPLY", "") == "1"

    # Startup delay (simulates Claude loading)
    if startup_delay > 0:
        time.sleep(startup_delay)

    # Show initial prompt
    emit("❯ ")

    turn = 0
    while True:
        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break

        if no_reply:
            # Hang simulation: read input but never respond
            continue

        time.sleep(reply_delay)
        turn += 1

        # Output reply
        emit(reply_text + "\n")

        # Append to transcript
        append_transcript(reply_text)

        # Call relay hook (mimics Claude's Stop hook)
        call_relay_hook(reply_text)

        # Check crash condition
        if crash_after > 0 and turn >= crash_after:
            emit("mock_claude: simulated crash\n")
            sys.exit(1)

        # Show prompt again
        emit("❯ ")


if __name__ == "__main__":
    main()
