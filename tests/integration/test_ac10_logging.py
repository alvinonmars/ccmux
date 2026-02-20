"""AC-10: Structured logging.

Tests that key events produce structured log entries with required fields.
Layer: Integration/mock.

T-10-1: full injection flow (receive → ready → inject → broadcast, in order)
T-10-2: crash recovery (crash + restart + backoff_seconds)
T-10-3: invalid input triggers error-level log entry
"""
import asyncio
import json
import logging
import time

from ccmux.detector import ReadyDetector
from ccmux.fifo import Message
from ccmux.lifecycle import LifecycleManager


class _LogCollector:
    """Lightweight replacement for structlog logger that collects records.

    daemon.py uses structlog (not stdlib logging), so caplog cannot capture its
    output.  This collector replaces the module-level ``log`` object and records
    every call as a dict with ``event``, ``log_level``, and any keyword args.
    """

    def __init__(self) -> None:
        self.records: list[dict] = []

    def _log(self, level: str, event: str, **kw) -> None:
        self.records.append({"event": event, "log_level": level, **kw})

    def info(self, event, **kw):
        self._log("info", event, **kw)

    def warning(self, event, **kw):
        self._log("warning", event, **kw)

    def error(self, event, **kw):
        self._log("error", event, **kw)

    def debug(self, event, **kw):
        self._log("debug", event, **kw)


class _FakePane:
    """Minimal fake pane for LifecycleManager tests."""

    def __init__(self, pid: str = "99999"):
        self._pid = pid
        self.sent_keys: list[str] = []

    @property
    def pid(self) -> str:
        return self._pid

    def send_keys(self, cmd: str, enter: bool = True, **kwargs) -> None:
        self.sent_keys.append(cmd)

    def cmd(self, *args) -> object:
        class R:
            stdout = ["❯"]
        return R()


async def test_T10_1_full_injection_flow_logging(
    net_daemon, bare_pane, test_config, fire_hook, tmp_path, monkeypatch
):
    """T-10-1: log contains receive → ready → inject → broadcast in order with fields."""
    collector = _LogCollector()
    monkeypatch.setattr("ccmux.daemon.log", collector)

    d = net_daemon
    d._pane = bare_pane
    d._detector = ReadyDetector(bare_pane, test_config.silence_timeout)
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
            },
            "ts": 1700000000,
        }) + "\n"
    )

    # Phase 1: message arrives
    d._on_message(Message(channel="test", content="hello", ts=int(time.time())))

    # Phase 2: ready detected → triggers inject (via silence monitor callback)
    d._on_silence_ready()
    await asyncio.sleep(0.3)

    # Phase 3: Stop hook → triggers broadcast
    fire_hook("Stop", {
        "session_id": "s-log-test",
        "transcript_path": str(transcript),
    })
    await asyncio.sleep(0.5)

    events = [r["event"] for r in collector.records]

    # All four event types present
    assert "message received" in events, f"events: {events}"
    assert "ready detected" in events, f"events: {events}"
    assert "injecting messages" in events, f"events: {events}"
    assert "broadcast sent" in events, f"events: {events}"

    # Correct order
    idx_recv = events.index("message received")
    idx_ready = events.index("ready detected")
    idx_inject = events.index("injecting messages")
    idx_broadcast = events.index("broadcast sent")
    assert idx_recv < idx_ready < idx_inject < idx_broadcast, (
        f"wrong order: recv={idx_recv} ready={idx_ready} "
        f"inject={idx_inject} broadcast={idx_broadcast}"
    )

    # Required fields per AC-10 table
    recv = collector.records[idx_recv]
    assert recv["channel"] == "test"
    assert recv["content_len"] == 5

    ready = collector.records[idx_ready]
    assert ready["method"] == "timeout"

    inject = collector.records[idx_inject]
    assert inject["message_count"] == 1

    broadcast = collector.records[idx_broadcast]
    assert "subscriber_count" in broadcast


async def test_T10_2_crash_recovery_logging(test_config, caplog):
    """T-10-2: crash + restart logs contain restart_count and backoff_seconds."""
    pane = _FakePane()
    alive = [True]
    restart_event = asyncio.Event()

    test_config.backoff_initial = 0.1
    test_config.backoff_cap = 10
    mgr = LifecycleManager(
        test_config, pane, on_restart=restart_event.set, poll_interval=0.2,
    )
    mgr._is_claude_running = lambda: alive[0]

    mgr.start()
    await asyncio.sleep(0.4)

    with caplog.at_level(logging.DEBUG, logger="ccmux.lifecycle"):
        alive[0] = False  # simulate crash
        await asyncio.wait_for(restart_event.wait(), timeout=3.0)

    mgr.stop()

    # Crash detection log
    crash = [r for r in caplog.records if "claude process died" in r.message]
    assert crash, "missing crash detection log"
    assert hasattr(crash[0], "restart_count")

    # Restart log with backoff_seconds
    restart = [r for r in caplog.records if "restarting claude" in r.message]
    assert restart, "missing restart log"
    assert hasattr(restart[0], "restart_count")
    assert hasattr(restart[0], "backoff_seconds")
    assert restart[0].backoff_seconds >= 0


async def test_T10_3_injection_failure_error_log(
    net_daemon, bare_pane, test_config, monkeypatch
):
    """T-10-3: injection failure produces error-level log entry."""
    collector = _LogCollector()
    monkeypatch.setattr("ccmux.daemon.log", collector)

    d = net_daemon
    d._pane = bare_pane
    d._detector = ReadyDetector(bare_pane, test_config.silence_timeout)
    monkeypatch.setattr(d, "_get_client_activity_ts", lambda: 0)

    # Make injection fail
    def _fail(*args, **kwargs):
        raise RuntimeError("simulated injection failure")

    monkeypatch.setattr("ccmux.daemon.inject_messages", _fail)

    d._message_queue.append(
        Message(channel="test", content="will fail", ts=int(time.time()))
    )
    await d._maybe_inject()

    errors = [r for r in collector.records if r["log_level"] == "error"]
    assert errors, "expected error-level log entry"

    fail_entry = next(r for r in errors if "injection failed" in r["event"])
    assert "simulated injection failure" in fail_entry.get("error", "")
