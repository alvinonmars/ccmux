#!/usr/bin/env python3
"""One-time device setup — run this after connecting a new device.

Guides through enabling required settings and verifies the device
is ready for AI control.

Usage:
    # USB-connected device (first time):
    python -m libs.pad_agent.setup_device

    # Switch to wireless after USB setup:
    python -m libs.pad_agent.setup_device --wifi 192.168.1.100

    # Connect via Tailscale:
    python -m libs.pad_agent.setup_device --wifi 100.64.0.7
"""

from __future__ import annotations

import argparse
import sys

from .adb import ADB, ADBError


def check_prerequisites(adb: ADB) -> list[str]:
    """Check device readiness and return a list of issues found."""
    issues: list[str] = []

    # Check ADB connection
    try:
        devices = adb.list_devices()
    except ADBError as e:
        return [f"ADB not working: {e}"]

    if not devices:
        return ["No devices found. Check USB cable and USB debugging setting."]

    # Get device info
    info = adb.get_device_info()
    print(f"  Device: {info.model}")
    print(f"  Android: {info.android_version}")
    print(f"  Serial: {info.serial}")
    print(f"  Wireless: {info.is_wireless}")

    # Check Android version
    try:
        ver = int(info.android_version.split(".")[0])
        if ver < 11:
            issues.append(
                f"Android {info.android_version} — wireless debugging requires "
                "Android 11+. USB ADB still works."
            )
    except ValueError:
        pass

    # Check security settings (Xiaomi-specific)
    security_setting = adb.shell(
        "settings get global development_settings_enabled"
    )
    if security_setting != "1":
        issues.append("Developer options may not be enabled.")

    return issues


def check_adb_input(adb: ADB) -> bool:
    """Test if ADB can simulate input (requires Security Settings on Xiaomi)."""
    try:
        # Try to get screen size — if this works, basic shell is fine
        size = adb.shell("wm size")
        print(f"  Screen size: {size}")

        # Try input simulation — this fails without Security Settings on MIUI
        adb.shell("input keyevent 0")  # KEYCODE_UNKNOWN, harmless
        print("  Input simulation: OK")
        return True
    except ADBError:
        print("  Input simulation: FAILED")
        print("  -> On Xiaomi/Redmi: enable 'USB debugging (Security settings)'")
        print("     Settings > Additional settings > Developer options")
        return False


def check_uiautomator(adb: ADB) -> bool:
    """Test if UIAutomator dump works."""
    try:
        result = adb.shell("uiautomator dump /sdcard/test_dump.xml", timeout=15)
        if "dumped" in result.lower() or "xml" in result.lower():
            adb.shell("rm /sdcard/test_dump.xml")
            print("  UIAutomator dump: OK")
            return True
        print(f"  UIAutomator dump: unexpected output: {result}")
        return False
    except ADBError as e:
        print(f"  UIAutomator dump: FAILED ({e})")
        return False


def setup_wireless(adb: ADB, host: str, port: int = 5555) -> bool:
    """Switch device to wireless ADB mode."""
    try:
        adb.enable_tcpip(port)
        print(f"  TCP/IP mode enabled on port {port}")
        print(f"  Connecting to {host}:{port}...")
        adb.connect_wireless(host, port)
        print("  Wireless connection: OK")
        return True
    except ADBError as e:
        print(f"  Wireless setup failed: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Setup Android device for AI control")
    parser.add_argument("--wifi", metavar="HOST", help="Device IP for wireless ADB")
    parser.add_argument("--port", type=int, default=5555, help="ADB port (default 5555)")
    parser.add_argument("--serial", help="Device serial for USB connection")
    args = parser.parse_args()

    print("=" * 60)
    print("pad_agent — Device Setup")
    print("=" * 60)

    adb = ADB(serial=args.serial)

    # Step 1: Connection
    print("\n[1/4] Checking connection...")
    if args.wifi:
        try:
            adb.connect_wireless(args.wifi, args.port)
        except ADBError:
            print("  Wireless connection failed. Trying USB first...")
            issues = check_prerequisites(adb)
            if issues:
                print("\n  Issues found:")
                for issue in issues:
                    print(f"    - {issue}")
                sys.exit(1)
            if not setup_wireless(adb, args.wifi, args.port):
                sys.exit(1)
    else:
        issues = check_prerequisites(adb)
        if issues:
            print("\n  Issues found:")
            for issue in issues:
                print(f"    - {issue}")
            sys.exit(1)

    # Step 2: Input simulation
    print("\n[2/4] Testing input simulation...")
    input_ok = check_adb_input(adb)

    # Step 3: UIAutomator
    print("\n[3/4] Testing UIAutomator (screen reading)...")
    ui_ok = check_uiautomator(adb)

    # Step 4: Summary
    print("\n[4/4] Summary")
    print("-" * 40)
    info = adb.get_device_info()
    status = {
        "ADB connection": True,
        "Input simulation": input_ok,
        "UIAutomator": ui_ok,
    }
    all_ok = all(status.values())

    for name, ok in status.items():
        icon = "PASS" if ok else "FAIL"
        print(f"  [{icon}] {name}")

    if all_ok:
        print(f"\n  Device {info.model} is ready for AI control!")
        if not info.is_wireless and not args.wifi:
            print(f"\n  Next step — switch to wireless:")
            print(f"    python -m libs.pad_agent.setup_device --wifi <DEVICE_IP>")
    else:
        print("\n  Fix the issues above and re-run this script.")
        sys.exit(1)


if __name__ == "__main__":
    main()
