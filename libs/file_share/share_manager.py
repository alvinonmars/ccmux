"""Share registry CRUD and token validation for file share service."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ShareManager:
    """Manage file share registry (create, get, delete, validate)."""

    def __init__(self, registry_path: Path) -> None:
        self._path = Path(registry_path)
        self._ensure_registry()

    def _ensure_registry(self) -> None:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._save({"shares": []})

    def _load(self) -> dict[str, Any]:
        return json.loads(self._path.read_text())

    def _save(self, data: dict[str, Any]) -> None:
        self._path.write_text(json.dumps(data, indent=2, default=str) + "\n")

    def list_shares(self) -> list[dict[str, Any]]:
        """Return all active (non-expired) shares."""
        now = datetime.now(timezone.utc)
        return [
            s for s in self._load()["shares"]
            if datetime.fromisoformat(s["expires_at"]) > now
        ]

    def get_share(self, share_id: str) -> dict[str, Any] | None:
        """Look up share by ID, return None if not found or expired."""
        now = datetime.now(timezone.utc)
        for s in self._load()["shares"]:
            if s["id"] == share_id:
                if datetime.fromisoformat(s["expires_at"]) > now:
                    return s
                return None
        return None

    def create_share(
        self,
        share_id: str,
        path: str,
        mode: str = "readonly",
        expires_days: int = 30,
        password: str | None = None,
        label: str = "",
        allowed_extensions: list[str] | None = None,
        max_upload_size_mb: int | None = None,
    ) -> dict[str, Any]:
        """Create a new share, generate token, return share dict."""
        data = self._load()

        # Reject duplicate IDs
        for s in data["shares"]:
            if s["id"] == share_id:
                raise ValueError(f"Share '{share_id}' already exists")

        # Validate mode
        if mode not in ("readonly", "writable"):
            raise ValueError(f"Invalid mode: {mode}")

        # Validate path exists for readonly shares
        share_path = Path(path)
        if mode == "readonly" and not share_path.is_dir():
            raise ValueError(f"Directory does not exist: {path}")

        # For writable shares, create directory if needed
        if mode == "writable":
            share_path.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        token = secrets.token_urlsafe(32)

        share = {
            "id": share_id,
            "path": str(share_path.resolve()),
            "mode": mode,
            "token": token,
            "password": password,
            "created_at": now.isoformat(),
            "expires_at": (
                now + __import__("datetime").timedelta(days=expires_days)
            ).isoformat(),
            "label": label,
            "thumbnail_dir": None,
            "allowed_extensions": allowed_extensions,
            "max_upload_size_mb": max_upload_size_mb,
        }

        data["shares"].append(share)
        self._save(data)
        return share

    def delete_share(self, share_id: str) -> bool:
        """Remove share from registry. Return True if found and removed."""
        data = self._load()
        original_len = len(data["shares"])
        data["shares"] = [s for s in data["shares"] if s["id"] != share_id]
        if len(data["shares"]) < original_len:
            self._save(data)
            return True
        return False

    def validate_token(self, share_id: str, token: str) -> dict[str, Any] | None:
        """Validate token for share. Return share dict if valid, None if not."""
        share = self.get_share(share_id)
        if share is None:
            return None
        if not secrets.compare_digest(share["token"], token):
            return None
        return share

    def cleanup_expired(self) -> int:
        """Remove expired shares from registry. Return count removed."""
        data = self._load()
        now = datetime.now(timezone.utc)
        original_len = len(data["shares"])
        data["shares"] = [
            s for s in data["shares"]
            if datetime.fromisoformat(s["expires_at"]) > now
        ]
        removed = original_len - len(data["shares"])
        if removed > 0:
            self._save(data)
        return removed

    def update_share(self, share_id: str, **updates: Any) -> dict[str, Any] | None:
        """Update fields on an existing share. Return updated share or None."""
        data = self._load()
        for s in data["shares"]:
            if s["id"] == share_id:
                for k, v in updates.items():
                    if k in s:
                        s[k] = v
                self._save(data)
                return s
        return None
