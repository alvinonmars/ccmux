"""Unit tests for WhatsApp notifier: config loading, SQLite polling, FIFO writing.

Test DB schema matches the real whatsapp-mcp Go bridge (main.go lines 70-86):
- Column ``content`` (not ``body``)
- Column ``timestamp`` stores ISO 8601 strings (Go time.Time via go-sqlite3)
- Column ``is_from_me`` stores BOOLEAN (0/1)
- Primary key is composite (id TEXT, chat_jid TEXT)
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from adapters.wa_notifier.config import WANotifierConfig, load as load_config
from adapters.wa_notifier.notifier import WhatsAppNotifier
from ccmux.fifo import parse_message


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_load_minimal(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        db_path.touch()
        toml_path = tmp_path / "ccmux.toml"
        toml_path.write_text(
            f'[whatsapp]\ndb_path = "{db_path}"\n'
        )
        cfg = load_config(project_root=tmp_path)
        assert cfg.db_path == db_path
        assert cfg.poll_interval == 30
        assert cfg.allowed_chats == []
        assert cfg.ignore_groups is True

    def test_load_all_fields(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        db_path.touch()
        toml_path = tmp_path / "ccmux.toml"
        toml_path.write_text(
            f'[whatsapp]\n'
            f'db_path = "{db_path}"\n'
            f'poll_interval = 10\n'
            f'allowed_chats = ["123@s.whatsapp.net"]\n'
            f'ignore_groups = false\n'
            f'[runtime]\n'
            f'dir = "{tmp_path / "rt"}"\n'
        )
        cfg = load_config(project_root=tmp_path)
        assert cfg.poll_interval == 10
        assert cfg.allowed_chats == ["123@s.whatsapp.net"]
        assert cfg.ignore_groups is False
        assert cfg.runtime_dir == tmp_path / "rt"

    def test_load_missing_db_path_raises(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "ccmux.toml"
        toml_path.write_text("[whatsapp]\n")
        with pytest.raises(ValueError, match="db_path is required"):
            load_config(project_root=tmp_path)

    def test_load_no_whatsapp_section_raises(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "ccmux.toml"
        toml_path.write_text("[project]\nname = 'test'\n")
        with pytest.raises(ValueError, match="db_path is required"):
            load_config(project_root=tmp_path)

    def test_load_zero_poll_interval_raises(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        db_path.touch()
        toml_path = tmp_path / "ccmux.toml"
        toml_path.write_text(
            f'[whatsapp]\ndb_path = "{db_path}"\npoll_interval = 0\n'
        )
        with pytest.raises(ValueError, match="poll_interval must be >= 1"):
            load_config(project_root=tmp_path)


# ---------------------------------------------------------------------------
# Helper: create a test SQLite database matching whatsapp-mcp schema
# ---------------------------------------------------------------------------

# Timestamps must match Go bridge format: "YYYY-MM-DD HH:MM:SS+ZZ:ZZ"
# Use a fixed base time; _BEFORE_BASE is guaranteed to be earlier.
_BASE = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
_FMT = "%Y-%m-%d %H:%M:%S%z"  # produces e.g. "2026-01-15 12:00:00+0800"


def _ts(offset_seconds: int) -> str:
    """Generate a timestamp string offset from _BASE, matching Go bridge format."""
    dt = _BASE + timedelta(seconds=offset_seconds)
    raw = dt.strftime(_FMT)
    # Python strftime %z gives +0800; Go uses +08:00. Insert the colon.
    return raw[:-2] + ":" + raw[-2:]


_BEFORE_BASE = _ts(-1)


def _create_test_db(path: Path) -> None:
    """Create SQLite database matching whatsapp-mcp Go bridge schema."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT,
            chat_jid TEXT,
            sender TEXT,
            content TEXT,
            timestamp TIMESTAMP,
            is_from_me BOOLEAN,
            media_type TEXT,
            filename TEXT,
            url TEXT,
            media_key BLOB,
            file_sha256 BLOB,
            file_enc_sha256 BLOB,
            file_length INTEGER,
            PRIMARY KEY (id, chat_jid),
            FOREIGN KEY (chat_jid) REFERENCES chats(jid)
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_messages(path: Path, messages: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    for i, m in enumerate(messages):
        msg_id = m.get("id", f"msg-{i}")
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(id, chat_jid, sender, content, timestamp, is_from_me) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, m["chat_jid"], m["sender"], m["content"],
             m["timestamp"], m.get("is_from_me", 0)),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Notifier tests
# ---------------------------------------------------------------------------


