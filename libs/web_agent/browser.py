"""Browser session with persistent state for screenshot-driven navigation.

Usage by a Claude Code agent:

    from libs.web_agent import BrowserSession

    with BrowserSession(state_dir="/tmp/ps_session",
                        screenshot_dir="data/household/tmp") as browser:
        browser.goto("https://example.com")
        path = browser.screenshot("01_landing")
        info = browser.page_info()
        # ... agent reads screenshot, decides next action ...
        browser.click(text="Sign In")
        path = browser.screenshot("02_after_click")

State (cookies, localStorage) is saved on exit and restored on next start,
so login sessions persist across script invocations.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

log = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Font extensions to block for faster screenshots
_FONT_PATTERNS = ("**/*.woff*", "**/*.ttf", "**/*.otf", "**/*.eot",
                   "**/fonts.googleapis.com/**", "**/fonts.gstatic.com/**")


class BrowserSession:
    """Persistent browser session with screenshot-driven navigation."""

    def __init__(
        self,
        state_dir: str | Path = "/tmp/web_agent_state",
        screenshot_dir: str | Path = "/tmp/web_agent_screenshots",
        headless: bool = True,
        viewport: tuple[int, int] = (1920, 1080),
        block_fonts: bool = True,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.screenshot_dir = Path(screenshot_dir)
        self.headless = headless
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self.block_fonts = block_fonts
        self.user_agent = user_agent

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._screenshot_counter = 0

    # -- Lifecycle -------------------------------------------------------------

    def start(self) -> BrowserSession:
        """Launch browser and restore state if available."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
        )

        state_path = self._state_file
        ctx_kwargs: dict = {
            "viewport": self.viewport,
            "user_agent": self.user_agent,
        }
        if state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)
            log.info("Restored browser state from %s", state_path)

        self._context = self._browser.new_context(**ctx_kwargs)
        self._page = self._context.new_page()

        if self.block_fonts:
            for pattern in _FONT_PATTERNS:
                self._page.route(pattern, lambda r: r.abort())

        return self

    def stop(self) -> None:
        """Save state and close browser."""
        if self._context:
            try:
                self._context.storage_state(path=str(self._state_file))
                log.info("Saved browser state to %s", self._state_file)
            except Exception as exc:
                log.warning("Failed to save browser state: %s", exc)

        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

        self._page = None
        self._context = None
        self._browser = None
        self._pw = None

    def __enter__(self) -> BrowserSession:
        return self.start()

    def __exit__(self, *args: object) -> None:
        self.stop()

    @property
    def _state_file(self) -> Path:
        return self.state_dir / "storage_state.json"

    @property
    def page(self) -> Page:
        """Direct access to the Playwright page for advanced operations."""
        if self._page is None:
            raise RuntimeError("BrowserSession not started")
        return self._page

    # -- Navigation ------------------------------------------------------------

    def goto(self, url: str, timeout: int = 30_000) -> dict:
        """Navigate to URL. Returns page_info dict."""
        page = self.page
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except PlaywrightTimeout:
            log.warning("Page load timed out for %s, continuing", url)
        page.wait_for_timeout(2000)
        return self.page_info()

    def wait(self, ms: int = 3000) -> None:
        """Wait for a specified duration (for JS rendering, etc.)."""
        self.page.wait_for_timeout(ms)

    # -- Screenshot ------------------------------------------------------------

    def screenshot(self, name: str | None = None) -> str:
        """Take a screenshot and return the file path.

        Args:
            name: Optional name (without extension). If None, auto-increments.

        Returns:
            Absolute path to the saved PNG file.
        """
        if name is None:
            self._screenshot_counter += 1
            name = f"{self._screenshot_counter:03d}"

        path = self.screenshot_dir / f"{name}.png"
        try:
            self._cdp_screenshot(path)
        except Exception:
            # Fallback to Playwright screenshot
            try:
                self.page.screenshot(path=str(path))
            except Exception as exc:
                log.error("Screenshot failed: %s", exc)
                return ""

        size = path.stat().st_size if path.exists() else 0
        log.info("Screenshot: %s (%d bytes)", path, size)
        return str(path)

    def _cdp_screenshot(self, path: Path) -> None:
        """Take screenshot via CDP (bypasses font waiting issues)."""
        cdp = self.page.context.new_cdp_session(self.page)
        try:
            cdp.send("Page.stopLoading")
            time.sleep(0.3)
            result = cdp.send("Page.captureScreenshot", {"format": "png"})
        finally:
            cdp.detach()
        img_data = base64.b64decode(result["data"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(img_data)

    # -- Interaction -----------------------------------------------------------

    def click(
        self,
        selector: str | None = None,
        text: str | None = None,
        position: tuple[int, int] | None = None,
        timeout: int = 5000,
    ) -> None:
        """Click an element by CSS selector, visible text, or coordinates.

        Exactly one of selector, text, or position must be provided.
        """
        page = self.page
        if selector:
            page.click(selector, timeout=timeout)
        elif text:
            page.get_by_text(text, exact=False).first.click(timeout=timeout)
        elif position:
            page.mouse.click(position[0], position[1])
        else:
            raise ValueError("Provide one of: selector, text, or position")

    def fill(self, selector: str, value: str) -> None:
        """Fill a form field identified by CSS selector."""
        self.page.fill(selector, value)

    def type_text(self, selector: str, value: str, delay: int = 50) -> None:
        """Type text character-by-character (for inputs that need key events)."""
        self.page.type(selector, value, delay=delay)

    def select(self, selector: str, value: str) -> None:
        """Select an option from a dropdown by value."""
        self.page.select_option(selector, value)

    def press(self, key: str, selector: str | None = None) -> None:
        """Press a keyboard key (e.g., 'Enter', 'Tab')."""
        if selector:
            self.page.press(selector, key)
        else:
            self.page.keyboard.press(key)

    def scroll(self, direction: str = "down", amount: int = 500) -> None:
        """Scroll the page. direction: 'up' or 'down'."""
        delta = amount if direction == "down" else -amount
        self.page.mouse.wheel(0, delta)
        self.page.wait_for_timeout(500)

    # -- Page info -------------------------------------------------------------

    def page_info(self) -> dict:
        """Return structured info about the current page.

        Returns dict with: url, title, text_snippet (first 2000 chars).
        """
        page = self.page
        title = ""
        try:
            title = page.title()
        except Exception:
            pass

        text = ""
        try:
            text = page.inner_text("body")[:2000]
        except Exception:
            pass

        return {
            "url": page.url,
            "title": title,
            "text_snippet": text,
        }

    def get_links(self) -> list[dict]:
        """Return all visible links on the page as [{text, href}]."""
        page = self.page
        links = []
        for el in page.query_selector_all("a[href]"):
            try:
                text = (el.inner_text() or "").strip()
                href = el.get_attribute("href") or ""
                if text or href:
                    links.append({"text": text[:100], "href": href})
            except Exception:
                continue
        return links

    def get_forms(self) -> list[dict]:
        """Return form field information for the current page."""
        page = self.page
        forms = []
        for form in page.query_selector_all("form"):
            fields = []
            for inp in form.query_selector_all(
                "input, select, textarea, button[type='submit']"
            ):
                try:
                    tag = inp.evaluate("el => el.tagName.toLowerCase()")
                    field_info = {
                        "tag": tag,
                        "type": inp.get_attribute("type") or "",
                        "name": inp.get_attribute("name") or "",
                        "id": inp.get_attribute("id") or "",
                        "placeholder": inp.get_attribute("placeholder") or "",
                        "value": inp.get_attribute("value") or "",
                    }
                    if tag == "select":
                        options = inp.query_selector_all("option")
                        field_info["options"] = [
                            {
                                "value": o.get_attribute("value") or "",
                                "text": (o.inner_text() or "").strip(),
                            }
                            for o in options[:20]
                        ]
                    fields.append(field_info)
                except Exception:
                    continue
            forms.append({
                "action": form.get_attribute("action") or "",
                "method": form.get_attribute("method") or "get",
                "fields": fields,
            })
        return forms

    # -- State management ------------------------------------------------------

    def clear_state(self) -> None:
        """Delete saved browser state (forces fresh login on next start)."""
        if self._state_file.exists():
            self._state_file.unlink()
            log.info("Cleared browser state")

    def has_saved_state(self) -> bool:
        """Check if a saved browser state file exists."""
        return self._state_file.exists()
