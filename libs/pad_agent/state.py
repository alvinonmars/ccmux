"""State persistence (atomic tmp+rename)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class StateManager:
    """Persist monitor state as JSON with atomic writes."""

    def __init__(self, state_path: Path) -> None:
        self._path = state_path

    def save(
        self,
        adb_state: dict,
        screen_time_state: dict,
        lock_state: dict,
        heartbeat_state: dict,
    ) -> None:
        """Atomically write state via tmp file + ``os.rename``."""
        data = {
            "version": 1,
            "last_updated": datetime.now(timezone.utc).astimezone().isoformat(),
            "adb": adb_state,
            "screen_time": screen_time_state,
            "lock": lock_state,
            "heartbeat": heartbeat_state,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Write to a tmp file in the *same* directory so os.rename is atomic
        # on the same filesystem.
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
            os.rename(tmp, str(self._path))
            log.debug("State saved to %s", self._path)
        except BaseException:
            # Clean up temp file on failure.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self) -> dict | None:
        """Load state, tolerant of missing or corrupt files."""
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            log.debug("Could not load state from %s: %s", self._path, exc)
            return None