class TestQueryNewMessages:
    def test_no_new_messages(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        assert n._query_new_messages() == []

    def test_finds_new_messages(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "Hello!", "timestamp": _ts(10)},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE
        result = n._query_new_messages()
        assert len(result) == 1
        assert result[0]["sender"] == "Alice"
        assert result[0]["count"] == 1
        assert result[0]["preview"] == "Hello!"

    def test_aggregates_by_chat(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "First", "timestamp": _ts(10)},
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "Second", "timestamp": _ts(20)},
            {"chat_jid": "456@s.whatsapp.net", "sender": "Bob",
             "content": "Hi", "timestamp": _ts(15)},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE
        result = n._query_new_messages()
        assert len(result) == 2
        alice = next(s for s in result if s["sender"] == "Alice")
        assert alice["count"] == 2
        assert alice["preview"] == "Second"  # last message

    def test_ignores_own_messages(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "123@s.whatsapp.net", "sender": "Me",
             "content": "My reply", "timestamp": _ts(10), "is_from_me": 1},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE
        assert n._query_new_messages() == []

    def test_filters_by_allowed_chats(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "Allowed", "timestamp": _ts(10)},
            {"chat_jid": "999@s.whatsapp.net", "sender": "Spam",
             "content": "Blocked", "timestamp": _ts(11)},
        ])
        cfg = WANotifierConfig(
            db_path=db_path, runtime_dir=tmp_path,
            allowed_chats=["123@s.whatsapp.net"],
        )
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE
        result = n._query_new_messages()
        assert len(result) == 1
        assert result[0]["sender"] == "Alice"

    def test_filters_groups(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "DM", "timestamp": _ts(10)},
            {"chat_jid": "group@g.us", "sender": "Bob",
             "content": "Group msg", "timestamp": _ts(11)},
        ])
        cfg = WANotifierConfig(
            db_path=db_path, runtime_dir=tmp_path, ignore_groups=True,
        )
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE
        result = n._query_new_messages()
        assert len(result) == 1
        assert result[0]["sender"] == "Alice"

    def test_includes_groups_when_not_ignored(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "group@g.us", "sender": "Bob",
             "content": "Group msg", "timestamp": _ts(10)},
        ])
        cfg = WANotifierConfig(
            db_path=db_path, runtime_dir=tmp_path, ignore_groups=False,
        )
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE
        result = n._query_new_messages()
        assert len(result) == 1

    def test_updates_last_seen_ts(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "Msg1", "timestamp": _ts(10)},
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "Msg2", "timestamp": _ts(50)},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE
        n._query_new_messages()
        assert n.last_seen_ts == _ts(50)

    def test_ignores_empty_content(self, tmp_path: Path) -> None:
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "", "timestamp": _ts(10)},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE
        assert n._query_new_messages() == []


class TestWriteNotification:
    def test_writes_json_to_fifo(self, tmp_path: Path) -> None:
        runtime = tmp_path / "rt"
        runtime.mkdir()
        fifo_path = runtime / "in.whatsapp"
        os.mkfifo(str(fifo_path))

        cfg = WANotifierConfig(
            db_path=tmp_path / "messages.db",
            runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)

        # Open FIFO for reading (non-blocking) so the write doesn't block
        read_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            summaries = [
                {"chat_id": "123@s.whatsapp.net", "sender": "+30000000000",
                 "count": 2, "preview": "Hey, are you free?"},
            ]
            n._write_notification(summaries)

            data = os.read(read_fd, 4096)
            payload = json.loads(data.decode().strip())
            assert payload["channel"] == "whatsapp"
            assert "+30000000000" in payload["content"]
            assert "2 msgs" in payload["content"]
            assert "list_messages" in payload["content"]
            assert isinstance(payload["ts"], int)
        finally:
            os.close(read_fd)

    def test_truncates_long_preview(self, tmp_path: Path) -> None:
        runtime = tmp_path / "rt"
        runtime.mkdir()
        fifo_path = runtime / "in.whatsapp"
        os.mkfifo(str(fifo_path))

        cfg = WANotifierConfig(
            db_path=tmp_path / "messages.db",
            runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)

        read_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            summaries = [
                {"chat_id": "123@s.whatsapp.net", "sender": "Alice",
                 "count": 1, "preview": "A" * 100},
            ]
            n._write_notification(summaries)

            data = os.read(read_fd, 4096)
            payload = json.loads(data.decode().strip())
            # Preview should be truncated to 57 chars + "..."
            assert "..." in payload["content"]
        finally:
            os.close(read_fd)


