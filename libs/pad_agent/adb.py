"""ADB connection management — USB, WiFi, and Tailscale.

Handles device discovery, connection lifecycle, and command execution.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

ADB_DEFAULT_PORT = 5555
CONNECT_TIMEOUT_S = 10


@dataclass
class Device:
    """Represents a connected Android device."""

    serial: str  # e.g. "192.168.1.100:5555" or "XXXXXX" (USB)
    model: str = ""
    android_version: str = ""
    is_wireless: bool = False

    @property
    def display_name(self) -> str:
        return self.model or self.serial


class ADBError(Exception):
    """Raised when an ADB command fails."""


class ADB:
    """Wrapper around the adb CLI binary."""

    def __init__(self, serial: str | None = None, adb_path: str | None = None):
        self._adb = adb_path or shutil.which("adb")
        if not self._adb:
            raise ADBError("adb binary not found in PATH")
        self._serial = serial

    # -- low-level --------------------------------------------------------

    def run(
        self,
        args: list[str],
        *,
        timeout: int = CONNECT_TIMEOUT_S,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Execute an adb command and return the result."""
        cmd = [self._adb]
        if self._serial:
            cmd += ["-s", self._serial]
        cmd += args
        log.debug("adb cmd: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ADBError(f"adb timed out after {timeout}s: {' '.join(cmd)}") from exc
        if check and result.returncode != 0:
            raise ADBError(
                f"adb failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        return result

    def shell(self, cmd: str, *, timeout: int = CONNECT_TIMEOUT_S) -> str:
        """Run a command in the device shell and return stdout."""
        result = self.run(["shell", cmd], timeout=timeout)
        return result.stdout.strip()

    # -- connection -------------------------------------------------------

    def connect_wireless(self, host: str, port: int = ADB_DEFAULT_PORT) -> None:
        """Connect to a device over WiFi/Tailscale."""
        target = f"{host}:{port}"
        result = self.run(["connect", target], timeout=CONNECT_TIMEOUT_S)
        if "connected" not in result.stdout.lower():
            raise ADBError(f"Failed to connect to {target}: {result.stdout}")
        self._serial = target
        log.info("Connected to %s", target)

    def disconnect(self) -> None:
        """Disconnect from the current wireless device."""
        if self._serial:
            self.run(["disconnect", self._serial], check=False)

    def enable_tcpip(self, port: int = ADB_DEFAULT_PORT) -> None:
        """Switch a USB-connected device to TCP/IP mode."""
        self.run(["tcpip", str(port)])
        log.info("Device switched to TCP/IP mode on port %d", port)

    # -- device info ------------------------------------------------------

    def list_devices(self) -> list[Device]:
        """List all connected devices."""
        result = self.run(["devices", "-l"], check=False)
        devices = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serial = parts[0]
                model = ""
                for part in parts[2:]:
                    if part.startswith("model:"):
                        model = part.split(":", 1)[1]
                devices.append(
                    Device(
                        serial=serial,
                        model=model,
                        is_wireless=":" in serial,
                    )
                )
        return devices

    def get_device_info(self) -> Device:
        """Get detailed info about the connected device."""
        model = self.shell("getprop ro.product.model")
        version = self.shell("getprop ro.build.version.release")
        serial = self._serial or self.shell("getprop ro.serialno")
        return Device(
            serial=serial,
            model=model,
            android_version=version,
            is_wireless=bool(self._serial and ":" in self._serial),
        )

    # -- app management ---------------------------------------------------

    def install_apk(self, apk_path: str) -> None:
        """Install an APK on the device."""
        self.run(["install", "-r", apk_path], timeout=120)
        log.info("Installed %s", apk_path)

    def launch_app(self, package: str, activity: str | None = None) -> None:
        """Launch an app by package name."""
        if activity:
            self.shell(f"am start -n {package}/{activity}")
        else:
            # Use monkey to launch the default activity
            self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
        log.info("Launched %s", package)

    def force_stop(self, package: str) -> None:
        """Force-stop an app."""
        self.shell(f"am force-stop {package}")

    def list_packages(self, *, third_party_only: bool = False) -> list[str]:
        """List installed packages."""
        flag = "-3" if third_party_only else ""
        output = self.shell(f"pm list packages {flag}")
        return [line.replace("package:", "") for line in output.splitlines()]

    def disable_package(self, package: str) -> None:
        """Disable a package (hide without uninstall, no root needed)."""
        self.shell(f"pm disable-user --user 0 {package}")
        log.info("Disabled %s", package)
