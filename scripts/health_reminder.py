#!/usr/bin/env python3
"""
Daily health reminder — bowel movement tracking.

Reads poo_log.jsonl, calculates days since last bowel movement,
and writes a reminder to the ccmux FIFO at /tmp/ccmux/in.health.

Designed to run via cron at 19:00 daily.
Output channel: [health]
"""

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path


# --- Configuration -----------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ccmux.paths import HEALTH_DIR

CHILD_NAME = os.environ.get("HEALTH_CHILD_NAME", "Child")
CHILD_DIR = os.environ.get("HEALTH_CHILD_DIR", "child")
POO_LOG = HEALTH_DIR / CHILD_DIR / "poo_log.jsonl"
FIFO_PATH = Path("/tmp/ccmux/in.health")
TODAY = date.today()
TODAY_ISO = TODAY.isoformat()
ALERT_THRESHOLD_DAYS = 3


# --- Log reading -------------------------------------------------------------

def get_last_poo_date() -> date | None:
    """Return the date of the last status='yes' entry, or None if no records."""
    if not POO_LOG.exists():
        return None

    last_yes: date | None = None
    with open(POO_LOG) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("status") == "yes" and entry.get("date"):
                try:
                    d = date.fromisoformat(entry["date"])
                    if last_yes is None or d > last_yes:
                        last_yes = d
                except ValueError:
                    continue
    return last_yes


# --- FIFO notification -------------------------------------------------------

def notify_ccmux(content: str) -> bool:
    """Write a health channel message to the ccmux FIFO.

    Uses O_WRONLY|O_NONBLOCK. Returns True if sent, False otherwise.
    """
    payload = json.dumps({
        "channel": "health",
        "content": content,
        "ts": int(time.time()),
    })
    payload_bytes = (payload + "\n").encode()

    if len(payload_bytes) > 4096:
        print(f"  WARNING: Payload {len(payload_bytes)} bytes exceeds PIPE_BUF")
        return False

    fifo_dir = FIFO_PATH.parent
    fifo_dir.mkdir(parents=True, exist_ok=True)
    if not FIFO_PATH.exists():
        os.mkfifo(str(FIFO_PATH))
        print(f"  Created FIFO: {FIFO_PATH}")

    try:
        fd = os.open(str(FIFO_PATH), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, payload_bytes)
            print(f"  Notification sent to ccmux ({len(payload_bytes)} bytes)")
            return True
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"  WARNING: FIFO write failed (ccmux not running?): {exc}")
        return False


# --- Main --------------------------------------------------------------------

def main() -> None:
    print(f"[health_reminder] {datetime.now().isoformat()}")

    last_poo = get_last_poo_date()

    if last_poo is None:
        days_since = None
        print("  No poo records found.")
        content = (
            f"Daily health check. No bowel movement records found for {CHILD_NAME} yet. "
            f"Ask the helper about {CHILD_NAME}'s poo today."
        )
    else:
        days_since = (TODAY - last_poo).days
        print(f"  Last poo: {last_poo.isoformat()} ({days_since} day(s) ago)")

        if days_since >= ALERT_THRESHOLD_DAYS:
            content = (
                f"ALERT: {CHILD_NAME} has not had a bowel movement in {days_since} days. "
                f"Last recorded: {last_poo.isoformat()}. Notify admin."
            )
        else:
            content = f"Daily health check. Ask about {CHILD_NAME}'s poo today."

    print(f"  Message: {content}")
    notify_ccmux(content)
    print("[health_reminder] Done.")


if __name__ == "__main__":
    main()
