#!/usr/bin/env python3
"""
PowerSchool portal explorer.

Logs into PowerSchool guardian portal via ADFS SSO, then systematically
explores the portal looking for event registration pages, specifically
"FieldTrip Fishing Village Exploration" on Feb 28, 2026.

Must be run with xvfb-run on headless Linux:
    xvfb-run .venv/bin/python3 scripts/powerschool_explore.py
"""

import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


# --- Configuration -----------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ccmux.paths import TMP_DIR

ENV_FILE = Path.home() / ".secrets" / "powerschool.env"
OUTPUT_DIR = TMP_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SCREENSHOT_COUNTER = 0


def load_credentials(env_path: Path) -> dict[str, str]:
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
    return creds


def take_screenshot(page, label: str) -> Path:
    """Take a screenshot with an incrementing counter and descriptive label."""
    global SCREENSHOT_COUNTER
    SCREENSHOT_COUNTER += 1
    filename = f"{SCREENSHOT_COUNTER:02d}_{label}.png"
    path = OUTPUT_DIR / filename
    try:
        # Use CDP screenshot to bypass font loading issues
        cdp = page.context.new_cdp_session(page)
        cdp.send("Page.stopLoading")
        time.sleep(0.3)
        result = cdp.send("Page.captureScreenshot", {"format": "png"})
        cdp.detach()
        img_data = base64.b64decode(result["data"])
        with open(path, "wb") as fh:
            fh.write(img_data)
        print(f"  Screenshot: {path} ({len(img_data)} bytes)")
    except Exception as exc:
        print(f"  WARNING: CDP screenshot failed ({exc}), trying Playwright fallback")
        try:
            page.screenshot(path=str(path), timeout=5000)
            print(f"  Screenshot (fallback): {path}")
        except Exception as exc2:
            print(f"  ERROR: All screenshot methods failed: {exc2}")
    return path


