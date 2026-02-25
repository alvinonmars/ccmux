#!/usr/bin/env python3
"""
PowerSchool portal explorer — find the FieldTrip Fishing Village Exploration sign-up.

Logs in via the same ADFS SSO flow as powerschool_checker.py, then explores
the portal for event registration, school forms, or announcements related to
"FieldTrip" or the Feb 28 field trip.

Usage:
    xvfb-run .venv/bin/python3 scripts/powerschool_fieldtrip_explorer.py

Screenshots are saved to ~/.ccmux/data/household/tmp/
"""

import base64
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ccmux.paths import TMP_DIR

ENV_FILE = Path.home() / ".secrets" / "powerschool.env"
OUT_DIR = TMP_DIR
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Pages to probe after login (relative to guardian base)
PROBE_PATHS = [
    "/guardian/home.html",
    "/guardian/forms.html",
    "/guardian/announcements.html",
    "/guardian/documents.html",
    "/guardian/schoolbulletin.html",
    "/guardian/activities.html",
    "/guardian/activityregistration.html",
    "/guardian/calendar.html",
    "/guardian/content.html",
    "/guardian/resources.html",
    "/guardian/studentpages.html",
    "/guardian/permissions.html",
    "/guardian/studentsurvey.html",
    "/guardian/consent.html",
    "/guardian/field_trips.html",
    "/guardian/trips.html",
    "/guardian/events.html",
    "/guardian/registration.html",
    "/guardian/signup.html",
    "/guardian/schoolforms.html",
    "/guardian/customforms.html",
]

