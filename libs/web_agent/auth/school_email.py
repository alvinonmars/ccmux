"""School parent email (Outlook Web) login via ADFS SSO.

Entry point: <school-portal-url> → ADFS → Microsoft → Outlook Web
Uses the same credentials as PowerSchool.

Required environment variable:
    CCMUX_SCHOOL_EMAIL_URL: School email portal entry URL (e.g. http://prtmail.example.edu)
"""

from __future__ import annotations

import logging
import os

from libs.web_agent.browser import BrowserSession
from libs.web_agent.auth.powerschool import load_credentials

log = logging.getLogger(__name__)

OUTLOOK_URL = "https://outlook.office365.com/mail/"


def _get_entry_url() -> str:
    url = os.environ.get("CCMUX_SCHOOL_EMAIL_URL")
    if not url:
        raise RuntimeError(
            "CCMUX_SCHOOL_EMAIL_URL environment variable is not set. "
            "Set it to the school email portal entry URL."
        )
    return url


def login(session: BrowserSession, creds: dict[str, str] | None = None) -> bool:
    """Login to School Outlook Web via ADFS SSO.

    Returns True if Outlook inbox is reached.
    """
    if creds is None:
        creds = load_credentials()

    username = creds["POWERSCHOOL_USER"]
    password = creds["POWERSCHOOL_PASS"]
    page = session.page

    # Check if already logged in (cookies restored)
    if "outlook" in page.url and "mail" in page.url:
        log.info("Already logged into Outlook (cookies restored)")
        return True

    # Navigate to email entry point
    entry_url = _get_entry_url()
    log.info("Navigating to %s", entry_url)
    session.goto(entry_url, timeout=30_000)
    session.wait(3000)

    # Check if cookies got us in
    if "outlook" in page.url and "mail" in page.url:
        log.info("Logged in via saved cookies")
        return True

    # Fill ADFS credentials
    info = session.page_info()
    if "adfs" in info["url"] or "login.microsoftonline" in info["url"]:
        user_el = page.query_selector("#userNameInput")
        pass_el = page.query_selector("#passwordInput")
        if user_el and pass_el:
            user_el.fill(username)
            pass_el.fill(password)
            submit = page.query_selector("#submitButton")
            if submit:
                submit.click()
            log.info("Submitted ADFS credentials")
        else:
            # Microsoft online login form
            email_el = page.query_selector('input[name="loginfmt"]')
            if email_el:
                email_el.fill(username)
                session.press("Enter")
                session.wait(3000)
                pass_el = page.query_selector('input[name="passwd"]')
                if pass_el:
                    pass_el.fill(password)
                    session.press("Enter")
                    log.info("Submitted Microsoft credentials")
                else:
                    log.error("Password field not found")
                    return False
            else:
                log.error("No credential fields found at %s", info["url"])
                return False

        session.wait(5000)

    # Handle Microsoft prompts (trust, stay signed in)
    for _ in range(3):
        current_url = page.url
        if "outlook" in current_url and "mail" in current_url:
            break

        # "Do you trust ...?" → Continue
        accept = page.query_selector("#idBtn_Accept")
        if accept and accept.is_visible():
            accept.click()
            session.wait(5000)
            log.info("Clicked 'Continue' (trust prompt)")
            continue

        # "Stay signed in?" → Yes
        stay = page.query_selector("#idSIButton9")
        if stay and stay.is_visible():
            stay.click()
            session.wait(5000)
            log.info("Clicked 'Yes' (stay signed in)")
            continue

        # Generic submit
        for sel in ['input[type="submit"]', 'button[type="submit"]']:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                session.wait(5000)
                break
        else:
            session.wait(3000)

    success = "outlook" in page.url
    if success:
        log.info("Outlook login successful: %s", page.url)
    else:
        log.error("Outlook login failed. URL: %s", page.url)
    return success
