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
import sys
import time
from datetime import datetime
from pathlib import Path


# --- Configuration -----------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ccmux.paths import BUTLER_DIR

FIFO_PATH = Path("/tmp/ccmux/in.butler")
STATE_DIR = BUTLER_DIR
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

    # Capture pane content (-S -50 captures last 50 lines)
    rc, pane_content = run_cmd([
        "tmux", "capture-pane", "-t", session_name, "-p", "-S", "-50"
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


# --- Check: Pending Tasks ----------------------------------------------------

def check_pending_tasks() -> str:
    """Load and display all open pending tasks from the persistent tracker."""
    try:
        from ccmux.pending_tasks import PendingTaskTracker
        tracker = PendingTaskTracker()
        open_tasks = tracker.list_open()
        overdue = tracker.overdue()

        if not open_tasks:
            return "  \u2705 No open pending tasks"

        lines = []
        overdue_ids = {t.task_id for t in overdue}
        for t in open_tasks:
            icon = "\u26a0\ufe0f OVERDUE" if t.task_id in overdue_ids else f"[{t.status}]"
            lines.append(
                f"  - {icon} {t.task_id}: {t.description}"
                + (f" (note: {t.note})" if t.note else "")
                + (f" (follow-up: {t.follow_up_hours}h)" if t.follow_up_hours else "")
                + f" (created: {t.created_at[:16]})"
            )

        header = f"  {len(open_tasks)} open task(s)"
        if overdue:
            header += f", \u26a0\ufe0f {len(overdue)} OVERDUE"
        return header + "\n" + "\n".join(lines)
    except Exception as exc:
        return f"  \u274c Could not load pending tasks: {exc}"


# --- Check: Context Recovery -------------------------------------------------

def check_context_recovery() -> str:
    """Load recent family context and last reflection for behavioral recovery."""
    parts = []

    # Last daily reflection
    try:
        from ccmux.paths import DAILY_REFLECTIONS_DIR
        if DAILY_REFLECTIONS_DIR.exists():
            reflections = sorted(DAILY_REFLECTIONS_DIR.iterdir(), reverse=True)
            if reflections:
                last = reflections[0]
                # Read first 500 chars of the most recent reflection
                text = last.read_text()[:500]
                parts.append(f"  Last reflection ({last.name}):\n    {text.strip()[:300]}...")
            else:
                parts.append("  No daily reflections found")
        else:
            parts.append("  Reflections directory missing")
    except Exception as exc:
        parts.append(f"  Could not load reflections: {exc}")

    # Recent family context (last 10 entries)
    try:
        from ccmux.paths import FAMILY_CONTEXT
        if FAMILY_CONTEXT.exists():
            lines = FAMILY_CONTEXT.read_text().strip().splitlines()
            recent = lines[-10:] if len(lines) > 10 else lines
            parts.append(f"  Family context: {len(lines)} total entries, last {len(recent)}:")
            for line in recent:
                try:
                    entry = json.loads(line)
                    key = entry.get("key", "?")
                    val = entry.get("value", "?")[:80]
                    parts.append(f"    - [{entry.get('category', '?')}] {key}: {val}")
                except json.JSONDecodeError:
                    continue
        else:
            parts.append("  Family context file missing")
    except Exception as exc:
        parts.append(f"  Could not load family context: {exc}")

    # Health tracking last entries (discover children dynamically)
    try:
        from ccmux.paths import HEALTH_DIR
        if HEALTH_DIR.is_dir():
            for child_dir in sorted(HEALTH_DIR.iterdir()):
                if not child_dir.is_dir():
                    continue
                poo_log = child_dir / "poo_log.jsonl"
                if poo_log.exists():
                    lines = poo_log.read_text().strip().splitlines()
                    if lines:
                        last = json.loads(lines[-1])
                        parts.append(
                            f"  Health ({child_dir.name}): last poo {last.get('date', '?')}, "
                            f"status={last.get('status', '?')}"
                        )
    except Exception as exc:
        parts.append(f"  Could not load health data: {exc}")

    if not parts:
        return "  No context data available"
    return "\n".join(parts)


# --- Build Report & Send -----------------------------------------------------

SELFCHECK_REPORT_PATH = STATE_DIR / "selfcheck_report.txt"


def build_report() -> str:
    """Run all checks, write full report to file, return a short FIFO message."""
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

    print("[startup_selfcheck] Checking pending tasks...")
    pending_tasks_report = check_pending_tasks()

    print("[startup_selfcheck] Loading context recovery...")
    context_recovery = check_context_recovery()

    full_report = (
        f"System Self-Check Report\n"
        f"Boot time: {boot_time}\n\n"
        f"Services:\n{services_report}\n\n"
        f"Timers:\n{timers_report}\n\n"
        f"Tmux: {tmux_status}\n\n"
        f"Proxy: {proxy_status}\n\n"
        f"Docker:\n{docker_status}\n\n"
        f"Disk:\n{disk_report}\n\n"
        f"Messages:{message_gap_report}\n\n"
        f"Pending Tasks:\n{pending_tasks_report}\n\n"
        f"Context Recovery:\n{context_recovery}\n\n"
        "RECOVERY ACTIONS (execute in order):\n"
        "1. Review all pending tasks above. Follow up on any that are overdue.\n"
        "2. Scan for missed messages during the gap window using list_messages "
        "with after= parameter set to the last scan timestamp.\n"
        "3. Check household group, admin self-chat, and contact chats.\n"
        "4. Reprocess any missed actionable messages (S3 commands, admin instructions, health reports).\n"
        "5. Send the self-check report to admin via WhatsApp.\n"
        "6. Resume normal operations."
    )

    # Write full report to file (may exceed PIPE_BUF, so send via file)
    SELFCHECK_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SELFCHECK_REPORT_PATH.write_text(full_report)
    print(f"[startup_selfcheck] Full report written to {SELFCHECK_REPORT_PATH}")

    # FIFO message is short — just tells Claude to read the file
    fifo_msg = (
        "Startup self-check completed. "
        f"Full report saved to: {SELFCHECK_REPORT_PATH}\n"
        "READ the report file, then:\n"
        "1. Send a summary to admin via WhatsApp (prefix with \U0001f916)\n"
        "2. Execute all RECOVERY ACTIONS listed in the report\n"
        "3. Resume normal operations"
    )

    return fifo_msg


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
