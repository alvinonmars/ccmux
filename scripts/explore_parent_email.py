#!/usr/bin/env python3
"""Explore PowerSchool → Parent Resources → Parent Email entry point.

Discovers what email system School uses and how the SSO flow works.
Run with: xvfb-run -a .venv/bin/python scripts/explore_parent_email.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from libs.web_agent.browser import BrowserSession
from libs.web_agent.auth.powerschool import load_credentials, login

SCREENSHOT_DIR = PROJECT_ROOT / "data" / "household" / "tmp" / "email_explore"
STATE_DIR = Path("/tmp/web_agent_ps_state")


def main() -> None:
    print(f"[explore_parent_email] {datetime.now().isoformat()}")
    creds = load_credentials()

    with BrowserSession(
        state_dir=STATE_DIR,
        screenshot_dir=SCREENSHOT_DIR,
        headless=True,
    ) as browser:
        # Login
        print("[1/4] Logging into PowerSchool...")
        success = login(browser, creds)
        if not success:
            print("  ERROR: Login failed")
            sys.exit(1)
        browser.screenshot("01_logged_in")
        print(f"  OK. URL: {browser.page.url}")

        # Navigate to Parent Resources
        print("[2/4] Navigating to Parent Resources...")
        from urllib.parse import urlparse
        parsed = urlparse(creds["POWERSCHOOL_URL"])
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        browser.goto(f"{base_url}/guardian/parentresources.html")
        browser.wait(3000)
        path = browser.screenshot("02_parent_resources")
        print(f"  Screenshot: {path}")

        info = browser.page_info()
        print(f"  URL: {info['url']}")
        print(f"  Title: {info['title']}")

        # Get all links on the page
        links = browser.get_links()
        print(f"  Total links: {len(links)}")

        # Find email-related links
        email_links = [
            l for l in links
            if any(kw in l["text"].lower() for kw in ["email", "mail", "outlook", "gmail"])
            or any(kw in l["href"].lower() for kw in ["email", "mail", "outlook", "gmail"])
        ]
        print(f"  Email-related links: {json.dumps(email_links, indent=2)}")

        # Also dump all links for analysis
        print("\n  All links on Parent Resources page:")
        for l in links:
            if l["text"].strip():
                print(f"    - [{l['text'][:60]}] -> {l['href'][:100]}")

        # Try clicking "Parent Email" if found
        if email_links:
            target = email_links[0]
            print(f"\n[3/4] Clicking email link: '{target['text']}' -> {target['href']}")

            # If it's an external link, navigate directly
            href = target["href"]
            if href.startswith("http"):
                browser.goto(href)
            else:
                browser.click(text=target["text"])
            browser.wait(5000)

            path = browser.screenshot("03_email_landing")
            print(f"  Screenshot: {path}")

            info = browser.page_info()
            print(f"  URL: {info['url']}")
            print(f"  Title: {info['title']}")
            print(f"  Text snippet: {info['text_snippet'][:500]}")

            # Check for further redirects (SSO)
            browser.wait(3000)
            path = browser.screenshot("04_email_after_wait")
            info2 = browser.page_info()
            if info2["url"] != info["url"]:
                print(f"\n[4/4] Redirected to: {info2['url']}")
                print(f"  Title: {info2['title']}")
                path = browser.screenshot("04_email_final")
                print(f"  Screenshot: {path}")
        else:
            print("\n[3/4] No email link found on Parent Resources page.")
            # Try looking at the page text for clues
            text = browser.page_info()["text_snippet"]
            print(f"  Page text: {text[:500]}")

    print("\n[explore_parent_email] Done.")


if __name__ == "__main__":
    main()
