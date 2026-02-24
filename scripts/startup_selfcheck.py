#!/usr/bin/env python3
"""
Startup self-check — comprehensive system health report on boot.

Runs all checks locally in Python (no shelling out to ccmux-deploy),
builds a detailed report, and sends it to Claude via FIFO so Claude
can forward it to admin via WhatsApp self-chat.

Checks performed:
    - systemd user services (whatsapp-bridge, ccmux, ccmux-wa-notifier)
    - systemd ccmux-* timers (active status, next trigger)
    - tmux session health (exists, prompt visible, no errors)
    - proxy reachability (port 8118)
    - Docker containers (surfshark-gluetun)
    - disk space (/ and /home)
    - missed messages gap (time since last scan vs. service start)

Output channel: [butler]
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path


# --- Configuration -----------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIFO_PATH = Path("/tmp/ccmux/in.butler")
STATE_DIR = PROJECT_ROOT / "data" / "household" / "butler"
LAST_SCAN_FILE = STATE_DIR / "last_scan.json"
NOW = datetime.now()
NOW_ISO = NOW.isoformat()


# --- FIFO notification -------------------------------------------------------

def notify_ccmux(
    content: str, max_retries: int = 10, retry_delay: float = 3.0
) -> bool:
    """Write a butler channel message to the ccmux FIFO.

    Uses O_WRONLY|O_NONBLOCK. Retries with backoff when the daemon
    is not yet ready (ENXIO — no reader on the FIFO), which happens
    during boot when a timer fires before ccmux opens its FIFOs.
    """
    payload = json.dumps({
        "channel": "butler",
        "content": content,
        "ts": int(time.time()),
    })
    payload_bytes = (payload + "\n").encode()

    if len(payload_bytes) > 4096:
        print(f"  WARNING: Payload {len(payload_bytes)} bytes exceeds PIPE_BUF")
        return False

    fifo_dir = FIFO_PATH.parent
    fifo_dir.mkdir(parents=True, exist_ok=True)
    if not FIFO_PATH.exists():
        os.mkfifo(str(FIFO_PATH))
        print(f"  Created FIFO: {FIFO_PATH}")

    for attempt in range(1, max_retries + 1):
        try:
            fd = os.open(str(FIFO_PATH), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, payload_bytes)
                print(f"  Notification sent to ccmux ({len(payload_bytes)} bytes)")
                return True
            finally:
                os.close(fd)
        except OSError as exc:
            if attempt < max_retries:
                print(
                    f"  FIFO write attempt {attempt}/{max_retries} failed "
                    f"({exc}), retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                print(
                    f"  WARNING: FIFO write failed after {max_retries} "
                    f"attempts (ccmux not running?): {exc}"
                )
                return False
    return False


# --- Utility -----------------------------------------------------------------

def run_cmd(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run a command and return (returncode, stdout). Stderr merged into stdout."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr.strip():
            output = output + "\n" + result.stderr.strip() if output else result.stderr.strip()
        return result.returncode, output
    except FileNotFoundError:
        return -1, f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, f"Command timed out after {timeout}s"
    except Exception as exc:
        return -1, str(exc)


def systemctl_user(*args: str, timeout: int = 10) -> tuple[int, str]:
    """Shorthand for systemctl --user <args>."""
    return run_cmd(["systemctl", "--user", *args], timeout=timeout)


# --- Check: Services ---------------------------------------------------------

def check_services() -> str:
    """Check systemd user services: whatsapp-bridge, ccmux, ccmux-wa-notifier."""
    services = ["whatsapp-bridge", "ccmux", "ccmux-wa-notifier"]
    lines = []

    for svc in services:
        # is-active
        rc_active, state = systemctl_user("is-active", svc)
        state = state.strip() if state else "unknown"

        # Get PID and uptime from show
        _, pid_str = systemctl_user("show", svc, "--property=MainPID", "--value")
        pid = pid_str.strip() if pid_str else "?"

        _, ts_str = systemctl_user(
            "show", svc, "--property=ActiveEnterTimestamp", "--value"
        )
        uptime_str = ""
        if ts_str and ts_str.strip():
            try:
                # Parse systemd timestamp like "Tue 2026-02-24 08:30:00 HKT"
                # Remove weekday prefix and timezone suffix for parsing
                raw = ts_str.strip()
                # Try multiple formats
                for fmt in [
                    "%a %Y-%m-%d %H:%M:%S %Z",
                    "%Y-%m-%d %H:%M:%S %Z",
                    "%a %Y-%m-%d %H:%M:%S",
                ]:
                    try:
                        start_dt = datetime.strptime(raw, fmt)
                        delta = NOW - start_dt
                        total_secs = int(delta.total_seconds())
                        if total_secs < 0:
                            uptime_str = "just started"
                        elif total_secs < 60:
                            uptime_str = f"{total_secs}s"
                        elif total_secs < 3600:
                            uptime_str = f"{total_secs // 60}m {total_secs % 60}s"
                        elif total_secs < 86400:
                            hours = total_secs // 3600
                            mins = (total_secs % 3600) // 60
                            uptime_str = f"{hours}h {mins}m"
                        else:
                            days = total_secs // 86400
                            hours = (total_secs % 86400) // 3600
                            uptime_str = f"{days}d {hours}h"
                        break
                    except ValueError:
                        continue
            except Exception:
                uptime_str = ts_str.strip()

        if not uptime_str:
            uptime_str = "n/a"

        icon = "\u2705" if state == "active" else "\u274c"
        pid_display = pid if pid and pid != "0" else "n/a"
        lines.append(f"  {icon} {svc}: {state} (PID {pid_display}, up {uptime_str})")

    return "\n".join(lines)


# --- Check: Timers -----------------------------------------------------------

def check_timers() -> str:
    """Check all ccmux-* timers: is-active, next trigger."""
    # List all ccmux-* timer units
    rc, output = systemctl_user("list-timers", "--no-pager", "--no-legend")
    lines = []

    if rc != 0 or not output:
        # Fallback: try to list timer units directly
        rc2, output2 = systemctl_user(
            "list-units", "--type=timer", "--no-pager", "--no-legend", "--all"
        )
        if not output2:
            return "  \u274c Could not list timers"
        output = output2

    # Parse list-timers output; each line contains timer info
    # Format: NEXT LEFT LAST PASSED UNIT ACTIVATES
    # We look for ccmux-* timers
    found_any = False
    for line in output.splitlines():
        if "ccmux" not in line.lower():
            continue
        found_any = True

        # Extract the timer unit name
        parts = line.split()
        timer_name = None
        for part in parts:
            if "ccmux" in part.lower() and part.endswith(".timer"):
                timer_name = part
                break

        if not timer_name:
            # Try to find any part containing ccmux
            for part in parts:
                if "ccmux" in part.lower():
                    timer_name = part
                    break

        if not timer_name:
            continue

        # Get detailed status
        rc_active, state = systemctl_user("is-active", timer_name)
        state = state.strip() if state else "unknown"

        # Next trigger time
        _, next_str = systemctl_user(
            "show", timer_name, "--property=NextElapseUSecRealtime", "--value"
        )
        next_trigger = next_str.strip() if next_str and next_str.strip() else "n/a"

        icon = "\u2705" if state in ("active", "waiting") else "\u274c"
        display_name = timer_name.replace(".timer", "")
        lines.append(f"  {icon} {display_name}: {state}, next: {next_trigger}")

    if not found_any:
        # Try explicit listing of known timer patterns
        rc3, output3 = systemctl_user(
            "list-units", "ccmux-*", "--type=timer", "--no-pager", "--no-legend", "--all"
        )
        if output3:
            for line in output3.splitlines():
                parts = line.split()
                if parts:
                    timer_name = parts[0]
                    rc_active, state = systemctl_user("is-active", timer_name)
                    state = state.strip() if state else "unknown"
                    icon = "\u2705" if state in ("active", "waiting") else "\u274c"
                    display_name = timer_name.replace(".timer", "")
                    lines.append(f"  {icon} {display_name}: {state}")
                    found_any = True

    if not lines:
        return "  \u26a0\ufe0f No ccmux-* timers found"

    return "\n".join(lines)


# --- Check: Tmux Session -----------------------------------------------------

def check_tmux() -> str:
    """Check tmux session: exists, prompt visible, no errors."""
    session_name = "ccmux-claude-code-hub"

    # Check if session exists
    rc, _ = run_cmd(["tmux", "has-session", "-t", session_name])
    if rc != 0:
        return f"\u274c Session '{session_name}' not found"

    # Capture pane content
    rc, pane_content = run_cmd([
        "tmux", "capture-pane", "-t", session_name, "-p", "-l", "50"
    ])
    if rc != 0:
        return f"\u2705 Session exists, but could not capture pane: {pane_content}"

    # Check for prompt
    has_prompt = "\u276f" in pane_content or ">" in pane_content

    # Check for errors
    error_patterns = ["API Error", "Please run /login", "Error:", "FATAL", "panic"]
    found_errors = []
    for pattern in error_patterns:
        if pattern.lower() in pane_content.lower():
            found_errors.append(pattern)

    parts = [f"\u2705 Session '{session_name}' exists"]
    if has_prompt:
        parts.append("prompt visible")
    else:
        parts.append("\u26a0\ufe0f prompt not detected")

    if found_errors:
        parts.append(f"\u274c Errors found: {', '.join(found_errors)}")

    return ", ".join(parts)


# --- Check: Proxy ------------------------------------------------------------

def check_proxy() -> str:
    """Check if proxy port 8118 is reachable."""
    rc, output = run_cmd(["nc", "-z", "127.0.0.1", "8118"], timeout=5)
    if rc == 0:
        return "\u2705 Port 8118 reachable"
    else:
        return f"\u274c Port 8118 unreachable ({output})"


# --- Check: Docker Containers ------------------------------------------------

def check_docker() -> str:
    """Check surfshark-gluetun container: running, healthy."""
    container_name = "surfshark-gluetun"

    # Check if docker is available
    if not shutil.which("docker"):
        return f"\u274c docker command not found"

    # Get container state and health
    rc, output = run_cmd([
        "docker", "inspect",
        "--format", "{{.State.Status}}|{{.State.Health.Status}}|{{.State.StartedAt}}",
        container_name,
    ], timeout=10)

    if rc != 0:
        # Try docker ps as fallback
        rc2, output2 = run_cmd(["docker", "ps", "--filter", f"name={container_name}", "--format", "{{.Status}}"])
        if rc2 != 0 or not output2:
            return f"\u274c {container_name}: not found or docker not accessible"
        return f"\u26a0\ufe0f {container_name}: {output2}"

    parts = output.split("|")
    status = parts[0] if len(parts) > 0 else "unknown"
    health = parts[1] if len(parts) > 1 else ""
    started_at = parts[2] if len(parts) > 2 else ""

    # Format started_at to something readable
    uptime_str = ""
    if started_at:
        try:
            # Docker timestamps: 2026-02-24T08:30:00.123456789Z
            clean_ts = re.sub(r"\.\d+Z$", "", started_at)
            start_dt = datetime.fromisoformat(clean_ts)
            delta = NOW - start_dt
            total_secs = int(delta.total_seconds())
            if total_secs < 3600:
                uptime_str = f"{total_secs // 60}m"
            elif total_secs < 86400:
                uptime_str = f"{total_secs // 3600}h {(total_secs % 3600) // 60}m"
            else:
                uptime_str = f"{total_secs // 86400}d {(total_secs % 86400) // 3600}h"
        except Exception:
            uptime_str = started_at[:19]

    is_ok = status == "running" and health in ("healthy", "")
    icon = "\u2705" if is_ok else "\u274c"

    detail = f"{status}"
    if health:
        detail += f", {health}"
    if uptime_str:
        detail += f", up {uptime_str}"

    return f"  {icon} {container_name}: {detail}"


# --- Check: Disk Space -------------------------------------------------------

def check_disk() -> str:
    """Check disk usage of / and /home. Warn if >80%."""
    lines = []
    for mount in ["/", "/home"]:
        try:
            stat = os.statvfs(mount)
            total = stat.f_frsize * stat.f_blocks
            free = stat.f_frsize * stat.f_bavail
            used = total - free
            pct = (used / total * 100) if total > 0 else 0

            total_gb = total / (1024 ** 3)
            used_gb = used / (1024 ** 3)
            free_gb = free / (1024 ** 3)

            icon = "\u274c" if pct > 90 else ("\u26a0\ufe0f" if pct > 80 else "\u2705")
            lines.append(
                f"  {icon} {mount}: {pct:.1f}% used "
                f"({used_gb:.1f}G / {total_gb:.1f}G, {free_gb:.1f}G free)"
            )
        except OSError as exc:
            lines.append(f"  \u274c {mount}: {exc}")

    return "\n".join(lines)


# --- Check: Missed Messages --------------------------------------------------

def check_message_gap() -> str:
    """Check gap between ccmux service start and last message scan."""
    parts = []

    # Get ccmux.service start time
    _, ts_str = systemctl_user(
        "show", "ccmux", "--property=ActiveEnterTimestamp", "--value"
    )
    service_start = ts_str.strip() if ts_str else None

    # Get last scan timestamp
    last_scan_ts = None
    if LAST_SCAN_FILE.exists():
        try:
            with open(LAST_SCAN_FILE) as fh:
                data = json.load(fh)
            last_scan_ts = data.get("last_scan_ts")
        except (json.JSONDecodeError, OSError):
            pass

    if service_start:
        parts.append(f"ccmux started: {service_start}")
    else:
        parts.append("ccmux start time: unknown")

    if last_scan_ts:
        parts.append(f"last message scan: {last_scan_ts}")

        # Calculate gap
        try:
            scan_dt = datetime.fromisoformat(last_scan_ts)
            gap = NOW - scan_dt
            gap_mins = int(gap.total_seconds() / 60)
            if gap_mins < 0:
                parts.append("scan is ahead of current time (clock skew?)")
            elif gap_mins <= 15:
                parts.append(f"\u2705 gap: {gap_mins}m (recent)")
            elif gap_mins <= 60:
                parts.append(f"\u26a0\ufe0f gap: {gap_mins}m")
            else:
                gap_hours = gap_mins // 60
                remaining_mins = gap_mins % 60
                parts.append(f"\u274c gap: {gap_hours}h {remaining_mins}m (messages may have been missed)")
        except Exception:
            parts.append("could not calculate gap")
    else:
        parts.append("\u274c no previous scan recorded")

    return "\n  ".join([""] + parts).rstrip()


# --- Build Report & Send -----------------------------------------------------

def build_report() -> str:
    """Run all checks and build the full report string."""
    boot_time = NOW.strftime("%Y-%m-%d %H:%M:%S")

    print("[startup_selfcheck] Checking services...")
    services_report = check_services()

    print("[startup_selfcheck] Checking timers...")
    timers_report = check_timers()

    print("[startup_selfcheck] Checking tmux...")
    tmux_status = check_tmux()

    print("[startup_selfcheck] Checking proxy...")
    proxy_status = check_proxy()

    print("[startup_selfcheck] Checking docker...")
    docker_status = check_docker()

    print("[startup_selfcheck] Checking disk...")
    disk_report = check_disk()

    print("[startup_selfcheck] Checking message gap...")
    message_gap_report = check_message_gap()

    content = (
        "Startup self-check completed. Send the following report to admin via WhatsApp self-chat "
        "(use send_message tool, prefix with \U0001f916). Break into multiple messages if needed.\n\n"
        f"\U0001f916 System Self-Check Report\n"
        f"Boot time: {boot_time}\n\n"
        f"\U0001f4e1 Services:\n{services_report}\n\n"
        f"\u23f0 Timers:\n{timers_report}\n\n"
        f"\U0001f5a5\ufe0f Tmux: {tmux_status}\n\n"
        f"\U0001f512 Proxy: {proxy_status}\n\n"
        f"\U0001f433 Docker:\n{docker_status}\n\n"
        f"\U0001f4be Disk:\n{disk_report}\n\n"
        f"\U0001f4e8 Messages:{message_gap_report}"
    )

    return content


def main() -> None:
    print(f"[startup_selfcheck] {NOW_ISO} Starting system self-check...")

    report = build_report()

    print(f"[startup_selfcheck] Report built ({len(report)} chars), sending to ccmux...")
    success = notify_ccmux(report)

    if success:
        print("[startup_selfcheck] Done. Report sent to ccmux.")
    else:
        print("[startup_selfcheck] WARNING: Failed to send report to ccmux.")
        # Print report to stdout as fallback
        print("--- Report (fallback to stdout) ---")
        print(report)
        print("--- End Report ---")


if __name__ == "__main__":
    main()
