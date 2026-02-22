#!/usr/bin/env python3
"""
PowerSchool homework checker.

Logs into PowerSchool guardian portal via ADFS SSO, navigates to the
"Classes and Home Learning" page, parses the homework table, detects
new assignments via deduplication state, and notifies ccmux via FIFO.

Credentials: ~/.secrets/powerschool.env
Must be run with xvfb-run on headless Linux:
    xvfb-run .venv/bin/python3 scripts/powerschool_checker.py

Flags:
    --force   Skip dedup, always save and notify (for manual testing)
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


# --- Configuration -----------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path.home() / ".secrets" / "powerschool.env"
CHILD_NAME = os.environ.get("PS_CHILD_NAME", "Child")
SCHOOL_CODE = os.environ.get("PS_SCHOOL_CODE", "school")
CHILD_DIR = os.environ.get("PS_CHILD_DIR", "child")
BASE_OUTPUT_DIR = PROJECT_ROOT / "data" / "household" / "homework" / SCHOOL_CODE / CHILD_DIR
STATE_FILE = BASE_OUTPUT_DIR / ".seen_assignments.json"
FIFO_PATH = Path("/tmp/ccmux/in.homework")
TODAY = date.today()
TODAY_ISO = TODAY.isoformat()
MONTH_DIR = BASE_OUTPUT_DIR / TODAY.strftime("%Y-%m")

# Date parsing: PowerSchool uses "05 FEB 2026" format
_MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


def load_credentials(env_path: Path) -> dict[str, str]:
    """Read key=value pairs from an .env file (no shell expansion)."""
    creds: dict[str, str] = {}
    if not env_path.exists():
        print(f"ERROR: Credential file not found: {env_path}")
        sys.exit(1)
    with open(env_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            creds[key.strip()] = value.strip()
    required = ("POWERSCHOOL_URL", "POWERSCHOOL_USER", "POWERSCHOOL_PASS")
    for key in required:
        if key not in creds:
            print(f"ERROR: Missing {key} in {env_path}")
            sys.exit(1)
    return creds


def cdp_screenshot(page, path: Path) -> bool:
    """Take a viewport screenshot via CDP (bypasses Playwright font waiting)."""
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Page.stopLoading")
        time.sleep(0.5)
        result = cdp.send(
            "Page.captureScreenshot",
            {"format": "png"},
        )
        cdp.detach()
        img_data = base64.b64decode(result["data"])
        with open(path, "wb") as fh:
            fh.write(img_data)
        print(f"  Screenshot saved: {path}  ({len(img_data)} bytes)")
        return True
    except Exception as exc:
        print(f"  ERROR: CDP screenshot failed: {exc}")
        return False


# --- Assignment parsing ------------------------------------------------------

def _parse_ps_date(raw: str) -> str:
    """Convert PowerSchool date '05 FEB 2026' -> '2026-02-05'.

    Returns empty string if parsing fails.
    """
    parts = raw.strip().split()
    if len(parts) != 3:
        return ""
    day, month_str, year = parts
    month = _MONTH_MAP.get(month_str.upper(), "")
    if not month:
        return ""
    return f"{year}-{month}-{day.zfill(2)}"


def _make_assignment_id(assigned_date: str, class_name: str, task: str) -> str:
    """Generate a stable ID from assignment fields (sha256 truncated to 16 hex chars)."""
    raw = f"{assigned_date}|{class_name}|{task}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_assignments(page) -> list[dict]:
    """Parse the homework table rows from the page.

    Each assignment dict has: id, assigned_date, due_date, class_name,
    teacher, task_description, full_text.
    """
    assignments = []

    # The homework table lives inside #content-main
    rows = page.query_selector_all("#content-main table tbody tr")
    if not rows:
        # Fallback: try any table row inside content-main
        rows = page.query_selector_all("#content-main tr")

    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) < 6:
            continue

        # Columns: Completed, Assigned Date, Due Date, Class, Teacher, Task Description
        assigned_raw = (cells[1].inner_text() or "").strip()
        due_raw = (cells[2].inner_text() or "").strip()
        class_name = (cells[3].inner_text() or "").strip()
        teacher = (cells[4].inner_text() or "").strip()
        task_desc = (cells[5].inner_text() or "").strip()

        if not assigned_raw or not class_name:
            continue

        assigned_date = _parse_ps_date(assigned_raw)
        due_date = _parse_ps_date(due_raw)

        # Full text: get the entire row text (includes duration, type, details)
        full_text = (row.inner_text() or "").strip()

        aid = _make_assignment_id(assigned_date, class_name, task_desc)
        assignments.append({
            "id": aid,
            "assigned_date": assigned_date,
            "due_date": due_date,
            "class_name": class_name,
            "teacher": teacher,
            "task_description": task_desc,
            "full_text": full_text,
        })

    return assignments


# --- Deduplication state -----------------------------------------------------

def load_state() -> dict:
    """Load seen assignment IDs from state file.

    Returns dict with 'seen' key mapping ID -> first_seen ISO date.
    """
    if not STATE_FILE.exists():
        return {"seen": {}}
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        if "seen" not in data:
            data["seen"] = {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  WARNING: Could not load state: {exc}")
        return {"seen": {}}


def save_state(state: dict) -> None:
    """Write state file, pruning entries older than 60 days."""
    cutoff = (TODAY - timedelta(days=60)).isoformat()
    pruned = {
        k: v for k, v in state.get("seen", {}).items()
        if v >= cutoff
    }
    state["seen"] = pruned
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def detect_new(assignments: list[dict], state: dict) -> list[dict]:
    """Return assignments not yet in the seen state."""
    seen = state.get("seen", {})
    return [a for a in assignments if a["id"] not in seen]


def mark_seen(assignments: list[dict], state: dict) -> None:
    """Add assignments to the seen state."""
    for a in assignments:
        state["seen"][a["id"]] = TODAY_ISO


# --- Screenshot & text extraction --------------------------------------------

def capture_table_screenshot(page, path: Path) -> bool:
    """Screenshot the #content-main element (cropped to homework area).

    Falls back to CDP full-page screenshot if element screenshot fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        el = page.query_selector("#content-main")
        if el:
            el.screenshot(path=str(path))
            size = path.stat().st_size
            print(f"  Table screenshot saved: {path}  ({size} bytes)")
            return True
    except Exception as exc:
        print(f"  WARNING: Element screenshot failed ({exc}), falling back to CDP")
    return cdp_screenshot(page, path)


