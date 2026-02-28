#!/usr/bin/env python3
"""Bridge: buffer voice transcription fragments, forward and batch to ccmux.

Dual-channel design:
  Channel 1 (instant): Each sentence → WhatsApp immediately via REST API
  Channel 2 (batched): Sentences collected into batch → ccmux FIFO for Claude analysis

Environment variables:
    RELAY_FIFO_PATH     Source FIFO (WebRTC relay writes here)  [/tmp/ccmux/relay.voice]
    CCMUX_RUNTIME_DIR   ccmux runtime directory                 [/tmp/ccmux]
    VOICE_SILENCE_SEC   Seconds of silence = sentence boundary  [1.0]
    VOICE_BATCH_SEC     Seconds to collect sentences per batch   [10.0]
    VOICE_BATCH_MAX     Max sentences before force flush         [20]
    VOICE_RAW_LOG       Raw transcript log (tail -f to inspect)  [~/.ccmux/data/voice/raw.log]
    VOICE_PROMPT_FILE   Load prompt from file instead of default [empty = use built-in]
    WA_API_URL          WhatsApp bridge REST API                 [http://127.0.0.1:8080/api/send]
    WA_ADMIN_JID        Admin JID for instant forwarding         [required]
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
import urllib.request
import urllib.error
from pathlib import Path

RELAY_FIFO = os.environ.get("RELAY_FIFO_PATH", "/tmp/ccmux/relay.voice")
CCMUX_DIR = Path(os.environ.get("CCMUX_RUNTIME_DIR", "/tmp/ccmux"))
CCMUX_FIFO = CCMUX_DIR / "in.voice"
SILENCE_TIMEOUT = float(os.environ.get("VOICE_SILENCE_SEC", "1.0"))
BATCH_WINDOW = float(os.environ.get("VOICE_BATCH_SEC", "10.0"))
MAX_BATCH_LINES = int(os.environ.get("VOICE_BATCH_MAX", "20"))
RAW_LOG = Path(os.environ.get("VOICE_RAW_LOG", os.path.expanduser(
    "~/.ccmux/data/voice/raw.log")))
WA_API_URL = os.environ.get("WA_API_URL", "http://127.0.0.1:8080/api/send")
WA_ADMIN_JID = os.environ.get("WA_ADMIN_JID", "")

# Filter out known test/idle audio patterns (model default output when no real speech)
_NOISE_RE = re.compile(r"如果说想去看美丽的风景|像之前电影当中")

_DEFAULT_PROMPT = (
    "[Real-time voice transcription from phone microphone. "
    "Speech-to-text may contain recognition errors or incomplete phrases. "
    "Interpret intent from context, not literal words. "
    "Respond concisely — the user is speaking, not typing. "
    "ALWAYS forward the raw transcription to admin WhatsApp self-chat immediately.]"
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


def _send_wa_instant(text: str) -> None:
    """Send raw transcription to admin WhatsApp immediately via REST API."""
    payload = json.dumps({
        "recipient": WA_ADMIN_JID,
        "message": f"🎙️ {text}",
    }).encode("utf-8")
    req = urllib.request.Request(
        WA_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3):
            pass
    except (urllib.error.URLError, OSError) as e:
        print(f"[voice-bridge] WA send failed: {e}", flush=True)


def _write_to_ccmux(text: str) -> None:
    """Write batched utterances to ccmux input FIFO as JSON."""
    payload = json.dumps({
        "channel": "voice",
        "content": text,
        "ts": int(time.time()),
    })
    try:
        fd = os.open(str(CCMUX_FIFO), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (payload + "\n").encode("utf-8"))
            print(f"[voice-bridge] Injected batch ({text.count(chr(10))+1} lines)",
                  flush=True)
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
        f"(silence={SILENCE_TIMEOUT}s, batch={BATCH_WINDOW}s, max={MAX_BATCH_LINES})",
        flush=True,
    )
    print(f"[voice-bridge] Instant WA forward: {WA_API_URL} -> {WA_ADMIN_JID}",
          flush=True)

    buffer: list[str] = []       # current sentence fragments
    batch: list[str] = []        # collected sentences for batch
    batch_start: float | None = None
    remainder = ""

    try:
        fd = os.open(RELAY_FIFO, os.O_RDWR | os.O_NONBLOCK)
        print("[voice-bridge] FIFO open (O_RDWR), waiting for input...", flush=True)

        while True:
            # Calculate select timeout
            timeout = SILENCE_TIMEOUT
            if batch_start is not None:
                remaining = BATCH_WINDOW - (time.time() - batch_start)
                if remaining <= 0:
                    # Batch window expired — flush to ccmux
                    if batch:
                        _write_to_ccmux("\n".join(batch))
                        batch.clear()
                    batch_start = None
                else:
                    timeout = min(timeout, remaining)

            ready, _, _ = select.select([fd], [], [], timeout)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                if data:
                    text = remainder + data.decode("utf-8", errors="replace")
                    lines = text.split("\n")
                    remainder = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if line:
                            _log_raw(line)
                            buffer.append(line)
            else:
                # Silence timeout — sentence complete
                if buffer:
                    aggregated = " ".join(buffer)
                    if _NOISE_RE.search(aggregated):
                        print(f"[voice-bridge] Filtered noise: {aggregated!r}",
                              flush=True)
                    else:
                        # Channel 1: instant WhatsApp forward
                        _send_wa_instant(aggregated)
                        # Channel 2: add to batch for Claude
                        batch.append(aggregated)
                        if batch_start is None:
                            batch_start = time.time()
                    buffer.clear()

                # Check batch flush conditions
                if batch and (
                    len(batch) >= MAX_BATCH_LINES
                    or (batch_start and time.time() - batch_start >= BATCH_WINDOW)
                ):
                    _write_to_ccmux("\n".join(batch))
                    batch.clear()
                    batch_start = None

    except KeyboardInterrupt:
        print("\n[voice-bridge] Shutting down", flush=True)
        if buffer:
            aggregated = " ".join(buffer)
            if not _NOISE_RE.search(aggregated):
                batch.append(aggregated)
        if batch:
            _write_to_ccmux("\n".join(batch))
    finally:
        _cleanup_fifo()


if __name__ == "__main__":
    main()
