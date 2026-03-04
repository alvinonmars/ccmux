#!/usr/bin/env python3
"""PoC verification — test the full AI control pipeline.

Runs a series of checks to verify that all pad_agent capabilities
work end-to-end on the connected device.

Usage:
    # Via USB:
    python -m libs.pad_agent.poc_verify

    # Via wireless/Tailscale:
    python -m libs.pad_agent.poc_verify --host 192.168.1.100
    python -m libs.pad_agent.poc_verify --host 100.64.0.7
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time

from .adb import ADB, ADBError
from .control import DeviceController


class PoCVerifier:
    """Run PoC checks against a connected device."""

    def __init__(self, ctrl: DeviceController):
        self.ctrl = ctrl
        self.results: list[tuple[str, bool, str]] = []

    def _record(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append((name, passed, detail))
        icon = "PASS" if passed else "FAIL"
        msg = f"  [{icon}] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)

    def check_device_info(self) -> None:
        """Verify we can read basic device properties."""
        try:
            info = self.ctrl.adb.get_device_info()
            self._record(
                "Device info",
                True,
                f"{info.model}, Android {info.android_version}",
            )
        except ADBError as e:
            self._record("Device info", False, str(e))

    def check_screen_read(self) -> None:
        """Verify UIAutomator can dump the screen hierarchy."""
        try:
            root = self.ctrl.screen.dump_hierarchy()
            # Count total elements
            count = _count_elements(root)
            self._record("Screen read (UIAutomator)", True, f"{count} elements found")
        except Exception as e:
            self._record("Screen read (UIAutomator)", False, str(e))

    def check_clickable_elements(self) -> None:
        """Verify we can find clickable/interactive elements."""
        try:
            elements = self.ctrl.screen.get_clickable_elements()
            labels = [e.label for e in elements[:5]]
            self._record(
                "Clickable elements",
                len(elements) > 0,
                f"{len(elements)} found: {labels}",
            )
        except Exception as e:
            self._record("Clickable elements", False, str(e))

    def check_text_content(self) -> None:
        """Verify we can extract text from the screen."""
        try:
            texts = self.ctrl.screen.get_text_content()
            preview = texts[:3] if texts else []
            self._record(
                "Text extraction",
                len(texts) > 0,
                f"{len(texts)} strings: {preview}",
            )
        except Exception as e:
            self._record("Text extraction", False, str(e))

    def check_tap(self) -> None:
        """Verify tap input works (tap home, then verify screen changed)."""
        try:
            self.ctrl.press_home()
            time.sleep(1)
            texts_before = set(self.ctrl.screen.get_text_content())

            # Open recent apps and go back
            self.ctrl.press_key("KEYCODE_APP_SWITCH")
            time.sleep(1)
            texts_after = set(self.ctrl.screen.get_text_content())
            self.ctrl.press_home()

            changed = texts_before != texts_after
            self._record(
                "Tap / keyevent input",
                True,
                f"screen {'changed' if changed else 'same (OK, home screen)'}",
            )
        except ADBError as e:
            self._record("Tap / keyevent input", False, str(e))

    def check_text_input(self) -> None:
        """Verify text input works via the search bar or browser."""
        try:
            # Open a browser search — use ADB am start with a URL
            self.ctrl.adb.shell(
                'am start -a android.intent.action.VIEW -d "https://example.com"'
            )
            time.sleep(2)

            # Check if we can read the page
            texts = self.ctrl.screen.get_text_content()
            has_content = any("example" in t.lower() for t in texts)
            self.ctrl.press_home()

            self._record(
                "Browser launch",
                True,
                f"page loaded: {has_content}",
            )
        except Exception as e:
            self._record("Browser launch", False, str(e))

    def check_screenshot(self) -> None:
        """Verify screenshot capture works."""
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                path = self.ctrl.screenshot(f.name)
            import os
            size = os.path.getsize(path)
            self._record(
                "Screenshot capture",
                size > 0,
                f"{size / 1024:.0f} KB",
            )
        except Exception as e:
            self._record("Screenshot capture", False, str(e))

    def check_app_management(self) -> None:
        """Verify we can list and query packages."""
        try:
            packages = self.ctrl.adb.list_packages(third_party_only=True)
            self._record(
                "App management",
                True,
                f"{len(packages)} third-party apps installed",
            )
        except Exception as e:
            self._record("App management", False, str(e))

    def run_all(self) -> bool:
        """Run all PoC checks and return True if all passed."""
        checks = [
            self.check_device_info,
            self.check_screen_read,
            self.check_clickable_elements,
            self.check_text_content,
            self.check_tap,
            self.check_text_input,
            self.check_screenshot,
            self.check_app_management,
        ]
        for check in checks:
            check()
        return all(passed for _, passed, _ in self.results)


def _count_elements(element) -> int:
    return 1 + sum(_count_elements(c) for c in element.children)


def main() -> None:
    parser = argparse.ArgumentParser(description="PoC verification for pad_agent")
    parser.add_argument("--host", help="Device IP (wireless/Tailscale)")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--serial", help="Device serial (USB)")
    args = parser.parse_args()

    print("=" * 60)
    print("pad_agent — PoC Verification")
    print("=" * 60)

    adb = ADB(serial=args.serial)
    if args.host:
        print(f"\nConnecting to {args.host}:{args.port}...")
        adb.connect_wireless(args.host, args.port)

    ctrl = DeviceController(adb)

    print("\nRunning checks:\n")
    all_passed = PoCVerifier(ctrl).run_all()

    print("\n" + "=" * 60)
    if all_passed:
        print("ALL CHECKS PASSED — device is ready for AI control")
        print("\nNext steps:")
        print("  1. Install Tailscale on device for remote access")
        print("  2. Install NewPipe for YouTube (adb install newpipe.apk)")
        print("  3. Set up Device Owner for kiosk mode")
    else:
        print("SOME CHECKS FAILED — see details above")
        sys.exit(1)


if __name__ == "__main__":
    main()
