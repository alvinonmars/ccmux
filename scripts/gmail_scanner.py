#!/usr/bin/env python3
"""Gmail IMAP scanner — fetches new emails and notifies ccmux.

Connects to Gmail via IMAP using App Password credentials stored in
~/.ccmux/secrets/gmail.env. Fetches emails received since the last scan,
extracts subject/sender/date/body, and writes a summary to the ccmux
FIFO so Claude can analyze and act on actionable items.

Credentials file format (gmail.env):
    GMAIL_ADDRESS=user@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

Scan state: ~/.ccmux/data/household/tmp/gmail_scan/last_scan.json

Usage:
    .venv/bin/python scripts/gmail_scanner.py
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ccmux.paths import GMAIL_ENV, GMAIL_SCAN_DIR

SCAN_STATE_PATH = GMAIL_SCAN_DIR / "last_scan.json"
FIFO_PATH = Path("/tmp/ccmux/in.gmail")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Max emails to fetch per scan to avoid overwhelming Claude
MAX_EMAILS_PER_SCAN = 20
# Max body length per email (chars) to keep FIFO payload reasonable
MAX_BODY_LENGTH = 1000


def load_credentials() -> tuple[str, str]:
    """Load Gmail credentials from env file."""
    if not GMAIL_ENV.exists():
        print(f"  ERROR: Credentials file not found: {GMAIL_ENV}")
        sys.exit(1)

    creds = {}
    with open(GMAIL_ENV) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                creds[key.strip()] = value.strip()

    addr = creds.get("GMAIL_ADDRESS", "")
    pwd = creds.get("GMAIL_APP_PASSWORD", "")
    if not addr or not pwd:
        print("  ERROR: GMAIL_ADDRESS or GMAIL_APP_PASSWORD missing in gmail.env")
        sys.exit(1)

    return addr, pwd


def load_last_scan() -> dict | None:
    """Load the last scan state from disk."""
    if not SCAN_STATE_PATH.exists():
        return None
    try:
        with open(SCAN_STATE_PATH) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def save_scan_state(timestamp: str, email_count: int, last_uid: str) -> None:
    """Persist the current scan state."""
    state = {
        "last_scan": timestamp,
        "email_count": email_count,
        "last_uid": last_uid,
    }
    SCAN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SCAN_STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=2)


def decode_header_value(raw: str | None) -> str:
    """Decode an email header value (handles RFC 2047 encoded words)."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def extract_text_body(msg: Message) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback to HTML if no plain text
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/html" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    # Strip HTML tags for a rough text extraction
                    text = re.sub(r"<[^>]+>", " ", html)
                    text = re.sub(r"\s+", " ", text).strip()
                    return text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def format_sender(raw_from: str) -> str:
    """Extract a clean sender name/address from the From header."""
    name, addr = email.utils.parseaddr(raw_from)
    if name:
        return f"{name} <{addr}>"
    return addr


def fetch_emails(
    addr: str, pwd: str, since_date: datetime
) -> list[dict]:
    """Connect to Gmail IMAP and fetch emails since the given date."""
    # IMAP SINCE uses date only (no time), format: DD-Mon-YYYY
    imap_since = since_date.strftime("%d-%b-%Y")

    print(f"  Connecting to {IMAP_HOST}...")
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(addr, pwd)
        print("  Login successful")

        conn.select("INBOX", readonly=True)

        # Search for emails since the date
        status, msg_nums = conn.search(None, f'(SINCE "{imap_since}")')
        if status != "OK" or not msg_nums[0]:
            print("  No new emails found")
            return []

        ids = msg_nums[0].split()
        print(f"  Found {len(ids)} email(s) since {imap_since}")

        # Take only the most recent N
        if len(ids) > MAX_EMAILS_PER_SCAN:
            ids = ids[-MAX_EMAILS_PER_SCAN:]
            print(f"  Limiting to {MAX_EMAILS_PER_SCAN} most recent")

        results = []
        for msg_id in ids:
            status, data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = decode_header_value(msg.get("Subject"))
            from_addr = decode_header_value(msg.get("From"))
            date_str = msg.get("Date", "")
            msg_date = email.utils.parsedate_to_datetime(date_str) if date_str else None

            # Skip emails before our actual timestamp (IMAP SINCE is date-only)
            if msg_date and msg_date < since_date:
                continue

            body = extract_text_body(msg)
            if len(body) > MAX_BODY_LENGTH:
                body = body[:MAX_BODY_LENGTH] + "..."

            results.append({
                "uid": msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
                "from": format_sender(from_addr),
                "subject": subject,
                "date": msg_date.isoformat() if msg_date else date_str,
                "body_preview": body,
            })

        return results

    finally:
        try:
            conn.logout()
        except Exception:
            pass