def extract_text(page) -> str:
    """Extract text content from the homework page."""
    lines: list[str] = []
    lines.append(f"PowerSchool Homework Check - {TODAY_ISO}")
    lines.append(f"URL: {page.url}")
    lines.append("=" * 60)

    for sel in ("#content-main", ".content-main", "main"):
        try:
            elements = page.query_selector_all(sel)
            for el in elements:
                text = el.inner_text().strip()
                if text and len(text) > 10:
                    lines.append(text)
                    return "\n".join(lines)
        except Exception:
            continue

    # Fallback
    try:
        body_text = page.inner_text("body")
        if body_text.strip():
            lines.append(body_text.strip())
    except Exception as exc:
        lines.append(f"ERROR extracting body text: {exc}")

    return "\n".join(lines)


# --- FIFO notification -------------------------------------------------------

def notify_ccmux(new_assignments: list[dict], screenshot_path: Path, text_path: Path) -> bool:
    """Write notification to ccmux FIFO.

    Creates FIFO if not exists. Uses O_WRONLY|O_NONBLOCK for non-blocking write.
    Returns True if notification was sent, False otherwise.
    """
    # Build summary lines
    summary_lines = []
    for a in new_assignments:
        due = a["due_date"] or "no due date"
        summary_lines.append(f"- [{a['class_name']}] {a['task_description'][:80]} (due {due})")
    summary = "\n".join(summary_lines)

    content = (
        f"New homework for {CHILD_NAME}:\n"
        f"{summary}\n\n"
        f"Screenshot: {screenshot_path}\n"
        f"Details: {text_path}"
    )

    payload = json.dumps({
        "channel": "homework",
        "content": content,
        "ts": int(time.time()),
    })

    # Check payload size (PIPE_BUF = 4096 for atomic write)
    payload_bytes = (payload + "\n").encode()
    if len(payload_bytes) > 4096:
        print(f"  WARNING: Payload {len(payload_bytes)} bytes exceeds PIPE_BUF, truncating")
        # Truncate summary to fit
        content = (
            f"New homework for {CHILD_NAME}: {len(new_assignments)} assignment(s)\n\n"
            f"Screenshot: {screenshot_path}\n"
            f"Details: {text_path}"
        )
        payload = json.dumps({
            "channel": "homework",
            "content": content,
            "ts": int(time.time()),
        })
        payload_bytes = (payload + "\n").encode()

    # Create FIFO if not exists
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