class TestFifoLifecycle:
    def test_ensure_fifo_creates_fifo(self, tmp_path: Path) -> None:
        runtime = tmp_path / "rt"
        cfg = WANotifierConfig(
            db_path=tmp_path / "messages.db",
            runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)
        n._ensure_fifo()
        fifo_path = runtime / "in.whatsapp"
        assert fifo_path.exists()
        assert fifo_path.is_fifo()

    def test_cleanup_fifo_removes_fifo(self, tmp_path: Path) -> None:
        runtime = tmp_path / "rt"
        runtime.mkdir()
        fifo_path = runtime / "in.whatsapp"
        os.mkfifo(str(fifo_path))
        cfg = WANotifierConfig(
            db_path=tmp_path / "messages.db",
            runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)
        n._cleanup_fifo()
        assert not fifo_path.exists()

    def test_ensure_fifo_idempotent(self, tmp_path: Path) -> None:
        runtime = tmp_path / "rt"
        runtime.mkdir()
        fifo_path = runtime / "in.whatsapp"
        os.mkfifo(str(fifo_path))
        cfg = WANotifierConfig(
            db_path=tmp_path / "messages.db",
            runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)
        n._ensure_fifo()  # should not raise
        assert fifo_path.is_fifo()


class TestIsGroupJid:
    def test_group_jid(self) -> None:
        assert WhatsAppNotifier._is_group_jid("120363123@g.us") is True

    def test_personal_jid(self) -> None:
        assert WhatsAppNotifier._is_group_jid("30000000000@s.whatsapp.net") is False

    def test_empty_string(self) -> None:
        assert WhatsAppNotifier._is_group_jid("") is False


# ---------------------------------------------------------------------------
# Gap 1: No-duplicate invariant — second poll returns nothing
# ---------------------------------------------------------------------------


class TestNoDuplicateNotification:
    def test_second_query_returns_empty(self, tmp_path: Path) -> None:
        """Core invariant: after polling once, the same messages must not reappear."""
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "123@s.whatsapp.net", "sender": "Alice",
             "content": "Hello!", "timestamp": _ts(10)},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = _BEFORE_BASE

        first = n._query_new_messages()
        assert len(first) == 1

        second = n._query_new_messages()
        assert second == []


# ---------------------------------------------------------------------------
# Gap 2: run() loop — error resilience + graceful shutdown
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_run_survives_sqlite_error_and_stops_gracefully(
        self, tmp_path: Path,
    ) -> None:
        """run() must not crash on a transient SQLite error and must
        clean up the FIFO on shutdown."""
        runtime = tmp_path / "rt"
        # Point to a non-existent db so every query raises sqlite3.Error
        cfg = WANotifierConfig(
            db_path=tmp_path / "nonexistent.db",
            poll_interval=0,  # tight loop for fast test
            runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)

        async def _stop_after_iterations() -> None:
            # Let a few poll cycles execute (with errors), then stop
            await asyncio.sleep(0.15)
            n.stop()

        asyncio.get_event_loop().create_task(_stop_after_iterations())
        await n.run()

        # FIFO must be cleaned up after run() exits
        assert not (runtime / "in.whatsapp").exists()

    async def test_run_creates_and_cleans_fifo(self, tmp_path: Path) -> None:
        """run() creates FIFO on start and removes it on exit."""
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        runtime = tmp_path / "rt"
        cfg = WANotifierConfig(
            db_path=db_path, poll_interval=0, runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)

        fifo_path = runtime / "in.whatsapp"

        async def _verify_and_stop() -> None:
            await asyncio.sleep(0.05)
            assert fifo_path.exists() and fifo_path.is_fifo()
            n.stop()

        asyncio.get_event_loop().create_task(_verify_and_stop())
        await n.run()

        assert not fifo_path.exists()


# ---------------------------------------------------------------------------
# Gap 3: FIFO write with no reader raises OSError (ENXIO)
# ---------------------------------------------------------------------------


class TestFifoNoReader:
    def test_write_raises_when_no_reader(self, tmp_path: Path) -> None:
        """O_WRONLY | O_NONBLOCK on a FIFO with no reader raises ENXIO."""
        runtime = tmp_path / "rt"
        runtime.mkdir()
        fifo_path = runtime / "in.whatsapp"
        os.mkfifo(str(fifo_path))

        cfg = WANotifierConfig(
            db_path=tmp_path / "messages.db", runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)

        with pytest.raises(OSError) as exc_info:
            n._write_notification([
                {"chat_id": "x", "sender": "A", "count": 1, "preview": "hi"},
            ])
        assert exc_info.value.errno == errno.ENXIO


# ---------------------------------------------------------------------------
# Gap 4: Notification payload parseable by ccmux parse_message()
# ---------------------------------------------------------------------------