def notify_ccmux(emails: list[dict], scan_time: str) -> bool:
    """Write Gmail scan result to ccmux FIFO."""
    if not emails:
        content = f"Gmail scan at {scan_time}: no new emails."
    else:
        lines = [f"Gmail scan complete. {len(emails)} new email(s):"]
        for i, em in enumerate(emails, 1):
            lines.append(f"\n--- Email {i} ---")
            lines.append(f"From: {em['from']}")
            lines.append(f"Subject: {em['subject']}")
            lines.append(f"Date: {em['date']}")
            lines.append(f"Body preview:\n{em['body_preview']}")
        lines.append(
            "\nPlease review these emails. Forward actionable items to admin "
            "via self-chat. Summarize key information."
        )
        content = "\n".join(lines)

    payload = json.dumps({
        "channel": "gmail",
        "content": content,
        "ts": int(time.time()),
    })
    payload_bytes = (payload + "\n").encode()

    fifo_dir = FIFO_PATH.parent
    fifo_dir.mkdir(parents=True, exist_ok=True)
    if not FIFO_PATH.exists():
        os.mkfifo(str(FIFO_PATH))

    try:
        fd = os.open(str(FIFO_PATH), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, payload_bytes)
            print(f"  Notification sent ({len(payload_bytes)} bytes)")
            return True
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"  FIFO write failed (ccmux not running?): {exc}")
        return False


def main() -> None:
    timestamp = datetime.now(tz=timezone(timedelta(hours=8)))
    print(f"[gmail_scanner] {timestamp.isoformat()}")

    GMAIL_SCAN_DIR.mkdir(parents=True, exist_ok=True)

    addr, pwd = load_credentials()
    print(f"  Account: {addr}")

    # Determine scan window
    last_scan = load_last_scan()
    if last_scan and last_scan.get("last_scan"):
        since = datetime.fromisoformat(last_scan["last_scan"])
        print(f"  Last scan: {last_scan['last_scan']} ({last_scan.get('email_count', '?')} emails)")
    else:
        # First run: check last 24 hours
        since = timestamp - timedelta(hours=24)
        print("  First scan (no previous state). Checking last 24 hours.")

    print(f"  Scan window: {since.isoformat()} to {timestamp.isoformat()}")

    # Fetch emails
    emails = fetch_emails(addr, pwd, since)
    print(f"  Fetched {len(emails)} email(s)")

    # Save results
    results_path = GMAIL_SCAN_DIR / "scan_results.json"
    with open(results_path, "w") as fh:
        json.dump({
            "timestamp": timestamp.isoformat(),
            "since": since.isoformat(),
            "email_count": len(emails),
            "emails": emails,
        }, fh, indent=2, ensure_ascii=False)
    print(f"  Results: {results_path}")

    # Notify ccmux
    if emails:
        print("  Notifying ccmux...")
        notify_ccmux(emails, timestamp.strftime("%H:%M"))
    else:
        print("  No new emails, skipping notification.")

    # Save scan state
    last_uid = emails[-1]["uid"] if emails else last_scan.get("last_uid", "") if last_scan else ""
    save_scan_state(timestamp.isoformat(), len(emails), last_uid)
    print(f"  Scan state saved: {SCAN_STATE_PATH}")

    print("\n[gmail_scanner] Done.")


if __name__ == "__main__":
    main()