# --- Main flow ---------------------------------------------------------------

def run_checker(force: bool = False) -> None:
    creds = load_credentials(ENV_FILE)
    MONTH_DIR.mkdir(parents=True, exist_ok=True)

    screenshot_path = MONTH_DIR / f"{TODAY_ISO}_homework.png"
    text_path = MONTH_DIR / f"{TODAY_ISO}_homework.txt"
    assignments_path = MONTH_DIR / f"{TODAY_ISO}_assignments.json"

    url = creds["POWERSCHOOL_URL"]
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    username = creds["POWERSCHOOL_USER"]
    password = creds["POWERSCHOOL_PASS"]

    print(f"[powerschool_checker] {datetime.now().isoformat()}")
    print(f"[powerschool_checker] Output dir : {MONTH_DIR}")
    if force:
        print("[powerschool_checker] --force mode: skipping dedup")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Block fonts to prevent screenshot timeout
        page.route("**/*.woff*", lambda r: r.abort())
        page.route("**/*.ttf", lambda r: r.abort())
        page.route("**/*.otf", lambda r: r.abort())
        page.route("**/*.eot", lambda r: r.abort())
        page.route("**/fonts.googleapis.com/**", lambda r: r.abort())
        page.route("**/fonts.gstatic.com/**", lambda r: r.abort())

        # ---- Step 1: Navigate to PowerSchool landing page -------------------
        print("[1/9] Navigating to PowerSchool ...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            print("  WARNING: load timed out, continuing.")
        except Exception as exc:
            print(f"  ERROR: Failed to load page: {exc}")
            browser.close()
            sys.exit(1)
        print(f"  URL: {page.url}")

        # ---- Step 2: Click "Parent Sign In" -> ADFS SSO redirect -----------
        print("[2/9] Clicking Parent Sign In ...")
        parent_btn = page.query_selector("#parentSignIn")
        if parent_btn:
            href = parent_btn.get_attribute("href")
            idp_url = (
                f"{base_url}{href}"
                if href and href.startswith("/")
                else href
            )
        else:
            ts = int(time.time() * 1000)
            idp_url = f"{base_url}/guardian/idp?_userTypeHint=guardian&_={ts}"
        try:
            page.goto(idp_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            print("  WARNING: IDP redirect timed out, continuing.")
        page.wait_for_timeout(3000)
        print(f"  Redirected to: {page.url}")

        # ---- Step 3: Fill ADFS credentials ----------------------------------
        print("[3/9] Filling ADFS credentials ...")
        login_ok = False

        user_el = page.query_selector("#userNameInput")
        pass_el = page.query_selector("#passwordInput")
        if user_el and pass_el:
            user_el.fill(username)
            pass_el.fill(password)
            print("  Credentials filled (ADFS form).")
            login_ok = True
        else:
            for sel in ['input[name="loginfmt"]', 'input[type="email"]',
                        'input[name="username"]', 'input[id="fieldAccount"]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(username)
                    print(f"  Username via: {sel}")
                    break
            for sel in ['input[name="passwd"]', 'input[name="password"]',
                        'input[type="password"]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(password)
                    print(f"  Password via: {sel}")
                    login_ok = True
                    break

        if not login_ok:
            debug_path = MONTH_DIR / f"{TODAY_ISO}_debug_idp.html"
            with open(debug_path, "w") as fh:
                fh.write(page.content())
            print(f"  ERROR: Could not fill credentials. Debug: {debug_path}")
            browser.close()
            sys.exit(1)

        # ---- Step 4: Submit login -------------------------------------------
        print("[4/9] Submitting login ...")
        submit_btn = page.query_selector("#submitButton")
        if submit_btn:
            submit_btn.click()
        else:
            for sel in ['input[type="submit"]', 'button[type="submit"]',
                        '#idSIButton9']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(5000)
        print(f"  Post-login URL: {page.url}")

        # Handle "Stay signed in?" (Microsoft IDP)
        try:
            stay = page.query_selector("#idSIButton9")
            if stay and stay.is_visible():
                stay.click()
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # Verify login success
        if "guardian" not in page.url:
            print("  ERROR: Login may have failed.")
            debug_path = MONTH_DIR / f"{TODAY_ISO}_debug_postlogin.html"
            with open(debug_path, "w") as fh:
                fh.write(page.content())
            print(f"  Debug HTML saved: {debug_path}")
            browser.close()
            sys.exit(1)
        print("  Login successful.")

        # ---- Step 5: Navigate to Classes and Home Learning ------------------
        print("[5/9] Navigating to Classes and Home Learning ...")
        homework_url = f"{base_url}/guardian/homelearning.html"

        hw_link = page.query_selector('a[href="/guardian/homelearning.html"]')
        if not hw_link:
            hw_link = page.query_selector('a:has-text("Classes and Home Learning")')
        if hw_link:
            hw_link.click()
            print("  Clicked nav link.")
        else:
            page.goto(homework_url, wait_until="domcontentloaded", timeout=30_000)
            print("  Direct navigation to homework URL.")

        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(3000)
        print(f"  URL: {page.url}")

        # ---- Step 6: Parse assignments --------------------------------------
        print("[6/9] Parsing assignments ...")
        assignments = parse_assignments(page)
        print(f"  Found {len(assignments)} assignment(s)")
        for a in assignments:
            print(f"    - [{a['class_name']}] {a['task_description'][:60]}")

        # ---- Step 7: Detect new assignments ---------------------------------
        print("[7/9] Checking for new assignments ...")
        state = load_state()

        if force:
            new_assignments = assignments
            print(f"  --force: treating all {len(new_assignments)} as new")
        else:
            new_assignments = detect_new(assignments, state)
            print(f"  New: {len(new_assignments)}, Previously seen: {len(assignments) - len(new_assignments)}")

        if not new_assignments:
            print("\n[powerschool_checker] No new homework. Done.")
            browser.close()
            return

        # ---- Step 8: Save screenshot, text, assignments JSON ----------------
        print("[8/9] Saving outputs ...")
        capture_table_screenshot(page, screenshot_path)

        text_content = extract_text(page)
        with open(text_path, "w") as fh:
            fh.write(text_content)
        print(f"  Text saved: {text_path}")

        with open(assignments_path, "w") as fh:
            json.dump(new_assignments, fh, indent=2, ensure_ascii=False)
        print(f"  Assignments JSON saved: {assignments_path}")

        # Update dedup state
        mark_seen(assignments, state)
        save_state(state)
        print(f"  State updated: {STATE_FILE}")

        # ---- Step 9: Notify ccmux ------------------------------------------
        print("[9/9] Notifying ccmux ...")
        notify_ccmux(new_assignments, screenshot_path, text_path)

        browser.close()

    print("\n[powerschool_checker] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PowerSchool homework checker")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip dedup check, always save and notify",
    )
    args = parser.parse_args()
    run_checker(force=args.force)
