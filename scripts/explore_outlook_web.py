#!/usr/bin/env python3
"""Explore School Outlook Web email via ADFS SSO.

Navigates: mail.school.example.com → ADFS login → Outlook Web → inbox
Run with: xvfb-run -a .venv/bin/python scripts/explore_outlook_web.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from libs.web_agent.browser import BrowserSession
from libs.web_agent.auth.powerschool import load_credentials

SCREENSHOT_DIR = PROJECT_ROOT / "data" / "household" / "tmp" / "outlook_explore"
STATE_DIR = Path("/tmp/web_agent_outlook_state")


def main() -> None:
    print(f"[explore_outlook_web] {datetime.now().isoformat()}")
    creds = load_credentials()
    username = creds["POWERSCHOOL_USER"]
    password = creds["POWERSCHOOL_PASS"]

    with BrowserSession(
        state_dir=STATE_DIR,
        screenshot_dir=SCREENSHOT_DIR,
        headless=True,
    ) as browser:
        # Step 1: Navigate to parent email portal
        print("[1/6] Navigating to mail.school.example.com...")
        browser.goto("http://mail.school.example.com", timeout=30_000)
        browser.wait(3000)
        path = browser.screenshot("01_email_landing")
        info = browser.page_info()
        print(f"  URL: {info['url']}")
        print(f"  Title: {info['title']}")
        print(f"  Screenshot: {path}")

        # Step 2: Fill ADFS credentials
        print("[2/6] Filling ADFS credentials...")
        page = browser.page

        # Check if already on ADFS login or Outlook
        if "adfs" in info["url"] or "login.microsoftonline" in info["url"]:
            # ADFS login form
            user_el = page.query_selector("#userNameInput")
            pass_el = page.query_selector("#passwordInput")
            if user_el and pass_el:
                user_el.fill(username)
                pass_el.fill(password)
                print("  Filled ADFS form")
            else:
                # Microsoft online login
                email_el = page.query_selector('input[name="loginfmt"]')
                if email_el:
                    email_el.fill(username)
                    browser.press("Enter")
                    browser.wait(3000)
                    pass_el = page.query_selector('input[name="passwd"]')
                    if pass_el:
                        pass_el.fill(password)
                        print("  Filled Microsoft login form")
                    else:
                        print("  WARNING: Password field not found")
                else:
                    print("  WARNING: No credential fields found")
                    path = browser.screenshot("02_no_creds")
                    print(f"  Debug screenshot: {path}")
        elif "outlook" in info["url"]:
            print("  Already logged into Outlook (cookies restored)")
        else:
            print(f"  Unexpected page: {info['url']}")
            path = browser.screenshot("02_unexpected")
            print(f"  Debug screenshot: {path}")

        # Step 3: Submit login
        print("[3/6] Submitting login...")
        submit_btn = page.query_selector("#submitButton")
        if submit_btn:
            submit_btn.click()
        else:
            for sel in ['input[type="submit"]', 'button[type="submit"]', '#idSIButton9']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    break

        browser.wait(5000)
        path = browser.screenshot("03_post_login")
        info = browser.page_info()
        print(f"  URL: {info['url']}")
        print(f"  Title: {info['title']}")
        print(f"  Screenshot: {path}")

        # Handle Microsoft prompts (up to 3 rounds)
        for attempt in range(3):
            page_text = browser.page_info()["text_snippet"].lower()
            current_url = browser.page.url

            # "Do you trust ...?" prompt → click Continue
            continue_btn = page.query_selector('#idBtn_Accept')
            if continue_btn and continue_btn.is_visible():
                continue_btn.click()
                browser.wait(5000)
                print(f"  [{attempt}] Clicked 'Continue' (trust prompt)")
                browser.screenshot(f"03b_after_trust_{attempt}")
                continue

            # "Stay signed in?" prompt → click Yes
            stay_btn = page.query_selector('#idSIButton9')
            if stay_btn and stay_btn.is_visible():
                stay_btn.click()
                browser.wait(5000)
                print(f"  [{attempt}] Clicked 'Yes' (stay signed in)")
                browser.screenshot(f"03c_after_stay_{attempt}")
                continue

            # Check if we've reached Outlook
            if "outlook" in current_url or "mail" in current_url:
                print(f"  Reached Outlook: {current_url}")
                break

            # Unknown prompt — try generic submit buttons
            for sel in ['input[type="submit"]', 'button[type="submit"]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    btn_text = el.inner_text() if el.inner_text() else el.get_attribute("value")
                    el.click()
                    browser.wait(5000)
                    print(f"  [{attempt}] Clicked generic submit: '{btn_text}'")
                    browser.screenshot(f"03d_generic_{attempt}")
                    break
            else:
                print(f"  [{attempt}] No buttons found, waiting...")
                browser.wait(3000)

        # Step 4: Wait for Outlook to load
        print("[4/6] Waiting for Outlook to load...")
        browser.wait(5000)
        path = browser.screenshot("04_outlook_inbox")
        info = browser.page_info()
        print(f"  URL: {info['url']}")
        print(f"  Title: {info['title']}")
        print(f"  Screenshot: {path}")

        # Step 5: Try to extract email subjects from the page
        print("[5/6] Extracting email content...")
        text = info["text_snippet"]
        print(f"  Page text (first 1000 chars): {text[:1000]}")

        # Try to get aria labels or email subject elements
        try:
            subjects = page.query_selector_all('[aria-label*="message"]')
            if not subjects:
                subjects = page.query_selector_all('[role="option"]')
            if not subjects:
                subjects = page.query_selector_all('.hcptT')  # OWA subject class
            if not subjects:
                subjects = page.query_selector_all('[data-convid]')
            print(f"  Found {len(subjects)} email elements")
            for i, s in enumerate(subjects[:10]):
                try:
                    txt = s.inner_text()[:200]
                    print(f"    [{i}] {txt}")
                except Exception:
                    pass
        except Exception as e:
            print(f"  Could not extract emails: {e}")

        # Step 6: Take final screenshot and save state
        print("[6/6] Final state...")
        path = browser.screenshot("05_final")
        print(f"  Screenshot: {path}")

    print("\n[explore_outlook_web] Done.")


if __name__ == "__main__":
    main()
