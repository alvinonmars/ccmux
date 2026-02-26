"""WhatsApp notifier configuration loaded from ccmux.toml [whatsapp] section."""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]


REPLY_PREFIX = "\U0001f916 "  # 🤖 prefix for Claude replies (anti-echo marker)

# Bot message prefixes — used to filter out bot-generated messages and prevent
# echo loops when forwarding admin's "From: Me" messages from all chats.
BOT_PREFIXES = (
    "\U0001f916",   # 🤖 — admin self-chat replies
    "S3 ",          # S3 — contact replies
    "S3\n",         # S3 — contact replies (multiline)
    "\U0001f3e1",   # 🏡 — household group replies
)


@dataclass
class WANotifierConfig:
    db_path: Path
    poll_interval: int = 30
    allowed_chats: list[str] = field(default_factory=list)
    ignore_groups: bool = True
    runtime_dir: Path = Path("/tmp/ccmux")
    admin_jid: str = ""  # self-chat JID for admin channel; auto-detected or env override
    classify_enabled: bool = False  # master switch for intent classification
    smart_classify_chats: list[str] = field(default_factory=list)  # JIDs that use classification


def load(project_root: Path | None = None) -> WANotifierConfig:
    """Load WhatsApp notifier config from ccmux.toml [whatsapp] section."""
    if project_root is None:
        project_root = Path.cwd()

    toml_path = project_root / "ccmux.toml"
    data: dict = {}
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

    wa = data.get("whatsapp", {})
    runtime = data.get("runtime", {})

    db_path_str = wa.get("db_path", "")
    if not db_path_str:
        raise ValueError(
            "ccmux.toml [whatsapp] db_path is required. "
            "Set it to the whatsapp-mcp SQLite database path."
        )

    poll_interval = wa.get("poll_interval", 30)
    if poll_interval < 1:
        raise ValueError(
            "ccmux.toml [whatsapp] poll_interval must be >= 1 second."
        )

    admin_jid = _resolve_admin_jid(Path(db_path_str))

    return WANotifierConfig(
        db_path=Path(db_path_str),
        poll_interval=poll_interval,
        allowed_chats=wa.get("allowed_chats", []),
        ignore_groups=wa.get("ignore_groups", True),
        runtime_dir=Path(runtime.get("dir", "/tmp/ccmux")),
        admin_jid=admin_jid,
        classify_enabled=wa.get("classify_enabled", False),
        smart_classify_chats=wa.get("smart_classify_chats", []),
    )


def _resolve_admin_jid(db_path: Path) -> str:
    """Resolve admin JID: env var > auto-detect from bridge device DB.

    Returns the self-chat JID (e.g. "1234567890@s.whatsapp.net") or ""
    if not available.
    """
    # 1. Env var override
    env_val = os.environ.get("CCMUX_WA_ADMIN_JID", "").strip()
    if env_val:
        # Normalize: add @s.whatsapp.net if bare number
        if "@" not in env_val:
            env_val = f"{env_val}@s.whatsapp.net"
        log.info("Admin JID from env: %s", env_val)
        return env_val

    # 2. Auto-detect from whatsapp.db (sibling of messages.db)
    device_db = db_path.parent / "whatsapp.db"
    if not device_db.exists():
        log.info("No device DB found at %s, admin chat disabled", device_db)
        return ""
    try:
        conn = sqlite3.connect(f"file:{device_db}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT jid FROM whatsmeow_device LIMIT 1"
            ).fetchone()
            if row and row[0]:
                # JID format: "1234567890:3@s.whatsapp.net" -> "1234567890@s.whatsapp.net"
                raw_jid = row[0]
                phone = raw_jid.split(":")[0].split("@")[0]
                admin_jid = f"{phone}@s.whatsapp.net"
                log.info("Admin JID auto-detected: %s", admin_jid)
                return admin_jid
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("Failed to read device DB: %s", exc)
    return ""