SEARCH_KEYWORDS = ["fieldtrip", "fishing village", "field trip", "exploration", "feb 28", "february 28"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_credentials(env_path: Path) -> dict:
    creds: dict = {}
    with open(env_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            creds[key.strip()] = value.strip()
    return creds


def cdp_screenshot(page, path: Path) -> bool:
    """Take a screenshot via CDP, bypassing Playwright font-wait issues."""
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Page.stopLoading")
        time.sleep(0.5)
        result = cdp.send("Page.captureScreenshot", {"format": "png"})
        cdp.detach()
        img_data = base64.b64decode(result["data"])
        path.write_bytes(img_data)
        print(f"  Screenshot saved: {path}  ({len(img_data):,} bytes)")
        return True
    except Exception as exc:
        print(f"  Screenshot failed: {exc}")
        return False


def safe_screenshot(page, name: str) -> Path:
    path = OUT_DIR / f"{name}.png"
    # Try element screenshot first, fall back to CDP
    try:
        page.screenshot(path=str(path), full_page=False, timeout=10_000)
        print(f"  Screenshot saved: {path}")
    except Exception:
        cdp_screenshot(page, path)
    return path


def page_contains_keywords(page) -> list[str]:
    """Return which search keywords are found in the page text."""
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return []
    return [kw for kw in SEARCH_KEYWORDS if kw in text]


def save_html(page, name: str) -> Path:
    path = OUT_DIR / f"{name}.html"
    path.write_text(page.content(), encoding="utf-8")
    print(f"  HTML saved: {path}")
    return path


def safe_goto(page, url: str, label: str) -> bool:
    """Navigate to url; return True if page loaded without error."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except PlaywrightTimeout:
        print(f"  WARNING: {label} — load timed out, continuing.")
    except Exception as exc:
        print(f"  ERROR: {label} — {exc}")
        return False
    page.wait_for_timeout(2000)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    creds = load_credentials(ENV_FILE)
    url = creds["POWERSCHOOL_URL"]
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    username = creds["POWERSCHOOL_USER"]
    password = creds["POWERSCHOOL_PASS"]

    print(f"[fieldtrip_explorer] Starting — output: {OUT_DIR}")
    print(f"[fieldtrip_explorer] Target: {base_url}")
    print()

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

        # Block fonts to prevent screenshot hang
        for pattern in ["**/*.woff*", "**/*.ttf", "**/*.otf", "**/*.eot",
                         "**/fonts.googleapis.com/**", "**/fonts.gstatic.com/**"]:
            page.route(pattern, lambda r: r.abort())

        # ---- Step 1: Load PowerSchool landing page --------------------------
        print("[1/9] Loading PowerSchool landing page ...")
        safe_goto(page, url, "landing page")
        print(f"  URL: {page.url}")
        safe_screenshot(page, "01_landing")

        # ---- Step 2: Trigger ADFS SSO redirect ------------------------------
        print("[2/9] Triggering SSO redirect ...")
        parent_btn = page.query_selector("#parentSignIn")
        if parent_btn:
            href = parent_btn.get_attribute("href")
            idp_url = (
                f"{base_url}{href}" if href and href.startswith("/") else href
            )
        else:
            ts = int(time.time() * 1000)
            idp_url = f"{base_url}/guardian/idp?_userTypeHint=guardian&_={ts}"

        safe_goto(page, idp_url, "IDP redirect")
        page.wait_for_timeout(3000)
        print(f"  Redirected to: {page.url}")
        safe_screenshot(page, "02_idp_form")

        # ---- Step 3: Fill credentials ----------------------------------------
        print("[3/9] Filling credentials ...")
        login_ok = False

        # ADFS form
        user_el = page.query_selector("#userNameInput")
        pass_el = page.query_selector("#passwordInput")
        if user_el and pass_el:
            user_el.fill(username)
            pass_el.fill(password)
            login_ok = True
            print("  Filled ADFS form.")
        else:
            # Microsoft / generic fallback
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
                    login_ok = True
                    print(f"  Password via: {sel}")
                    break

        if not login_ok:
            save_html(page, "debug_login_form")
            safe_screenshot(page, "debug_login_form")
            print("ERROR: Could not locate credential fields. Debug files saved.")
            browser.close()
            sys.exit(1)

        # ---- Step 4: Submit --------------------------------------------------
        print("[4/9] Submitting login ...")
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
        print(f"  Post-login URL: {page.url}")

        # Handle "Stay signed in?" prompt
        try:
            stay = page.query_selector("#idSIButton9")
            if stay and stay.is_visible():
                stay.click()
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                page.wait_for_timeout(3000)
        except Exception:
            pass

        if "guardian" not in page.url:
            save_html(page, "debug_postlogin")
            safe_screenshot(page, "debug_postlogin")
            print(f"ERROR: Login may have failed — unexpected URL: {page.url}")
            browser.close()
            sys.exit(1)

        print("  Login successful.")
        safe_screenshot(page, "04_dashboard")

        # ---- Step 5: Explore the navigation / dashboard ---------------------
        print("[5/9] Exploring dashboard navigation ...")

        # Extract all nav links from the dashboard
        nav_links = []
        try:
            links = page.query_selector_all("a[href]")
            seen = set()
            for link in links:
                href = link.get_attribute("href") or ""
                text = (link.inner_text() or "").strip()
                if href and href not in seen:
                    seen.add(href)
                    nav_links.append({"href": href, "text": text})
        except Exception as exc:
            print(f"  WARNING: Could not extract nav links: {exc}")

        nav_path = OUT_DIR / "nav_links.json"
        nav_path.write_text(json.dumps(nav_links, indent=2, ensure_ascii=False))
        print(f"  {len(nav_links)} nav links saved to {nav_path}")

        # Check if dashboard itself mentions FieldTrip
        kws = page_contains_keywords(page)
        if kws:
            print(f"  *** DASHBOARD MATCHES: {kws} ***")
            safe_screenshot(page, "05_dashboard_match")
            save_html(page, "05_dashboard_match")

        # ---- Step 6: Probe known portal paths --------------------------------
        print("[6/9] Probing known portal paths ...")
        found_pages = []

        for i, path_suffix in enumerate(PROBE_PATHS):
            probe_url = f"{base_url}{path_suffix}"
            print(f"  Probing [{i+1}/{len(PROBE_PATHS)}]: {path_suffix} ...", end=" ", flush=True)

            try:
                page.goto(probe_url, wait_until="domcontentloaded", timeout=15_000)
            except PlaywrightTimeout:
                print("(timeout)")
                continue
            except Exception as exc:
                print(f"(error: {exc})")
                continue

            page.wait_for_timeout(1500)
            current_url = page.url

            # Check if redirected to login (not authenticated)
            if "adfs" in current_url.lower() or "login" in current_url.lower():
                print("(redirected to login)")
                continue

            # Check for 404 / not found indicators
            try:
                body_text = page.inner_text("body").lower()
            except Exception:
                body_text = ""

            if "page not found" in body_text or "404" in body_text[:200]:
                print("(404/not found)")
                continue

            # Check for keyword matches
            kws = [kw for kw in SEARCH_KEYWORDS if kw in body_text]
            slug = path_suffix.strip("/").replace("/", "_")

            if kws:
                print(f"*** MATCH: {kws} ***")
                safe_screenshot(page, f"match_{slug}")
                save_html(page, f"match_{slug}")
                found_pages.append({
                    "url": probe_url,
                    "final_url": current_url,
                    "slug": slug,
                    "keywords_found": kws,
                })
            else:
                # Page loaded OK — note any pages with interesting titles
                try:
                    title = page.title()
                except Exception:
                    title = ""
                print(f"(OK — {title[:50]})")
                # Still take a screenshot of interesting-looking pages
                if any(kw in (title or "").lower() for kw in ["form", "event", "registr", "trip", "activit", "announc", "bulletin", "consent", "permiss"]):
                    safe_screenshot(page, f"interesting_{slug}")
                    save_html(page, f"interesting_{slug}")
                    found_pages.append({
                        "url": probe_url,
                        "final_url": current_url,
                        "slug": slug,
                        "keywords_found": [],
                        "title": title,
                    })

        # ---- Step 7: Follow nav links that look event/form-related ----------
        print("[7/9] Following event/form/registration nav links ...")
        interesting_keywords = [
            "form", "event", "registr", "trip", "activit",
            "announc", "bulletin", "consent", "permiss", "fieldtrip",
            "fishing", "field", "sign", "survey",
        ]

        visited_hrefs = set(p["url"] for p in found_pages)
        visited_hrefs.update(p["final_url"] for p in found_pages)

        for link in nav_links:
            href = link.get_attribute("href") if hasattr(link, "get_attribute") else link.get("href", "")
            text = link.get("text", "").lower() if isinstance(link, dict) else ""
            href = link.get("href", "") if isinstance(link, dict) else ""

            if not href or href in visited_hrefs:
                continue
            if not any(kw in (href + text).lower() for kw in interesting_keywords):
                continue
            if href.startswith("#") or href.startswith("javascript"):
                continue

            full_url = f"{base_url}{href}" if href.startswith("/") else href
            if not full_url.startswith(base_url):
                continue

            visited_hrefs.add(full_url)
            print(f"  Following link: {text!r} -> {href} ...", end=" ", flush=True)

            try:
                page.goto(full_url, wait_until="domcontentloaded", timeout=15_000)
            except PlaywrightTimeout:
                print("(timeout)")
                continue
            except Exception as exc:
                print(f"(error: {exc})")
                continue

            page.wait_for_timeout(1500)

            try:
                body_text = page.inner_text("body").lower()
            except Exception:
                body_text = ""

            kws = [kw for kw in SEARCH_KEYWORDS if kw in body_text]
            slug = href.strip("/").replace("/", "_")[:60]

            if kws:
                print(f"*** MATCH: {kws} ***")
                safe_screenshot(page, f"nav_match_{slug}")
                save_html(page, f"nav_match_{slug}")
                found_pages.append({"url": full_url, "final_url": page.url, "slug": slug, "keywords_found": kws})
            else:
                print(f"(no match)")

        # ---- Step 8: Dedicated search via PowerSchool search box (if any) ---
        print("[8/9] Attempting portal search ...")
        safe_goto(page, f"{base_url}/guardian/home.html", "home")
        page.wait_for_timeout(2000)

        search_selectors = [
            'input[type="search"]',
            'input[name="search"]',
            'input[placeholder*="search" i]',
            '#searchInput',
            '.search-input',
        ]
        search_attempted = False
        for sel in search_selectors:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill("FieldTrip")
                page.keyboard.press("Enter")
                page.wait_for_timeout(3000)
                kws = page_contains_keywords(page)
                print(f"  Search via {sel}: keywords={kws}")
                safe_screenshot(page, "08_search_result")
                save_html(page, "08_search_result")
                search_attempted = True
                if kws:
                    found_pages.append({
                        "url": page.url,
                        "final_url": page.url,
                        "slug": "search_result",
                        "keywords_found": kws,
                    })
                break
        if not search_attempted:
            print("  No search box found on home page.")

        # ---- Step 9: Final dashboard — full screenshot + all links ----------
        print("[9/9] Saving final state ...")
        safe_goto(page, f"{base_url}/guardian/home.html", "home final")
        page.wait_for_timeout(2000)
        safe_screenshot(page, "09_final_dashboard")

        # Save a summary
        summary = {
            "target_event": "FieldTrip Fishing Village Exploration — Feb 28 2026",
            "portal": base_url,
            "pages_with_keyword_match": found_pages,
            "total_nav_links": len(nav_links),
            "probed_paths": PROBE_PATHS,
        }
        summary_path = OUT_DIR / "exploration_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\n  Summary saved: {summary_path}")

        if found_pages:
            print(f"\n*** FOUND {len(found_pages)} PAGE(S) WITH RELEVANT CONTENT ***")
            for p in found_pages:
                print(f"  URL: {p['url']}")
                print(f"  Keywords: {p.get('keywords_found')}")
        else:
            print("\n  No pages containing FieldTrip keywords were found during this run.")
            print("  Check screenshots and nav_links.json for manual review.")

        browser.close()

    print("\n[fieldtrip_explorer] Done.")


if __name__ == "__main__":
    run()
