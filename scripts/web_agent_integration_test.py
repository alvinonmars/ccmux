#!/usr/bin/env python3
"""Integration test: BrowserSession + PowerSchool login.

Run with: xvfb-run -a .venv/bin/python scripts/web_agent_integration_test.py

Verifies:
1. BrowserSession starts and creates directories
2. PowerSchool ADFS SSO login succeeds
3. Screenshots are captured correctly
4. State persistence works (cookies saved)
5. Forms page is accessible and contains G1 Field Trip
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from libs.web_agent.browser import BrowserSession
from libs.web_agent.auth.powerschool import load_credentials, login

SCREENSHOT_DIR = PROJECT_ROOT / "data" / "household" / "tmp" / "integration_test"
STATE_DIR = Path("/tmp/web_agent_ps_state")


def main() -> None:
    print(f"[web_agent integration test] {datetime.now().isoformat()}")
    print(f"  Screenshot dir: {SCREENSHOT_DIR}")
    print(f"  State dir: {STATE_DIR}")
    print()

    # Load credentials
    creds = load_credentials()
    print("[1/6] Credentials loaded OK")

    results = {
        "timestamp": datetime.now().isoformat(),
        "steps": [],
    }

    with BrowserSession(
        state_dir=STATE_DIR,
        screenshot_dir=SCREENSHOT_DIR,
        headless=True,
    ) as browser:

        # Step 1: Verify directories created
        assert SCREENSHOT_DIR.exists(), "Screenshot dir not created"
        assert STATE_DIR.exists(), "State dir not created"
        results["steps"].append({"step": "dirs_created", "ok": True})
        print("[2/6] Directories created OK")

        # Step 2: Login to PowerSchool
        print("[3/6] Logging into PowerSchool...")
        success = login(browser, creds)
        path = browser.screenshot("01_post_login")
        print(f"  Screenshot: {path}")
        results["steps"].append({
            "step": "login",
            "ok": success,
            "url": browser.page.url,
            "screenshot": path,
        })
        if not success:
            print("  ERROR: Login failed!")
            _save_results(results)
            sys.exit(1)
        print(f"  Login OK. URL: {browser.page.url}")

        # Step 3: Verify page info works
        info = browser.page_info()
        print(f"[4/6] Page info: title='{info['title']}', "
              f"text_snippet={len(info['text_snippet'])} chars")
        results["steps"].append({
            "step": "page_info",
            "ok": bool(info["title"] or info["text_snippet"]),
            "title": info["title"],
        })

        # Step 4: Navigate to Forms page
        print("[5/6] Navigating to Forms page...")
        forms_url = creds["POWERSCHOOL_URL"].rsplit("/", 1)[0]
        if "/guardian" not in forms_url:
            from urllib.parse import urlparse
            parsed = urlparse(creds["POWERSCHOOL_URL"])
            forms_url = f"{parsed.scheme}://{parsed.netloc}/guardian/forms.html"
        else:
            forms_url = forms_url.rsplit("/guardian", 1)[0] + "/guardian/forms.html"

        browser.goto(forms_url)
        browser.wait(3000)
        path = browser.screenshot("02_forms_page")
        print(f"  Screenshot: {path}")

        info = browser.page_info()
        links = browser.get_links()
        forms_found = [l for l in links if "field trip" in l["text"].lower()
                       or "fieldtrip" in l["text"].lower()
                       or "G1 Field" in l["text"]]

        print(f"  URL: {info['url']}")
        print(f"  Total links: {len(links)}")
        print(f"  Field trip links: {[l['text'] for l in forms_found]}")

        results["steps"].append({
            "step": "forms_page",
            "ok": len(forms_found) > 0,
            "url": info["url"],
            "total_links": len(links),
            "field_trip_links": [l for l in forms_found],
            "screenshot": path,
        })

        # Step 5: Verify state persistence
        print("[6/6] Checking state persistence...")
        state_file = STATE_DIR / "storage_state.json"

    # Session closed — state should be saved
    state_saved = state_file.exists()
    if state_saved:
        state_size = state_file.stat().st_size
        print(f"  State saved: {state_file} ({state_size} bytes)")
    else:
        print("  WARNING: State file not found after session close")
    results["steps"].append({
        "step": "state_persistence",
        "ok": state_saved,
    })

    # Summary
    print()
    print("=" * 50)
    all_ok = all(s["ok"] for s in results["steps"])
    results["all_ok"] = all_ok
    for s in results["steps"]:
        status = "PASS" if s["ok"] else "FAIL"
        print(f"  [{status}] {s['step']}")
    print("=" * 50)
    print(f"  Overall: {'ALL PASSED' if all_ok else 'SOME FAILED'}")

    _save_results(results)


def _save_results(results: dict) -> None:
    out = SCREENSHOT_DIR / "integration_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)
    print(f"\n  Results saved: {out}")


if __name__ == "__main__":
    main()
