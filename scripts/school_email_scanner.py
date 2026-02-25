#!/usr/bin/env python3
"""School parent email daily scanner.

Screenshot-driven approach: logs into Outlook Web via <school-portal-url>,
captures inbox overview and individual email body screenshots for NEW emails
since the last scan, then notifies ccmux so Claude can analyze and forward
actionable items.

Scan window: checks emails between the last scan timestamp and now. On first
run or if the state file is missing, defaults to today's emails only.

Today detection: Outlook Web shows HH:MM for today's emails and "周X M/D"
for older ones. We parse aria-label on each email item to distinguish.

Date extraction: For older emails, "周X M/D" gives relative date; we parse
M/D and compare against the last scan date.

State file: ~/.ccmux/data/household/tmp/email_scan/last_scan.json — persists
the last successful scan timestamp.

Required environment variable:
    CCMUX_SCHOOL_EMAIL_URL: School email portal entry URL

Run with: xvfb-run -a .venv/bin/python scripts/school_email_scanner.py
Cron:     30 8 * * * cd <project_root> && xvfb-run -a .venv/bin/python scripts/school_email_scanner.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ccmux.paths import EMAIL_SCAN_DIR
from libs.web_agent.browser import BrowserSession
from libs.web_agent.auth.school_email import login

STATE_DIR = Path("/tmp/web_agent_outlook_state")
SCREENSHOT_DIR = EMAIL_SCAN_DIR
SCAN_STATE_PATH = SCREENSHOT_DIR / "last_scan.json"
FIFO_PATH = Path("/tmp/ccmux/in.email")

# Patterns for detecting email dates from aria-label
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")
_WEEKDAY_RE = re.compile(r"周[一二三四五六日]")
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})")

# Reading pane selectors in priority order
READING_PANE_SELECTORS = [
    '[role="main"] [role="region"]',
    '.ReadingPaneContainerId',
    '[aria-label*="Message body"]',
    '[role="main"]',
    '.customScrollBar',
]


def load_last_scan() -> dict | None:
    """Load the last scan state from disk."""
    if not SCAN_STATE_PATH.exists():
        return None
    try:
        with open(SCAN_STATE_PATH) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def save_scan_state(timestamp: str, email_count: int) -> None:
    """Persist the current scan timestamp."""
    state = {
        "last_scan": timestamp,
        "email_count": email_count,
    }
    with open(SCAN_STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=2)


_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6}


def get_email_date(aria_label: str, today: date) -> date | None:
    """Extract the date of an email from its aria-label.

    - Today's emails show HH:MM (no weekday) → return today
    - Same-week emails show "周X HH:MM" (weekday + time, no M/D) → compute date from weekday
    - Older emails show "周X M/D" → parse M/D and infer year from today
    - Returns None if date cannot be determined
    """
    has_time = _TIME_RE.search(aria_label)
    has_weekday = _WEEKDAY_RE.search(aria_label)

    if has_time and not has_weekday:
        return today

    # Same-week: "周X HH:MM" with no M/D date
    if has_weekday and has_time:
        date_match = _DATE_RE.search(aria_label)
        if not date_match:
            # Weekday + time but no M/D → same-week email
            weekday_char = has_weekday.group(0)[-1]  # 一, 二, 三, ...
            target_dow = _WEEKDAY_MAP.get(weekday_char)
            if target_dow is not None:
                today_dow = today.weekday()  # Monday=0
                days_back = (today_dow - target_dow) % 7
                from datetime import timedelta
                return today - timedelta(days=days_back)

    date_match = _DATE_RE.search(aria_label)
    if date_match:
        month = int(date_match.group(1))
        day = int(date_match.group(2))
        year = today.year
        # Handle year boundary (e.g., scanning in Jan for Dec emails)
        if month > today.month + 1:
            year -= 1
        try:
            return date(year, month, day)
        except ValueError:
            return None

    return None


def is_within_scan_window(aria_label: str, today: date, since_date: date) -> bool:
    """Check if an email falls within the scan window (since_date to today)."""
    email_date = get_email_date(aria_label, today)
    if email_date is None:
        return False
    return email_date >= since_date


def parse_aria_label(aria_label: str) -> dict:
    """Extract sender, subject, time, and preview from aria-label.

    Outlook Web aria-label format (today):
      "sender subject HH:MM preview..."
    Older:
      "sender subject 周X M/D preview..."
    Prefixes like "未读", "已折叠", "已答复" may appear at the start.
    """
    text = aria_label.strip()
    # Strip status prefixes
    for prefix in ("未读 ", "已折叠 ", "已答复 "):
        text = text.removeprefix(prefix)
    text = text.strip()

    time_match = _TIME_RE.search(text)
    if time_match:
        before_time = text[: time_match.start()].strip()
        email_time = time_match.group(1)
        preview = text[time_match.end() :].strip()
    else:
        before_time = text
        email_time = ""
        preview = ""

    return {
        "sender_subject": before_time,
        "time": email_time,
        "preview": preview[:150],
        "unread": "未读" in aria_label,
    }


def capture_new_emails(
    browser: BrowserSession, date_str: str, since_date: date
) -> list[dict]:
    """Click each email since since_date and capture reading pane screenshots."""
    page = browser.page
    today = date.today()

    items = page.query_selector_all('[role="option"]')
    if not items:
        items = page.query_selector_all("[data-convid]")

    print(f"  Found {len(items)} total email items")
    print(f"  Scan window: {since_date} to {today}")

    captured = []
    for i, item in enumerate(items):
        try:
            aria = item.get_attribute("aria-label") or ""
            if not aria:
                continue

            if not is_within_scan_window(aria, today, since_date):
                email_date = get_email_date(aria, today)
                if email_date is not None and email_date < since_date:
                    # Past the scan window, emails are chronological so stop
                    print(f"  [{i}] Before scan window ({email_date}), stopping.")
                    break
                # Could not determine date, skip but continue
                print(f"  [{i}] Date unknown, skipping. aria: {aria[:60]}...")
                continue

            info = parse_aria_label(aria)
            email_date = get_email_date(aria, today)
            date_label = "today" if email_date == today else str(email_date)
            print(
                f"  [{i}] {date_label} @ {info['time']}: "
                f"{info['sender_subject'][:60]}"
            )

            # Click to open in reading pane
            item.click()
            browser.wait(2000)

            # Screenshot just the reading pane
            reading_pane = None
            for selector in READING_PANE_SELECTORS:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    reading_pane = el
                    break

            idx = len(captured) + 1
            out_path = SCREENSHOT_DIR / f"email_body_{date_str}_{idx}.png"

            if reading_pane:
                reading_pane.screenshot(path=str(out_path))
                print(f"    Saved reading pane: {out_path}")
            else:
                page.screenshot(path=str(out_path))
                print(f"    Reading pane not found, saved full page: {out_path}")

            captured.append({
                "index": idx,
                "sender_subject": info["sender_subject"],
                "time": info["time"],
                "date": str(email_date) if email_date else "",
                "unread": info["unread"],
                "preview": info["preview"],
                "path": str(out_path),
            })

        except Exception as e:
            print(f"  [{i}] Error: {e}")
            continue

    return captured


def notify_ccmux(meta: dict) -> bool:
    """Write email scan result to ccmux FIFO."""
    if not meta.get("login_success"):
        content = "Email scan failed: could not log into Outlook Web."
    else:
        emails = meta.get("emails", [])
        since = meta.get("since_date", "today")
        screenshot = meta.get("inbox_screenshot", "")
        lines = [
            f"Email scan complete. {len(emails)} new email(s) since {since}.",
            f"Inbox screenshot: {screenshot}",
        ]
        for email in emails:
            lines.append(
                f"  {email['index']}. [{email.get('date', '')} {email['time']}] "
                f"{email['sender_subject'][:60]}"
            )
            lines.append(f"     Body screenshot: {email['path']}")
        lines.append(
            "Please read the body screenshots, identify actionable items, "
            "and forward each with its screenshot to the household group."
        )
        content = "\n".join(lines)

    payload = json.dumps({
        "channel": "email",
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
    timestamp = datetime.now()
    date_str = timestamp.strftime("%Y%m%d")
    today = date.today()
    print(f"[school_email_scanner] {timestamp.isoformat()}")

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # Determine scan window from last scan state
    last_scan = load_last_scan()
    if last_scan and last_scan.get("last_scan"):
        last_dt = datetime.fromisoformat(last_scan["last_scan"])
        since_date = last_dt.date()
        print(f"  Last scan: {last_scan['last_scan']} ({last_scan.get('email_count', '?')} emails)")
        print(f"  Scan window: {since_date} to {today}")
    else:
        since_date = today
        print("  First scan (no previous state). Checking today only.")

    with BrowserSession(
        state_dir=STATE_DIR,
        screenshot_dir=SCREENSHOT_DIR,
        headless=True,
    ) as browser:
        # Step 1: Login
        print("[1/4] Logging into Outlook Web...")
        success = login(browser)
        if not success:
            print("  ERROR: Login failed")
            browser.screenshot(f"login_failed_{date_str}")
            notify_ccmux({"login_success": False})
            sys.exit(1)

        info = browser.page_info()
        print(f"  URL: {info['url']}")
        print(f"  Title: {info['title']}")

        # Step 2: Capture inbox overview screenshot
        print("[2/4] Capturing inbox screenshot...")
        browser.wait(3000)
        inbox_screenshot = browser.screenshot(f"inbox_{date_str}")
        print(f"  Screenshot: {inbox_screenshot}")

        # Step 3: Capture email body screenshots for scan window
        print(f"[3/4] Capturing email bodies since {since_date}...")
        emails = capture_new_emails(browser, date_str, since_date)
        print(f"  Captured {len(emails)} email bodies")

        # Save scan metadata
        meta = {
            "timestamp": timestamp.isoformat(),
            "date": date_str,
            "since_date": str(since_date),
            "inbox_screenshot": str(inbox_screenshot),
            "url": info["url"],
            "login_success": True,
            "emails": emails,
        }
        meta_path = SCREENSHOT_DIR / "scan_results.json"
        with open(meta_path, "w") as fh:
            json.dump(meta, fh, indent=2, ensure_ascii=False)
        print(f"  Metadata: {meta_path}")

        # Step 4: Notify ccmux
        print("[4/4] Notifying ccmux...")
        notify_ccmux(meta)

    # Save scan state AFTER successful completion
    save_scan_state(timestamp.isoformat(), len(emails))
    print(f"  Scan state saved: {SCAN_STATE_PATH}")

    print("\n[school_email_scanner] Done.")


if __name__ == "__main__":
    main()
