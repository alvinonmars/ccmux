"""High-level device control — tap, type, swipe, navigate.

Built on top of adb.py and screen.py, provides the action primitives
that an AI agent uses to interact with the device.
"""

from __future__ import annotations

import logging
import time

from .adb import ADB
from .screen import Screen, UIElement

log = logging.getLogger(__name__)


class DeviceController:
    """High-level device control interface."""

    def __init__(self, adb: ADB):
        self.adb = adb
        self.screen = Screen(adb)

    # -- input actions ----------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        """Tap at screen coordinates."""
        self.adb.shell(f"input tap {x} {y}")
        log.debug("tap(%d, %d)", x, y)

    def tap_element(self, element: UIElement) -> None:
        """Tap the center of a UI element."""
        cx, cy = element.center
        self.tap(cx, cy)
        log.info("Tapped element: %s at (%d, %d)", element.label, cx, cy)

    def type_text(self, text: str) -> None:
        """Type text into the currently focused field.

        Spaces are handled by replacing with %s (ADB input encoding).
        """
        escaped = text.replace(" ", "%s").replace("&", "\\&").replace("'", "\\'")
        self.adb.shell(f"input text '{escaped}'")
        log.debug("Typed: %s", text)

    def press_key(self, keycode: int | str) -> None:
        """Press a key by keycode (int) or name (str).

        Common keycodes: HOME=3, BACK=4, ENTER=66, SEARCH=84
        """
        self.adb.shell(f"input keyevent {keycode}")
        log.debug("Key: %s", keycode)

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300
    ) -> None:
        """Swipe from (x1,y1) to (x2,y2)."""
        self.adb.shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    def scroll_down(self) -> None:
        """Scroll down by swiping from center-bottom to center-top."""
        # Assumes ~1080x2400 screen; actual coords are approximate
        self.swipe(540, 1800, 540, 600)

    def scroll_up(self) -> None:
        """Scroll up."""
        self.swipe(540, 600, 540, 1800)

    # -- navigation shortcuts ---------------------------------------------

    def press_home(self) -> None:
        self.press_key(3)

    def press_back(self) -> None:
        self.press_key(4)

    def press_enter(self) -> None:
        self.press_key(66)

    def press_search(self) -> None:
        self.press_key(84)

    # -- compound actions -------------------------------------------------

    def find_and_tap(self, text: str, *, timeout: float = 5.0) -> bool:
        """Find an element by text and tap it. Returns True if found."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elements = self.screen.find_elements(text=text, clickable=True)
            if elements:
                self.tap_element(elements[0])
                return True
            time.sleep(0.5)
        log.warning("Element with text '%s' not found within %.1fs", text, timeout)
        return False

    def type_in_field(self, resource_id: str, text: str) -> bool:
        """Find an input field by resource_id, tap it, and type text."""
        elements = self.screen.find_elements(resource_id=resource_id)
        if not elements:
            log.warning("Input field '%s' not found", resource_id)
            return False
        self.tap_element(elements[0])
        time.sleep(0.3)
        self.type_text(text)
        return True

    def wait_for_element(
        self, *, text: str = "", resource_id: str = "", timeout: float = 10.0
    ) -> UIElement | None:
        """Wait for an element to appear on screen."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elements = self.screen.find_elements(text=text, resource_id=resource_id)
            if elements:
                return elements[0]
            time.sleep(0.5)
        return None

    # -- screenshot -------------------------------------------------------

    def screenshot(self, local_path: str) -> str:
        """Take a screenshot and pull it to the local machine."""
        remote = "/sdcard/screenshot.png"
        self.adb.shell(f"screencap -p {remote}")
        self.adb.run(["pull", remote, local_path])
        return local_path
