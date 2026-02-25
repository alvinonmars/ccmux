"""Central data path configuration for ccmux and all scripts.

All privacy-sensitive data lives outside the repo under ~/.ccmux/data/.
Override via CCMUX_DATA_DIR environment variable if needed.

Usage from any script:

    import sys
    from pathlib import Path
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(PROJECT_ROOT))

    from ccmux.paths import DATA_ROOT, HOUSEHOLD_DIR, FAMILY_CONTEXT
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Root paths --------------------------------------------------------------

DATA_ROOT = Path(
    os.environ.get("CCMUX_DATA_DIR", str(Path.home() / ".ccmux" / "data"))
)

SECRETS_ROOT = Path(
    os.environ.get("CCMUX_SECRETS_DIR", str(Path.home() / ".ccmux" / "secrets"))
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Data sub-paths ----------------------------------------------------------

HOUSEHOLD_DIR = DATA_ROOT / "household"
DAILY_REFLECTIONS_DIR = DATA_ROOT / "daily_reflections"
CONTACTS_FILE = DATA_ROOT / "contacts.json"
FAMILY_CONTEXT = HOUSEHOLD_DIR / "family_context.jsonl"
CHAT_HISTORY = HOUSEHOLD_DIR / "chat_history.jsonl"

# Household sub-directories
BUTLER_DIR = HOUSEHOLD_DIR / "butler"
ANNOUNCEMENTS_DIR = BUTLER_DIR / "announcements"
HEALTH_DIR = HOUSEHOLD_DIR / "health"
RECEIPTS_DIR = HOUSEHOLD_DIR / "receipts"
HOMEWORK_DIR = HOUSEHOLD_DIR / "homework"

# Temporary / working files (screenshots, scan results)
TMP_DIR = HOUSEHOLD_DIR / "tmp"
EMAIL_SCAN_DIR = TMP_DIR / "email_scan"
GMAIL_SCAN_DIR = TMP_DIR / "gmail_scan"

# Security audit
SECURITY_AUDIT_DIR = DATA_ROOT / "security_audit"

# --- Secrets sub-paths -------------------------------------------------------

POWERSCHOOL_ENV = SECRETS_ROOT / "powerschool.env"
GMAIL_ENV = SECRETS_ROOT / "gmail.env"


def ensure_dirs() -> None:
    """Create all standard data directories if they don't exist."""
    for d in (
        DATA_ROOT,
        SECRETS_ROOT,
        HOUSEHOLD_DIR,
        DAILY_REFLECTIONS_DIR,
        BUTLER_DIR,
        ANNOUNCEMENTS_DIR,
        HEALTH_DIR,
        RECEIPTS_DIR,
        HOMEWORK_DIR,
        TMP_DIR,
        EMAIL_SCAN_DIR,
        GMAIL_SCAN_DIR,
        SECURITY_AUDIT_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
