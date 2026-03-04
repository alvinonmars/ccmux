"""Main sync monitor loop for KidPad.

Runs as systemd service. Single-instance guard via fcntl.flock PID file.
Logging via syslog identifier 'ccmux-kidpad'.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .adb import ADBError
from .adb_manager import ADBManager
from .butler_data import get_butler_state, get_tomorrow_schedule
from .config import PadAgentConfig
from .constants import (
    DASHBOARD_VERSION,
    DEVICE_KIDPAD_DIR,
    DEVICE_STATE_PATH,
    FK_REST_LOCAL_PORT,
    FK_REST_PORT,
    REPORT_EVERY_N_CYCLES,
)
from .heartbeat import HeartbeatManager
from .lock_manager import LockManager
from .notifier import PadNotifier
from .policy import LockReason, evaluate, load_policy
from .screen_time import ScreenTimeTracker
from .state import StateManager

log = logging.getLogger(__name__)

# Periodic check intervals (in cycles, not seconds)
FK_LIVENESS_CHECK_CYCLES = 10   # ~5 min at 30s interval
FK_PAGE_VERIFY_CYCLES = 10      # ~5 min at 30s interval
FK_RESTART_SETTLE_SECONDS = 2   # wait for FK to finish launching


class PadMonitor:
    """Main sync monitor loop.

    Sync while True + time.sleep(interval) loop.
    Single-instance guard via fcntl.flock PID file.
    """

    def __init__(self, cfg: PadAgentConfig) -> None:
        self._cfg = cfg
        self._policy = load_policy(cfg.policy_path)
        self._tz = ZoneInfo(self._policy.timezone)

        self._adb_mgr = ADBManager(cfg.tailscale_ip, cfg.adb_port)
        self._screen_time = ScreenTimeTracker(self._policy.timezone)
        # FK REST API accessed via ADB port forward (direct network doesn't work)
        self._fk_base_url = f"http://127.0.0.1:{FK_REST_LOCAL_PORT}"
        self._lock_mgr = LockManager(
            adb_mgr=self._adb_mgr,
            fk_password=cfg.fk_password,
            fk_base_url=self._fk_base_url,
            lock_url=cfg.lock_url,
            dashboard_url=cfg.dashboard_url,
        )
        self._heartbeat = HeartbeatManager(self._policy.heartbeat_timeout_seconds)
        self._notifier = PadNotifier(cfg.runtime_dir)
        self._state_mgr = StateManager(cfg.state_path)

        self._cycle_count = 0
        self._shutdown_requested = False
        self._pid_fd: int | None = None

    # -- Lifecycle -----------------------------------------------------------

    def run(self) -> None:
        """Main entry point. Acquire lock, restore state, loop."""
        self._setup_logging()
        self._acquire_pid_lock()

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        try:
            self._restore_state()
            self._initial_connect()
            self._run_loop()
        except Exception:
            log.exception("fatal error in monitor loop")
            raise
        finally:
            self._release_pid_lock()
            log.info("monitor shutdown")

    def _run_loop(self) -> None:
        """Sync main loop: while True + time.sleep."""
        while not self._shutdown_requested:
            cycle_start = time.monotonic()

            try:
                self._cycle()
            except ADBError:
                self._adb_mgr.mark_disconnected()
                self._notifier.notify_adb_status(
                    self._cfg.child_name, "disconnected"
                )
                log.warning("ADB error during cycle, marked disconnected")
            except Exception:
                log.exception("unexpected error during cycle")

            self._persist_state()

            elapsed = time.monotonic() - cycle_start
            if elapsed > self._cfg.monitor_interval:
                log.warning(
                    "cycle exceeded budget: %.1fs > %ds",
                    elapsed,
                    self._cfg.monitor_interval,
                )
            sleep_time = max(0, self._cfg.monitor_interval - elapsed)
            time.sleep(sleep_time)

    # -- Single cycle -------------------------------------------------------

    def _cycle(self) -> None:
        """One monitor cycle."""
        now = datetime.now(self._tz)

        # Phase 1: Connection
        connected = self._adb_mgr.ensure_connected()
        if not connected:
            return  # heartbeat times out naturally

        self._adb_mgr.reset_backoff()
        self._heartbeat.record_successful_cycle()

        # Phase 1b: FK liveness check
        if self._cycle_count % FK_LIVENESS_CHECK_CYCLES == 0:
            if not self._lock_mgr.check_fk_alive():
                log.warning("FK not running, restarting")
                try:
                    self._adb_mgr.shell(
                        "am start -n de.ozerov.fully/.FullyKioskActivity"
                    )
                    time.sleep(FK_RESTART_SETTLE_SECONDS)
                except ADBError:
                    log.warning("failed to restart FK")

        # Phase 2: Observe
        rollover_events = self._screen_time.check_midnight_rollover(now)
        for event in rollover_events:
            self._log_usage_event(event)

        screen_on = self._check_screen_state()
        events = self._screen_time.update(
            screen_on=screen_on,
            is_locked=self._lock_mgr.is_locked,
            now=now,
        )

        # Phase 3: Evaluate (pure CPU)
        evaluation = evaluate(
            policy=self._policy,
            active_minutes_today=self._screen_time.active_minutes_today,
            current_session_minutes=self._screen_time.current_session_minutes,
            minutes_since_last_eye_break=self._screen_time.minutes_since_last_eye_break,
            last_heartbeat_age_seconds=self._heartbeat.last_seen_seconds_ago,
            eye_break_started_at=self._lock_mgr.eye_break_started_at,
            now=now,
            is_locked=self._lock_mgr.is_locked,
        )

        # Phase 4: Act
        transitions = self._lock_mgr.apply_evaluation(evaluation)

        # Handle eye break explicitly (not via apply_evaluation — see contract)
        if evaluation.eye_break_due and not self._lock_mgr.is_locked:
            self._lock_mgr.add_reason(LockReason.EYE_BREAK, now)
            self._screen_time.record_eye_break(now)
            self._notifier.notify_lock_change(
                self._cfg.child_name, "lock", LockReason.EYE_BREAK.value,
                self._lock_mgr.active_reasons,
            )

        if evaluation.eye_break_expired:
            self._lock_mgr.remove_reason(LockReason.EYE_BREAK, now)
            self._notifier.notify_lock_change(
                self._cfg.child_name, "unlock", LockReason.EYE_BREAK.value,
                self._lock_mgr.active_reasons,
            )

        # Session reset on unlock transition (NOT for eye break)
        for action, reason in transitions:
            if action == "unlock" and reason != LockReason.EYE_BREAK:
                self._screen_time.reset_session()

        # Phase 4b: Push state to device
        self._push_dashboard_state(now)

        # Phase 4c: Verify FK page
        if self._cycle_count % FK_PAGE_VERIFY_CYCLES == 0:
            self._verify_fk_page()

        # Phase 5: Report
        for action, reason in transitions:
            self._notifier.notify_lock_change(
                self._cfg.child_name, action, reason.value,
                self._lock_mgr.active_reasons,
            )

        for event in events:
            self._log_usage_event(event)

        if self._cycle_count % REPORT_EVERY_N_CYCLES == 0:
            self._notifier.notify_screen_time_update(
                child_name=self._cfg.child_name,
                active_min=round(self._screen_time.active_minutes_today, 1),
                daily_limit=self._policy.daily_limit_minutes,
                session_min=round(self._screen_time.current_session_minutes, 1),
                session_limit=self._policy.session_limit_minutes,
            )

        self._cycle_count += 1

    # -- Helpers -------------------------------------------------------------

    def _check_screen_state(self) -> bool:
        """Check if screen is on via dumpsys display."""
        try:
            output = self._adb_mgr.shell(
                "dumpsys display | grep mScreenState", timeout=5
            )
            return "ON" in output.upper()
        except ADBError:
            log.warning("failed to check screen state")
            return False

    def _push_dashboard_state(self, now: datetime) -> None:
        """Push state.json to device via ADB (primary, read by XHR)
        and FK REST setStringSetting (secondary)."""

        eye_break_interval = self._policy.eye_break_interval_minutes
        minutes_since = self._screen_time.minutes_since_last_eye_break
        next_eb = max(0, eye_break_interval - minutes_since)

        state = {
            "active_minutes": round(self._screen_time.active_minutes_today, 1),
            "daily_limit": self._policy.daily_limit_minutes,
            "session_minutes": round(
                self._screen_time.current_session_minutes, 1
            ),
            "session_limit": self._policy.session_limit_minutes,
            "next_eye_break_minutes": round(next_eb, 1),
            "is_locked": self._lock_mgr.is_locked,
            "lock_reasons": self._lock_mgr.active_reasons,
            "child_name": self._cfg.child_name,
            "server_epoch_ms": int(now.timestamp() * 1000),
            "heartbeat_timeout_ms": self._policy.heartbeat_timeout_seconds * 1000,
            "seq": self._heartbeat.seq,
            "updated_at": now.isoformat(),
        }

        # Merge butler data (schedule, homework, weather, reminder)
        try:
            from ccmux.paths import DATA_ROOT

            butler = get_butler_state(self._cfg.child_name, DATA_ROOT, now)
            state.update(butler)

            # Tomorrow's schedule for lock screen
            tomorrow = get_tomorrow_schedule(
                DATA_ROOT, self._cfg.child_name, now
            )
            if tomorrow:
                state["tomorrow_schedule"] = tomorrow
        except Exception:
            log.debug("butler data unavailable", exc_info=True)

        state_json = json.dumps(state)

        # Primary: ADB push state.json (dashboard reads via XHR)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".json")
            os.write(fd, state_json.encode())
            os.close(fd)
            self._adb_mgr.push_file(tmp, DEVICE_STATE_PATH)
        except Exception:
            log.warning("failed to push state.json to device")
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

        # Secondary: FK REST setStringSetting (works on free version)
        try:
            requests.get(
                self._fk_base_url,
                params={
                    "cmd": "setStringSetting",
                    "key": "kidpad_state",
                    "value": state_json,
                    "password": self._cfg.fk_password,
                    "type": "json",
                },
                timeout=5,
            )
        except requests.RequestException:
            pass  # non-critical secondary path

    def _verify_fk_page(self) -> None:
        """Verify FK is showing the correct page."""
        actual_url = self._lock_mgr.verify_fk_page()
        if not actual_url:
            return
        if self._lock_mgr.is_locked and not actual_url.startswith(
            self._cfg.lock_url
        ):
            log.warning("FK on wrong page while locked: %s", actual_url)
            self._lock_mgr.assert_device_state()
        elif not self._lock_mgr.is_locked and not actual_url.startswith(
            self._cfg.dashboard_url
        ):
            log.warning("FK on wrong page while unlocked: %s", actual_url)
            self._lock_mgr.assert_device_state()

    def _log_usage_event(self, event: dict) -> None:
        """Append event to daily usage.jsonl file."""
        now = datetime.now(self._tz)
        date_str = now.strftime("%Y-%m-%d")
        usage_file = self._cfg.usage_dir / f"{date_str}.jsonl"

        if "ts" not in event:
            event["ts"] = now.isoformat()

        try:
            with open(usage_file, "a") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            log.warning("failed to write usage event to %s", usage_file)

    def _ensure_device_dir(self) -> None:
        """Create /sdcard/kidpad/ on device."""
        try:
            self._adb_mgr.shell(f"mkdir -p {DEVICE_KIDPAD_DIR}")
        except ADBError:
            log.warning("failed to create device directory")

    def _ensure_dashboard_files(self) -> None:
        """Push dashboard files to device if version changed."""
        dashboard_dir = (
            Path(__file__).resolve().parent / "dashboard"
        )
        if not dashboard_dir.exists():
            log.warning("dashboard directory not found: %s", dashboard_dir)
            return

        # Version check
        try:
            remote_version = self._adb_mgr.shell(
                f"cat {DEVICE_KIDPAD_DIR}version.txt 2>/dev/null"
            )
        except ADBError:
            remote_version = ""

        if remote_version.strip() == DASHBOARD_VERSION:
            log.debug("dashboard version matches, skipping push")
            return

        log.info("pushing dashboard files (version %s)", DASHBOARD_VERSION)
        files = ["index.html", "lock.html", "lock.js", "style.css", "app.js"]
        for fname in files:
            local = dashboard_dir / fname
            if local.exists():
                try:
                    self._adb_mgr.push_file(
                        str(local), f"{DEVICE_KIDPAD_DIR}{fname}"
                    )
                except ADBError:
                    log.warning("failed to push %s", fname)

        # Write version marker
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".txt")
            os.write(fd, DASHBOARD_VERSION.encode())
            os.close(fd)
            self._adb_mgr.push_file(tmp, f"{DEVICE_KIDPAD_DIR}version.txt")
        except Exception:
            log.warning("failed to write version.txt")
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

        # Clear FK cache
        try:
            requests.get(
                self._fk_base_url,
                params={
                    "cmd": "clearCache",
                    "password": self._cfg.fk_password,
                    "type": "json",
                },
                timeout=5,
            )
        except requests.RequestException:
            log.warning("FK clearCache failed")

    # -- State persistence ---------------------------------------------------

    def _persist_state(self) -> None:
        """Save all component state to disk."""
        self._state_mgr.save(
            adb_state=self._adb_mgr.get_state_snapshot(),
            screen_time_state=self._screen_time.get_state_snapshot(),
            lock_state=self._lock_mgr.get_state_snapshot(),
            heartbeat_state=self._heartbeat.get_state_snapshot(),
        )

    def _restore_state(self) -> None:
        """Restore state from disk if available."""
        saved = self._state_mgr.load()
        if saved is None:
            log.info("no saved state found, starting fresh")
            return

        log.info("restoring state from disk")
        if "adb" in saved:
            self._adb_mgr.restore_from_state(saved["adb"])
        if "screen_time" in saved:
            self._screen_time.restore_from_state(saved["screen_time"])
        if "lock" in saved:
            self._lock_mgr.restore_from_state(saved["lock"])
        if "heartbeat" in saved:
            self._heartbeat.restore_from_state(saved["heartbeat"])

    # -- Initial connection --------------------------------------------------

    def _initial_connect(self) -> None:
        """Initial ADB connection + device setup."""
        log.info(
            "connecting to %s:%d", self._cfg.tailscale_ip, self._cfg.adb_port
        )
        if self._adb_mgr.connect():
            log.info("ADB connected")
            self._setup_port_forward()
            self._ensure_device_dir()
            self._ensure_dashboard_files()
            self._lock_mgr.assert_device_state()
            self._notifier.notify_adb_status(
                self._cfg.child_name, "connected"
            )
        else:
            log.warning("initial ADB connection failed, will retry")
            self._notifier.notify_adb_status(
                self._cfg.child_name, "disconnected"
            )

    def _setup_port_forward(self) -> None:
        """Set up ADB port forward for FK REST API access.

        FK REST API (port 2323) is not directly reachable over the network.
        We forward via ADB tunnel: localhost:2323 -> device:2323.
        """
        try:
            serial = f"{self._cfg.tailscale_ip}:{self._cfg.adb_port}"
            subprocess.run(
                ["adb", "-s", serial, "forward",
                 f"tcp:{FK_REST_LOCAL_PORT}", f"tcp:{FK_REST_PORT}"],
                capture_output=True, timeout=5,
            )
            log.info("ADB port forward: localhost:%d -> device:%d",
                      FK_REST_LOCAL_PORT, FK_REST_PORT)
        except Exception:
            log.warning("failed to set up ADB port forward")

    # -- PID lock ------------------------------------------------------------

    def _acquire_pid_lock(self) -> None:
        """Acquire PID lock file. Exit if another instance is running."""
        self._cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._pid_fd = os.open(
                str(self._cfg.pid_file),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            )
            fcntl.flock(self._pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(self._pid_fd, str(os.getpid()).encode())
        except OSError:
            log.error("another monitor instance is already running")
            raise SystemExit(1)

    def _release_pid_lock(self) -> None:
        """Release PID lock file."""
        if self._pid_fd is not None:
            try:
                fcntl.flock(self._pid_fd, fcntl.LOCK_UN)
                os.close(self._pid_fd)
            except OSError:
                pass
            try:
                self._cfg.pid_file.unlink(missing_ok=True)
            except OSError:
                pass

    # -- Signal handling -----------------------------------------------------

    def _handle_signal(self, signum: int, frame: object) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        log.info("received signal %d, shutting down", signum)
        self._shutdown_requested = True

    # -- Logging setup -------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure logging with syslog identifier."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