def extract_all_links(page) -> list[dict]:
    """Extract all links from the page with their text and href."""
    links = page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('a[href]').forEach(a => {
            results.push({
                text: a.innerText.trim().substring(0, 200),
                href: a.href,
                visible: a.offsetParent !== null
            });
        });
        return results;
    }""")
    return links


def extract_nav_items(page) -> list[dict]:
    """Extract navigation/sidebar menu items."""
    nav_items = page.evaluate("""() => {
        const results = [];
        // Common nav selectors
        const selectors = [
            'nav a', '.nav a', '#nav a', '.sidebar a', '#sidebar a',
            '.menu a', '#menu a', '.navbar a', '#navbar a',
            'ul.nav li a', '.navigation a', '#left-nav a',
            '.btnLink', '.btn-link', '[class*="nav"] a',
            '[class*="menu"] a', '[id*="nav"] a'
        ];
        const seen = new Set();
        selectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(a => {
                const key = a.href + '|' + a.innerText.trim();
                if (!seen.has(key) && a.innerText.trim()) {
                    seen.add(key);
                    results.push({
                        text: a.innerText.trim().substring(0, 200),
                        href: a.href,
                        visible: a.offsetParent !== null
                    });
                }
            });
        });
        return results;
    }""")
    return nav_items


def search_page_for_keywords(page, keywords: list[str]) -> list[str]:
    """Search the page text content for any of the given keywords."""
    body_text = ""
    try:
        body_text = page.inner_text("body")
    except Exception:
        pass

    found = []
    lower_text = body_text.lower()
    for kw in keywords:
        if kw.lower() in lower_text:
            found.append(kw)
    return found


def log_page_info(page, label: str):
    """Log current page URL and title."""
    print(f"\n{'='*60}")
    print(f"  Page: {label}")
    print(f"  URL:  {page.url}")
    try:
        print(f"  Title: {page.title()}")
    except Exception:
        pass
    print(f"{'='*60}")


def run_explorer():
    creds = load_credentials(ENV_FILE)
    url = creds["POWERSCHOOL_URL"]
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    username = creds["POWERSCHOOL_USER"]
    password = creds["POWERSCHOOL_PASS"]

    print(f"[powerschool_explore] {datetime.now().isoformat()}")
    print(f"[powerschool_explore] Output dir: {OUTPUT_DIR}")
    print()

    # Keywords to search for on each page
    KEYWORDS = [
        "fieldtrip", "fishing village", "field trip", "excursion",
        "event", "registration", "sign up", "sign-up", "signup",
        "form", "activity", "activities", "bulletin", "announcement",
        "feb 28", "february 28", "28 feb",
    ]

    findings = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
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

        # ================================================================
        # STEP 1: Navigate to PowerSchool landing page
        # ================================================================
        print("[STEP 1] Navigating to PowerSchool ...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            print("  WARNING: Load timed out, continuing.")
        take_screenshot(page, "01_landing_page")
        print(f"  URL: {page.url}")

        # ================================================================
        # STEP 2: Click "Parent Sign In" -> ADFS SSO redirect
        # ================================================================
        print("\n[STEP 2] Clicking Parent Sign In ...")
        parent_btn = page.query_selector("#parentSignIn")
        if parent_btn:
            href = parent_btn.get_attribute("href")
            idp_url = f"{base_url}{href}" if href and href.startswith("/") else href
        else:
            ts = int(time.time() * 1000)
            idp_url = f"{base_url}/guardian/idp?_userTypeHint=guardian&_={ts}"
        try:
            page.goto(idp_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            print("  WARNING: IDP redirect timed out, continuing.")
        page.wait_for_timeout(3000)
        take_screenshot(page, "02_adfs_login")
        print(f"  Redirected to: {page.url}")

        # ================================================================
        # STEP 3: Fill ADFS credentials
        # ================================================================
        print("\n[STEP 3] Filling ADFS credentials ...")
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
                    break
            for sel in ['input[name="passwd"]', 'input[name="password"]',
                        'input[type="password"]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(password)
                    login_ok = True
                    break

        if not login_ok:
            print("  ERROR: Could not fill credentials.")
            take_screenshot(page, "02_error_credentials")
            browser.close()
            sys.exit(1)

        # ================================================================
        # STEP 4: Submit login
        # ================================================================
        print("\n[STEP 4] Submitting login ...")
        submit_btn = page.query_selector("#submitButton")
        if submit_btn:
            submit_btn.click()
        else:
            for sel in ['input[type="submit"]', 'button[type="submit"]', '#idSIButton9']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(5000)

        # Handle "Stay signed in?" (Microsoft IDP)
        try:
            stay = page.query_selector("#idSIButton9")
            if stay and stay.is_visible():
                stay.click()
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                page.wait_for_timeout(3000)
        except Exception:
            pass

        print(f"  Post-login URL: {page.url}")
        take_screenshot(page, "03_post_login")

        # Verify login success
        if "guardian" not in page.url:
            print("  ERROR: Login may have failed.")
            take_screenshot(page, "03_login_failed")
            browser.close()
            sys.exit(1)
        print("  Login successful!")

        # ================================================================
        # STEP 5: Explore the home/dashboard page
        # ================================================================
        log_page_info(page, "Dashboard / Home")
        take_screenshot(page, "04_dashboard")

        # Extract all navigation links
        nav_items = extract_nav_items(page)
        all_links = extract_all_links(page)

        print(f"\n  Navigation items found: {len(nav_items)}")
        for item in nav_items:
            print(f"    - [{item['text'][:60]}] -> {item['href']}")

        print(f"\n  All links on page: {len(all_links)}")
        for link in all_links:
            if link['text']:
                print(f"    - [{link['text'][:60]}] -> {link['href']}")

        # Search for keywords on dashboard
        kw_found = search_page_for_keywords(page, KEYWORDS)
        if kw_found:
            print(f"\n  KEYWORDS FOUND on dashboard: {kw_found}")
            findings.append({"page": "dashboard", "url": page.url, "keywords": kw_found})

        # Save full page text for analysis
        try:
            body_text = page.inner_text("body")
            with open(OUTPUT_DIR / "dashboard_text.txt", "w") as fh:
                fh.write(body_text)
            print(f"  Full page text saved to dashboard_text.txt")
        except Exception as e:
            print(f"  Could not extract body text: {e}")

        # ================================================================
        # STEP 6: Visit each navigation link systematically
        # ================================================================
        # Collect unique guardian URLs to visit
        visited_urls = set()
        visited_urls.add(page.url)

        # Priority pages to check (common PowerSchool guardian paths)
        priority_paths = [
            "/guardian/home.html",
            "/guardian/forms.html",
            "/guardian/schoolbulletin.html",
            "/guardian/school_bulletin.html",
            "/guardian/bulletin.html",
            "/guardian/announcements.html",
            "/guardian/events.html",
            "/guardian/activities.html",
            "/guardian/registration.html",
            "/guardian/calendar.html",
            "/guardian/schoolforms.html",
            "/guardian/signups.html",
            "/guardian/fieldtrips.html",
            "/guardian/notices.html",
            "/guardian/studentforms.html",
            "/guardian/parentforms.html",
            "/guardian/ecollect.html",
            "/guardian/more.html",
        ]

        # Build full list: nav items + priority paths + all page links
        urls_to_visit = []

        # Add priority paths first
        for p in priority_paths:
            full_url = f"{base_url}{p}"
            urls_to_visit.append({"text": f"Priority: {p}", "href": full_url})

        # Add nav items
        for item in nav_items:
            if item['href'] and base_url in item['href']:
                urls_to_visit.append(item)

        # Add all links that look like guardian pages
        for link in all_links:
            if link['href'] and '/guardian/' in link['href'] and link['text']:
                urls_to_visit.append(link)

        print(f"\n[STEP 6] Exploring {len(urls_to_visit)} candidate pages ...")

        for i, target in enumerate(urls_to_visit):
            target_url = target['href']
            if target_url in visited_urls:
                continue
            visited_urls.add(target_url)

            label = target['text'][:40].replace('/', '_').replace(' ', '_').replace('\n', '_')
            safe_label = ''.join(c for c in label if c.isalnum() or c in '_-')[:30]

            print(f"\n  [{i+1}/{len(urls_to_visit)}] Visiting: {target['text'][:60]}")
            print(f"    URL: {target_url}")

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
            except PlaywrightTimeout:
                print("    WARNING: Load timed out, continuing.")
            except Exception as exc:
                print(f"    ERROR: {exc}")
                continue

            page.wait_for_timeout(2000)

            # Check if we got redirected away (e.g., 404 or not found)
            current_url = page.url

            log_page_info(page, target['text'][:60])
            take_screenshot(page, f"explore_{safe_label}")

            # Search for keywords
            kw_found = search_page_for_keywords(page, KEYWORDS)
            if kw_found:
                print(f"    *** KEYWORDS FOUND: {kw_found}")
                findings.append({
                    "page": target['text'][:60],
                    "url": current_url,
                    "keywords": kw_found
                })

                # If we found fieldtrip related keywords, do a deep dive
                if any(k in ["fieldtrip", "fishing village", "field trip", "excursion"] for k in kw_found):
                    print("    *** POTENTIAL MATCH - extracting full page text ***")
                    try:
                        body_text = page.inner_text("body")
                        with open(OUTPUT_DIR / f"match_{safe_label}_text.txt", "w") as fh:
                            fh.write(body_text)
                    except Exception:
                        pass

                    # Extract all links from this page too
                    sub_links = extract_all_links(page)
                    print(f"    Sub-links on this page: {len(sub_links)}")
                    for sl in sub_links:
                        if sl['text']:
                            print(f"      - [{sl['text'][:60]}] -> {sl['href']}")

            # Also save text for pages with "form" or "bulletin" in URL
            page_url_lower = current_url.lower()
            if any(w in page_url_lower for w in ['form', 'bulletin', 'event', 'activit', 'signup', 'registr', 'announce', 'ecollect']):
                try:
                    body_text = page.inner_text("body")
                    with open(OUTPUT_DIR / f"page_{safe_label}_text.txt", "w") as fh:
                        fh.write(body_text)
                    print(f"    Page text saved.")
                except Exception:
                    pass

        # ================================================================
        # STEP 7: Try PowerSchool eCollect forms (common for event signups)
        # ================================================================
        print("\n[STEP 7] Checking PowerSchool eCollect / Forms ...")
        ecollect_urls = [
            f"{base_url}/guardian/ecollectforms.html",
            f"{base_url}/guardian/ecollect/home.html",
            f"{base_url}/guardian/forms/ecollect",
            f"{base_url}/guardian/formlist.html",
            f"{base_url}/guardian/forms",
            f"{base_url}/guardian/myforms.html",
            f"{base_url}/public/ecollect",
        ]

        for eurl in ecollect_urls:
            if eurl in visited_urls:
                continue
            visited_urls.add(eurl)

            print(f"\n  Trying: {eurl}")
            try:
                page.goto(eurl, wait_until="domcontentloaded", timeout=15_000)
            except PlaywrightTimeout:
                print("    Timed out.")
                continue
            except Exception as exc:
                print(f"    Error: {exc}")
                continue

            page.wait_for_timeout(2000)
            current = page.url
            log_page_info(page, f"eCollect: {eurl}")

            safe = eurl.split("/")[-1].replace(".", "_")[:20]
            take_screenshot(page, f"ecollect_{safe}")

            kw_found = search_page_for_keywords(page, KEYWORDS)
            if kw_found:
                print(f"    *** KEYWORDS FOUND: {kw_found}")
                findings.append({"page": f"eCollect {safe}", "url": current, "keywords": kw_found})

                try:
                    body_text = page.inner_text("body")
                    with open(OUTPUT_DIR / f"ecollect_{safe}_text.txt", "w") as fh:
                        fh.write(body_text)
                except Exception:
                    pass

            # Check links on the forms page
            links = extract_all_links(page)
            for link in links:
                link_text = (link.get('text', '') or '').lower()
                if any(kw in link_text for kw in ['fieldtrip', 'fishing', 'field trip', 'excursion', 'event', 'form', 'sign']):
                    print(f"    Interesting link: [{link['text'][:60]}] -> {link['href']}")

        # ================================================================
        # STEP 8: Try searching within Forms/eCollect for specific form links
        # ================================================================
        print("\n[STEP 8] Looking for specific form/event links within iframes ...")

        # Check for iframes (PowerSchool often uses iframes)
        iframes = page.query_selector_all("iframe")
        if iframes:
            print(f"  Found {len(iframes)} iframe(s)")
            for idx, iframe in enumerate(iframes):
                src = iframe.get_attribute("src")
                print(f"    iframe[{idx}] src: {src}")
                if src:
                    try:
                        frame = iframe.content_frame()
                        if frame:
                            frame_text = frame.inner_text("body")
                            print(f"    iframe text length: {len(frame_text)}")
                            with open(OUTPUT_DIR / f"iframe_{idx}_text.txt", "w") as fh:
                                fh.write(frame_text)

                            frame_lower = frame_text.lower()
                            for kw in KEYWORDS:
                                if kw in frame_lower:
                                    print(f"    *** KEYWORD in iframe: {kw}")
                                    findings.append({"page": f"iframe_{idx}", "url": src, "keywords": [kw]})
                    except Exception as exc:
                        print(f"    Could not read iframe: {exc}")

        # ================================================================
        # STEP 9: Check School Bulletin specifically
        # ================================================================
        print("\n[STEP 9] Checking School Bulletin / Announcements ...")
        bulletin_urls = [
            f"{base_url}/guardian/schoolbulletin.html",
            f"{base_url}/guardian/bulletin.html",
            f"{base_url}/guardian/notifications.html",
            f"{base_url}/guardian/announcements.html",
        ]

        for burl in bulletin_urls:
            if burl in visited_urls:
                continue
            visited_urls.add(burl)

            print(f"\n  Trying: {burl}")
            try:
                page.goto(burl, wait_until="domcontentloaded", timeout=15_000)
            except PlaywrightTimeout:
                continue
            except Exception:
                continue

            page.wait_for_timeout(2000)
            log_page_info(page, f"Bulletin: {burl}")
            safe = burl.split("/")[-1].replace(".", "_")[:20]
            take_screenshot(page, f"bulletin_{safe}")

            kw_found = search_page_for_keywords(page, KEYWORDS)
            if kw_found:
                print(f"    *** KEYWORDS FOUND: {kw_found}")
                findings.append({"page": f"Bulletin {safe}", "url": page.url, "keywords": kw_found})
                try:
                    body_text = page.inner_text("body")
                    with open(OUTPUT_DIR / f"bulletin_{safe}_text.txt", "w") as fh:
                        fh.write(body_text)
                except Exception:
                    pass

        # ================================================================
        # STEP 10: Try left-nav expansion and sub-menus
        # ================================================================
        print("\n[STEP 10] Checking for expandable menus / sub-navigation ...")

        # Go back to dashboard first
        try:
            page.goto(f"{base_url}/guardian/home.html", wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(2000)

        # Look for expandable menu items
        expandable = page.query_selector_all('[class*="expand"], [class*="toggle"], [class*="dropdown"], [data-toggle], .has-children, .has-submenu')
        print(f"  Expandable elements: {len(expandable)}")
        for el in expandable:
            try:
                el.click()
                page.wait_for_timeout(500)
            except Exception:
                pass

        if expandable:
            take_screenshot(page, "expanded_menus")
            # Re-extract links after expanding
            all_links = extract_all_links(page)
            for link in all_links:
                link_text = (link.get('text', '') or '').lower()
                if link['href'] not in visited_urls and any(kw in link_text for kw in ['form', 'event', 'activit', 'sign', 'bulletin', 'registr']):
                    print(f"    New link after expand: [{link['text'][:60]}] -> {link['href']}")

        # ================================================================
        # STEP 11: Check for PowerSchool student-specific pages
        # ================================================================
        print("\n[STEP 11] Checking student-specific pages ...")
        student_paths = [
            "/guardian/grades.html",
            "/guardian/attendance.html",
            "/guardian/demographics.html",
            "/guardian/schedulematrix.html",
        ]
        for sp in student_paths:
            sp_url = f"{base_url}{sp}"
            if sp_url in visited_urls:
                continue
            visited_urls.add(sp_url)

            print(f"\n  Trying: {sp_url}")
            try:
                page.goto(sp_url, wait_until="domcontentloaded", timeout=15_000)
            except (PlaywrightTimeout, Exception):
                continue
            page.wait_for_timeout(1500)

            safe = sp.split("/")[-1].replace(".", "_")[:20]
            log_page_info(page, sp)
            take_screenshot(page, f"student_{safe}")

        # ================================================================
        # STEP 12: Full-text search using browser search (Ctrl+F simulation)
        # ================================================================
        print("\n[STEP 12] Doing full HTML source search for 'FieldTrip' across visited pages ...")
        # Go back to home and get the full page source
        try:
            page.goto(f"{base_url}/guardian/home.html", wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(2000)

        html_source = page.content()
        with open(OUTPUT_DIR / "home_source.html", "w") as fh:
            fh.write(html_source)

        html_lower = html_source.lower()
        for kw in ["fieldtrip", "fishing", "field trip", "ecollect", "form"]:
            if kw in html_lower:
                print(f"  Found '{kw}' in home HTML source")

        # ================================================================
        # SUMMARY
        # ================================================================
        print("\n" + "=" * 60)
        print("EXPLORATION SUMMARY")
        print("=" * 60)
        print(f"Total pages visited: {len(visited_urls)}")
        print(f"Screenshots saved to: {OUTPUT_DIR}")
        print(f"\nKeyword findings ({len(findings)}):")
        for f in findings:
            print(f"  Page: {f['page']}")
            print(f"  URL:  {f['url']}")
            print(f"  Keywords: {f['keywords']}")
            print()

        if not findings:
            print("  No keyword matches found on any visited page.")
            print("  The event sign-up may be in a different system (not PowerSchool),")
            print("  or it may use a different URL pattern.")

        # Save findings summary
        with open(OUTPUT_DIR / "findings_summary.json", "w") as fh:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "visited_urls": sorted(visited_urls),
                "findings": findings,
                "screenshot_count": SCREENSHOT_COUNTER,
            }, fh, indent=2)
        print(f"\nFindings saved to: {OUTPUT_DIR / 'findings_summary.json'}")

        browser.close()

    print("\n[powerschool_explore] Done.")


if __name__ == "__main__":
    run_explorer()
