"""Unit tests for pad_agent StateManager."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

from libs.pad_agent.state import StateManager

log = logging.getLogger(__name__)


class TestStateManager:

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        """Data saved is identical when loaded back."""
        sm = StateManager(tmp_path / "state.json")
        adb = {"connected": True}
        screen = {"active_minutes": 42}
        lock = {"locked": False}
        heartbeat = {"seq": 7}

        sm.save(adb, screen, lock, heartbeat)
        loaded = sm.load()

        assert loaded is not None
        assert loaded["adb"] == adb
        assert loaded["screen_time"] == screen
        assert loaded["lock"] == lock
        assert loaded["heartbeat"] == heartbeat

    def test_load_missing_file_returns_none(self, tmp_path: Path) -> None:
        sm = StateManager(tmp_path / "does_not_exist.json")
        assert sm.load() is None

    def test_load_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        p.write_text("{corrupt json!!")
        sm = StateManager(p)
        assert sm.load() is None

    def test_atomic_write_uses_rename(self, tmp_path: Path) -> None:
        """Verify that save() goes through os.rename for atomicity."""
        import os as _os

        sm = StateManager(tmp_path / "state.json")
        with patch("libs.pad_agent.state.os.rename", wraps=_os.rename) as mock_rename:
            sm.save({}, {}, {}, {})
            mock_rename.assert_called_once()

    def test_version_field_present(self, tmp_path: Path) -> None:
        sm = StateManager(tmp_path / "state.json")
        sm.save({}, {}, {}, {})
        loaded = sm.load()
        assert loaded is not None
        assert loaded["version"] == 1

    def test_last_updated_field_present(self, tmp_path: Path) -> None:
        sm = StateManager(tmp_path / "state.json")
        sm.save({}, {}, {}, {})
        loaded = sm.load()
        assert loaded is not None
        assert "last_updated" in loaded
        # Verify it is a valid ISO datetime string
        from datetime import datetime
        datetime.fromisoformat(loaded["last_updated"])