class TestNotificationParseContract:
    def test_payload_parseable_by_ccmux(self, tmp_path: Path) -> None:
        """The JSON written to the FIFO must be correctly parsed by
        ccmux's parse_message() into Message(channel='whatsapp', ...)."""
        runtime = tmp_path / "rt"
        runtime.mkdir()
        fifo_path = runtime / "in.whatsapp"
        os.mkfifo(str(fifo_path))

        cfg = WANotifierConfig(
            db_path=tmp_path / "messages.db", runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)

        read_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            n._write_notification([
                {"chat_id": "123@s.whatsapp.net", "sender": "Alice",
                 "count": 1, "preview": "Hey"},
            ])
            raw = os.read(read_fd, 4096).decode().strip()
        finally:
            os.close(read_fd)

        # Feed the raw line into ccmux's parser, exactly as FifoReader would
        msg = parse_message(raw, "in.whatsapp")
        assert msg.channel == "whatsapp"
        assert "Alice" in msg.content
        assert "list_messages" in msg.content
        assert isinstance(msg.ts, int)


# ---------------------------------------------------------------------------
# Gap 5: Singular/plural formatting
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Gap 6: _init_last_seen() reads max(timestamp) from DB
# ---------------------------------------------------------------------------


class TestInitLastSeen:
    def test_reads_max_timestamp_from_db(self, tmp_path: Path) -> None:
        """_init_last_seen() should set last_seen_ts to max(timestamp) from DB."""
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "a@s.whatsapp.net", "sender": "A",
             "content": "Old", "timestamp": _ts(10)},
            {"chat_jid": "b@s.whatsapp.net", "sender": "B",
             "content": "New", "timestamp": _ts(50)},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        assert n.last_seen_ts == ""  # not yet initialized
        n._init_last_seen()
        assert n.last_seen_ts == _ts(50)

    def test_empty_db_sets_empty_string(self, tmp_path: Path) -> None:
        """When DB has no messages, last_seen_ts should be empty string."""
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n._init_last_seen()
        assert n.last_seen_ts == ""

    def test_nonexistent_db_sets_empty_string(self, tmp_path: Path) -> None:
        """When DB file doesn't exist, last_seen_ts should be empty string."""
        cfg = WANotifierConfig(
            db_path=tmp_path / "missing.db", runtime_dir=tmp_path,
        )
        n = WhatsAppNotifier(cfg)
        n._init_last_seen()
        assert n.last_seen_ts == ""

    def test_skips_if_already_set(self, tmp_path: Path) -> None:
        """If last_seen_ts was pre-set (e.g. by test), _init_last_seen() is a no-op."""
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "a@s.whatsapp.net", "sender": "A",
             "content": "Msg", "timestamp": _ts(99)},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n.last_seen_ts = "preset-value"
        n._init_last_seen()
        assert n.last_seen_ts == "preset-value"  # unchanged

    def test_init_then_query_skips_existing_messages(self, tmp_path: Path) -> None:
        """After _init_last_seen(), querying should return nothing for existing msgs."""
        db_path = tmp_path / "messages.db"
        _create_test_db(db_path)
        _insert_messages(db_path, [
            {"chat_jid": "a@s.whatsapp.net", "sender": "A",
             "content": "History", "timestamp": _ts(10)},
            {"chat_jid": "b@s.whatsapp.net", "sender": "B",
             "content": "Also history", "timestamp": _ts(20)},
        ])
        cfg = WANotifierConfig(db_path=db_path, runtime_dir=tmp_path)
        n = WhatsAppNotifier(cfg)
        n._init_last_seen()

        # All existing messages should be skipped
        assert n._query_new_messages() == []

        # But a NEW message should be found
        _insert_messages(db_path, [
            {"id": "new-1", "chat_jid": "a@s.whatsapp.net", "sender": "A",
             "content": "New!", "timestamp": _ts(30)},
        ])
        result = n._query_new_messages()
        assert len(result) == 1
        assert result[0]["preview"] == "New!"


# ---------------------------------------------------------------------------
# Gap 5: Singular/plural formatting
# ---------------------------------------------------------------------------


class TestSingularPluralFormatting:
    def test_singular_msg(self, tmp_path: Path) -> None:
        runtime = tmp_path / "rt"
        runtime.mkdir()
        fifo_path = runtime / "in.whatsapp"
        os.mkfifo(str(fifo_path))

        cfg = WANotifierConfig(
            db_path=tmp_path / "messages.db", runtime_dir=runtime,
        )
        n = WhatsAppNotifier(cfg)

        read_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            n._write_notification([
                {"chat_id": "x", "sender": "Bob", "count": 1, "preview": "Hi"},
            ])
            data = os.read(read_fd, 4096).decode()
            assert "1 msg)" in data
            assert "1 msgs)" not in data
        finally:
            os.close(read_fd)
