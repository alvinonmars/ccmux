"""PowerSchool ADFS SSO login for BrowserSession.

Extracted from scripts/powerschool_checker.py.
Credentials loaded from ~/.ccmux/secrets/powerschool.env.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ccmux.paths import POWERSCHOOL_ENV
from libs.web_agent.browser import BrowserSession

log = logging.getLogger(__name__)

ENV_FILE = POWERSCHOOL_ENV


def load_credentials(env_path: Path = ENV_FILE) -> dict[str, str]:
    """Read key=value pairs from the .env file."""
    creds: dict[str, str] = {}
    if not env_path.exists():
        raise FileNotFoundError(f"Credential file not found: {env_path}")
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
            raise ValueError(f"Missing {key} in {env_path}")
    return creds


def login(session: BrowserSession, creds: dict[str, str] | None = None) -> bool:
    """Perform PowerSchool ADFS SSO login.

    Args:
        session: An already-started BrowserSession.
        creds: Credentials dict with POWERSCHOOL_URL, POWERSCHOOL_USER,
               POWERSCHOOL_PASS. If None, loaded from ENV_FILE.

    Returns:
        True if login succeeded (page URL contains 'guardian').
    """
    if creds is None:
        creds = load_credentials()

    url = creds["POWERSCHOOL_URL"]
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    username = creds["POWERSCHOOL_USER"]
    password = creds["POWERSCHOOL_PASS"]
    page = session.page

    # Step 0: Check if already logged in (cookies restored from state)
    if "guardian" in page.url and "home" in page.url:
        log.info("Already logged in (session restored). URL: %s", page.url)
        return True

    # Step 1: Navigate to PowerSchool
    log.info("Navigating to PowerSchool: %s", url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeout:
        log.warning("Landing page load timed out, continuing")

    # Check if already logged in after navigation
    if "guardian" in page.url and "public" not in page.url:
        log.info("Already logged in after navigation. URL: %s", page.url)
        return True

    # Step 2: Click Parent Sign In -> ADFS redirect
    log.info("Clicking Parent Sign In...")
    parent_btn = page.query_selector("#parentSignIn")
    if parent_btn:
        href = parent_btn.get_attribute("href")
        idp_url = (
            f"{base_url}{href}" if href and href.startswith("/") else href
        )
    else:
        import time
        ts = int(time.time() * 1000)
        idp_url = f"{base_url}/guardian/idp?_userTypeHint=guardian&_={ts}"

    try:
        page.goto(idp_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeout:
        log.warning("IDP redirect timed out, continuing")
    page.wait_for_timeout(3000)
    log.info("Redirected to: %s", page.url)

    # Step 3: Fill ADFS credentials
    log.info("Filling ADFS credentials...")
    login_ok = False

    user_el = page.query_selector("#userNameInput")
    pass_el = page.query_selector("#passwordInput")
    if user_el and pass_el:
        user_el.fill(username)
        pass_el.fill(password)
        login_ok = True
    else:
        for sel in [
            'input[name="loginfmt"]', 'input[type="email"]',
            'input[name="username"]', 'input[id="fieldAccount"]',
        ]:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(username)
                break
        for sel in [
            'input[name="passwd"]', 'input[name="password"]',
            'input[type="password"]',
        ]:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(password)
                login_ok = True
                break

    if not login_ok:
        log.error("Could not find credential fields on: %s", page.url)
        return False

    # Step 4: Submit login
    log.info("Submitting login...")
    submit_btn = page.query_selector("#submitButton")
    if submit_btn:
        submit_btn.click()
    else:
        for sel in [
            'input[type="submit"]', 'button[type="submit"]', '#idSIButton9',
        ]:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                break
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(5000)
    log.info("Post-login URL: %s", page.url)

    # Handle "Stay signed in?" prompt
    try:
        stay = page.query_selector("#idSIButton9")
        if stay and stay.is_visible():
            stay.click()
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            page.wait_for_timeout(3000)
    except Exception:
        pass

    # Verify login
    success = "guardian" in page.url
    if success:
        log.info("PowerSchool login successful")
    else:
        log.error("Login may have failed. Current URL: %s", page.url)
    return success
