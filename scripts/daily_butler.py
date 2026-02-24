#!/usr/bin/env python3
"""
Daily butler — timed household management triggers.

Writes a butler channel message to the ccmux FIFO, waking Claude
to perform scheduled household duties (morning briefing, class
reminders, evening wrap-up, etc.).

Designed to run via cron at multiple times throughout the day.
Output channel: [butler]

Usage:
    python3 daily_butler.py <action>

Actions:
    morning_briefing   — 07:00 daily: weather, schedule, homework, health
    class_reminder     — 15 min before each class (dynamic)
    evening_wrapup     — 20:00 daily: summary, tomorrow prep
    message_scan       — periodic: pull and process new WhatsApp messages
    daily_reflection   — 23:00 daily: review day's work, generate reflection log

Environment:
    BUTLER_STATE_DIR   — directory for state files (default: data/household/butler/)
"""

import json
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path


# --- Configuration -----------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(
    os.environ.get(
        "BUTLER_STATE_DIR",
        str(PROJECT_ROOT / "data" / "household" / "butler"),
    )
)
FIFO_PATH = Path("/tmp/ccmux/in.butler")
TODAY = date.today()
TODAY_ISO = TODAY.isoformat()
NOW = datetime.now()
NOW_ISO = NOW.isoformat()


# --- State management -------------------------------------------------------

def load_last_scan_ts() -> str | None:
    """Return ISO timestamp of last message scan, or None."""
    state_file = STATE_DIR / "last_scan.json"
    if not state_file.exists():
        return None
    try:
        with open(state_file) as fh:
            data = json.load(fh)
        return data.get("last_scan_ts")
    except (json.JSONDecodeError, OSError):
        return None


def save_last_scan_ts(ts: str) -> None:
    """Persist the timestamp of this scan."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / "last_scan.json"
    with open(state_file, "w") as fh:
        json.dump({"last_scan_ts": ts}, fh)


# --- FIFO notification -------------------------------------------------------

def notify_ccmux(
    content: str, max_retries: int = 5, retry_delay: float = 2.0
) -> bool:
    """Write a butler channel message to the ccmux FIFO.

    Uses O_WRONLY|O_NONBLOCK. Retries with backoff when the daemon
    is not yet ready (ENXIO — no reader on the FIFO), which happens
    during boot when a timer fires before ccmux opens its FIFOs.
    """
    payload = json.dumps({
        "channel": "butler",
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

    for attempt in range(1, max_retries + 1):
        try:
            fd = os.open(str(FIFO_PATH), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, payload_bytes)
                print(f"  Notification sent to ccmux ({len(payload_bytes)} bytes)")
                return True
            finally:
                os.close(fd)
        except OSError as exc:
            if attempt < max_retries:
                print(
                    f"  FIFO write attempt {attempt}/{max_retries} failed "
                    f"({exc}), retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                print(
                    f"  WARNING: FIFO write failed after {max_retries} "
                    f"attempts (ccmux not running?): {exc}"
                )
                return False
    return False


# --- Actions -----------------------------------------------------------------

def morning_briefing() -> None:
    """Trigger morning briefing: weather, today's schedule, homework, health."""
    weekday = NOW.strftime("%A")
    content = (
        f"Morning briefing trigger. Today is {weekday}, {TODAY_ISO}. "
        "Actions: "
        "1) Check weather for Hong Kong and prepare clothing advice. "
        "2) Read data/household/family_context.jsonl for today's class schedule and activities. "
        "3) Check for homework due today or this week. "
        "4) Send a morning briefing message to the household group. "
        "5) Pull recent WhatsApp messages from monitored chats for context."
    )
    notify_ccmux(content)


def class_reminder() -> None:
    """Trigger class reminder check: look at today's schedule, send reminders."""
    content = (
        f"Class reminder check at {NOW.strftime('%H:%M')}. "
        "Actions: "
        "1) Read data/household/family_context.jsonl for today's class schedule. "
        "2) Check if any class starts within the next 20 minutes. "
        "3) If yes, send a reminder to the household group. "
        "4) If no upcoming class, do nothing."
    )
    notify_ccmux(content)


def evening_wrapup() -> None:
    """Trigger evening wrap-up: summary and tomorrow prep."""
    content = (
        f"Evening wrap-up trigger for {TODAY_ISO}. "
        "Actions: "
        "1) Health tracking: ask about kids' bowel movements if not yet reported today. "
        "2) Check if any homework is due tomorrow. "
        "3) Read tomorrow's schedule from family_context.jsonl. "
        "4) Send evening summary to household group: what to prepare for tomorrow. "
        "5) Pull and process any unread WhatsApp messages."
    )
    notify_ccmux(content)


def daily_reflection() -> None:
    """Trigger daily reflection: review the day's work and generate reflection log."""
    content = (
        f"Daily reflection trigger for {TODAY_ISO}. "
        "Actions: "
        "1) Review today's work: messages processed, tasks completed, agent tasks run. "
        "2) Identify what went well (timely responses, correct handling, good judgments). "
        "3) Identify mistakes and delays (missed messages, slow responses, wrong decisions). "
        "4) For each mistake, note a specific improvement (code fix, rule update, behavioral change). "
        "5) List new rules learned from admin corrections today. "
        "6) Preview tomorrow's agenda (pending items, scheduled reminders, follow-ups). "
        f"7) Write the reflection to data/daily_reflections/{TODAY_ISO}.md."
    )
    notify_ccmux(content)


def message_scan() -> None:
    """Trigger periodic message scan: pull new messages, update context."""
    last_ts = load_last_scan_ts()
    after_clause = f" Pull messages after {last_ts}." if last_ts else " Pull recent messages (no previous scan recorded)."
    content = (
        f"Periodic message scan at {NOW.strftime('%H:%M')}.{after_clause} "
        "Actions: "
        "1) Use list_messages with after parameter to efficiently pull only new messages. "
        "2) Scan household group, School community group, activity groups for relevant info. "
        "3) Update data/household/family_context.jsonl with any new learnings. "
        "4) If any message requires action (delivery notification, schedule change, etc.), act on it. "
        "5) Do NOT reply to messages unless they start with S3."
    )
    save_last_scan_ts(NOW_ISO)
    notify_ccmux(content)


# --- Main --------------------------------------------------------------------

ACTIONS = {
    "morning_briefing": morning_briefing,
    "class_reminder": class_reminder,
    "evening_wrapup": evening_wrapup,
    "message_scan": message_scan,
    "daily_reflection": daily_reflection,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ACTIONS:
        print(f"Usage: {sys.argv[0]} <{'|'.join(ACTIONS.keys())}>")
        sys.exit(1)

    action = sys.argv[1]
    print(f"[daily_butler] {NOW_ISO} action={action}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ACTIONS[action]()

    print("[daily_butler] Done.")


if __name__ == "__main__":
    main()
