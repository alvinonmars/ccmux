#!/usr/bin/env python3
"""Bridge: buffer voice transcription fragments, write aggregated utterances to ccmux.

The WebRTC relay writes one sentence per line to a FIFO. This bridge:
1. Reads sentences from the relay FIFO
2. Buffers until silence (no new input for SILENCE_TIMEOUT seconds)
3. Writes the aggregated utterance as JSON to the ccmux input FIFO

Environment variables:
    RELAY_FIFO_PATH     Source FIFO (WebRTC relay writes here)  [/tmp/ccmux/relay.voice]
    CCMUX_RUNTIME_DIR   ccmux runtime directory                 [/tmp/ccmux]
    VOICE_SILENCE_SEC   Seconds of silence = utterance complete  [2.0]
    VOICE_RAW_LOG       Raw transcript log (tail -f to inspect)  [~/.ccmux/data/voice/raw.log]
    VOICE_PROMPT_FILE   Load prompt from file instead of default  [empty = use built-in]
"""
from __future__ import annotations

import errno
import json
import os
import re
import select
import stat
import sys
import time
from pathlib import Path

RELAY_FIFO = os.environ.get("RELAY_FIFO_PATH", "/tmp/ccmux/relay.voice")
CCMUX_DIR = Path(os.environ.get("CCMUX_RUNTIME_DIR", "/tmp/ccmux"))
CCMUX_FIFO = CCMUX_DIR / "in.voice"
SILENCE_TIMEOUT = float(os.environ.get("VOICE_SILENCE_SEC", "2.0"))
RAW_LOG = Path(os.environ.get("VOICE_RAW_LOG", os.path.expanduser(
    "~/.ccmux/data/voice/raw.log")))

# Filter out known test/idle audio patterns (model default output when no real speech)
_NOISE_RE = re.compile(r"如果说想去看美丽的风景|像之前电影当中")

_DEFAULT_PROMPT = (
    "[Real-time voice transcription from phone microphone. "
    "Speech-to-text may contain recognition errors or incomplete phrases. "
    "Interpret intent from context, not literal words. "
    "Respond concisely — the user is speaking, not typing.]"
)

_prompt_file = os.environ.get("VOICE_PROMPT_FILE", "")


def _load_prompt() -> str:
    """Load prompt from file if VOICE_PROMPT_FILE is set, else use default."""
    if _prompt_file and os.path.isfile(_prompt_file):
        with open(_prompt_file, encoding="utf-8") as f:
            return f.read().strip()
    return _DEFAULT_PROMPT


VOICE_PROMPT = _load_prompt()


def _log_raw(line: str) -> None:
    """Append a raw transcript line with timestamp to the log file."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    RAW_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RAW_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")


def _ensure_fifo(path: str | Path) -> None:
    """Create FIFO if it does not exist."""
    p = Path(path)
    if p.exists():
        if not stat.S_ISFIFO(p.stat().st_mode):
            print(f"[voice-bridge] ERROR: {p} exists but is not a FIFO", flush=True)
            sys.exit(1)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(str(p))
    print(f"[voice-bridge] Created FIFO: {p}", flush=True)


def _write_to_ccmux(text: str) -> None:
    """Write aggregated utterance to ccmux input FIFO as JSON."""
    content = f"{VOICE_PROMPT}\n\n{text}"
    payload = json.dumps({
        "channel": "voice",
        "content": content,
        "ts": int(time.time()),
    })
    try:
        fd = os.open(str(CCMUX_FIFO), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (payload + "\n").encode("utf-8"))
            print(f"[voice-bridge] Injected: {text!r}", flush=True)
        finally:
            os.close(fd)
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            print(f"[voice-bridge] ccmux not ready (no reader): {e}", flush=True)
        else:
            print(f"[voice-bridge] Write error: {e}", flush=True)


def _cleanup_fifo() -> None:
    """Remove the ccmux FIFO on shutdown."""
    try:
        if CCMUX_FIFO.exists():
            CCMUX_FIFO.unlink()
            print(f"[voice-bridge] Removed FIFO: {CCMUX_FIFO}", flush=True)
    except OSError as e:
        print(f"[voice-bridge] Failed to remove FIFO: {e}", flush=True)


def main() -> None:
    _ensure_fifo(RELAY_FIFO)
    _ensure_fifo(CCMUX_FIFO)
    print(
        f"[voice-bridge] Listening: {RELAY_FIFO} -> {CCMUX_FIFO} "
        f"(silence={SILENCE_TIMEOUT}s)",
        flush=True,
    )

    buffer: list[str] = []
    remainder = ""

    try:
        # O_RDWR prevents EOF when all writers close (same trick as ccmux daemon).
        # The relay server opens/writes/closes per sentence — O_RDONLY would get
        # EOF after every single sentence, defeating buffering.
        fd = os.open(RELAY_FIFO, os.O_RDWR | os.O_NONBLOCK)
        print("[voice-bridge] FIFO open (O_RDWR), waiting for input...", flush=True)

        while True:
            ready, _, _ = select.select([fd], [], [], SILENCE_TIMEOUT)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                if data:
                    text = remainder + data.decode("utf-8", errors="replace")
                    lines = text.split("\n")
                    # Last element is incomplete (no trailing newline) — keep as remainder
                    remainder = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if line:
                            _log_raw(line)
                            buffer.append(line)
            else:
                # Silence timeout — flush buffer as one utterance
                if buffer:
                    aggregated = " ".join(buffer)
                    if _NOISE_RE.search(aggregated):
                        print(f"[voice-bridge] Filtered noise: {aggregated!r}", flush=True)
                    else:
                        _write_to_ccmux(aggregated)
                    buffer.clear()

    except KeyboardInterrupt:
        print("\n[voice-bridge] Shutting down", flush=True)
        if buffer:
            aggregated = " ".join(buffer)
            if not _NOISE_RE.search(aggregated):
                _write_to_ccmux(aggregated)
    finally:
        _cleanup_fifo()


if __name__ == "__main__":
    main()
