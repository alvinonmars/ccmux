#!/usr/bin/env python3
"""
One-time announcement sender via ccmux FIFO.

Usage:
    python3 announcement.py <announcement_id>

Reads announcement content from data/household/butler/announcements/<id>.json
and writes it to the butler FIFO. The JSON file should have:
    {"target": "household_group", "message": "...", "sent": false}

After sending, marks "sent": true to prevent re-sending.
"""

import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANNOUNCEMENTS_DIR = PROJECT_ROOT / "data" / "household" / "butler" / "announcements"
FIFO_PATH = Path("/tmp/ccmux/in.butler")


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <announcement_id>")
        sys.exit(1)

    ann_id = sys.argv[1]
    ann_file = ANNOUNCEMENTS_DIR / f"{ann_id}.json"

    if not ann_file.exists():
        print(f"ERROR: Announcement file not found: {ann_file}")
        sys.exit(1)

    with open(ann_file) as fh:
        ann = json.load(fh)

    if ann.get("sent"):
        print(f"Announcement {ann_id} already sent. Skipping.")
        return

    content = (
        f"Announcement trigger: {ann_id}. "
        f"Target: {ann.get('target', 'household_group')}. "
        f"Send this message to the household group: {ann['message']}"
    )

    payload = json.dumps({
        "channel": "butler",
        "content": content,
        "ts": int(time.time()),
    })
    payload_bytes = (payload + "\n").encode()

    if not FIFO_PATH.exists():
        os.mkfifo(str(FIFO_PATH))

    try:
        fd = os.open(str(FIFO_PATH), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, payload_bytes)
            print(f"Announcement sent ({len(payload_bytes)} bytes)")
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"WARNING: FIFO write failed: {exc}")
        sys.exit(1)

    ann["sent"] = True
    ann["sent_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(ann_file, "w") as fh:
        json.dump(ann, fh, indent=2, ensure_ascii=False)
    print(f"Marked as sent: {ann_file}")


if __name__ == "__main__":
    main()
