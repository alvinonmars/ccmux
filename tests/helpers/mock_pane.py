#!/usr/bin/env python3
"""mock_pane — configurable I/O process that runs in a tmux pane.

Simulates Claude Code's observable I/O patterns (not its intelligence).
All behavior is controlled via environment variables.

Environment variables:
  MOCK_PROMPT            Prompt string when waiting (default: ❯ )
  MOCK_REPLY             Reply text per turn (default: mock reply)
  MOCK_DELAY             Seconds before outputting reply (default: 0.1)
  MOCK_TRANSCRIPT        Transcript file path; appends JSONL line after each reply
  MOCK_HOOK_SCRIPT       Hook script path; called after each reply with Stop hook JSON
  MOCK_SPINNER           If > 0, emit N spinner sequences before reply
  MOCK_CONTINUOUS_SPINNER If > 0, emit spinner sequences indefinitely until input arrives
  MOCK_PERMISSION_INTERVAL If > 0, every N turns: output permission prompt, call hook with
                           PermissionRequest JSON, wait for next stdin
"""
import json
import os
import subprocess
import sys
import time

SPINNER_SEQ = "\x1b[?2026l\x1b[?2026h✻ "


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(key, default))
    except ValueError:
        return default


def emit(text: str, flush: bool = True) -> None:
    sys.stdout.write(text)
    if flush:
        sys.stdout.flush()


def call_hook(event: str, session_id: str, extra: dict | None = None) -> None:
    hook_script = _env("MOCK_HOOK_SCRIPT")
    if not hook_script:
        return
    transcript_path = _env("MOCK_TRANSCRIPT")
    data = {
        "hook_event_name": event,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": os.getcwd(),
        "permission_mode": "default",
    }
    if extra:
        data.update(extra)
    try:
        subprocess.run(
            [sys.executable, hook_script],
            input=json.dumps(data).encode(),
            timeout=5.0,
        )
    except Exception:
        pass


def append_transcript(reply: str) -> None:
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
    with open(transcript_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> None:
    prompt = _env("MOCK_PROMPT", "❯ ")
    reply = _env("MOCK_REPLY", "mock reply")
    delay = _env_float("MOCK_DELAY", 0.1)
    spinner_count = _env_int("MOCK_SPINNER", 0)
    continuous_spinner = _env_int("MOCK_CONTINUOUS_SPINNER", 0)
    permission_interval = _env_int("MOCK_PERMISSION_INTERVAL", 0)

    session_id = f"mock-{os.getpid()}"
    turn = 0

    # Make stdin non-blocking for continuous spinner detection
    emit(prompt)

    while True:
        # Wait for input
        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break

        time.sleep(delay)

        # Continuous spinner: emit until we detect silence then stop
        # (simulated: emit for a fixed burst, then stop)
        if continuous_spinner:
            # Emit spinner continuously in a tight loop
            # Stop when no more input is pending (simulate generating state)
            stop_time = time.time() + 999999  # run "forever" until interrupted
            # We use a flag file to signal stop (set by test), or just
            # emit until next stdin. Actually: emit until we get input.
            # But we're in a sync readline() loop. Use a thread approach:
            import threading
            stop_event = threading.Event()

            def spinner_thread():
                while not stop_event.is_set():
                    emit(SPINNER_SEQ)
                    time.sleep(0.1)

            t = threading.Thread(target=spinner_thread, daemon=True)
            t.start()
            # Wait for next input line (this is the "next turn" signal)
            # In the continuous spinner test, we don't expect another input;
            # the test just checks that ready event does NOT fire while spinning.
            # For simplicity, emit spinner for 10s then stop.
            time.sleep(10)
            stop_event.set()
            t.join(timeout=1.0)
        elif spinner_count > 0:
            for _ in range(spinner_count):
                emit(SPINNER_SEQ)
                time.sleep(0.1)

        turn += 1

        # Check if this is a permission turn
        if permission_interval > 0 and turn % permission_interval == 0:
            emit("Allow this action? Yes/No ")
            call_hook(
                "PermissionRequest",
                session_id,
                extra={"last_assistant_message": "Allow this action? Yes/No"},
            )
            # Wait for human to resolve permission
            try:
                sys.stdin.readline()
            except (EOFError, KeyboardInterrupt):
                pass
            emit("\n")

        emit(reply + "\n")
        append_transcript(reply)
        call_hook("Stop", session_id)
        emit(prompt)


if __name__ == "__main__":
    main()
