"""Unit tests for privacy_check.py — Layer 1 regex scanning."""

from __future__ import annotations

import re

import pytest

from scripts.privacy_check import (
    scan_content,
    scan_filename,
    scan_files,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BLOCKLIST_PATTERN_JOY = (r"\bjoy\b", "BLOCKLIST", re.IGNORECASE)
BLOCKLIST_PATTERN_ALICE = (r"\balice\b", "BLOCKLIST", re.IGNORECASE)
PHONE_PATTERN = (r"852[0-9]{8}", "PHONE_NUMBER", re.IGNORECASE)
JID_PATTERN = (r"852\d{8}@s\.whatsapp\.net", "WHATSAPP_JID", 0)


# ---------------------------------------------------------------------------
# scan_filename
# ---------------------------------------------------------------------------


class TestScanFilename:
    """Verify that filename scanning catches PII in path components."""

    def test_underscore_delimited(self):
        findings = scan_filename(
            "scripts/joy_daily_checkin.py",
            [BLOCKLIST_PATTERN_JOY],
            set(),
        )
        assert len(findings) == 1
        assert findings[0]["line"] == 0
        assert findings[0]["match"].lower() == "joy"

    def test_hyphen_delimited(self):
        findings = scan_filename(
            "scripts/joy-health.py",
            [BLOCKLIST_PATTERN_JOY],
            set(),
        )
        assert len(findings) == 1

    def test_directory_component(self):
        findings = scan_filename(
            "data/joy/logs.txt",
            [BLOCKLIST_PATTERN_JOY],
            set(),
        )
        assert len(findings) == 1

    def test_dot_delimited(self):
        findings = scan_filename(
            "joy.config.json",
            [BLOCKLIST_PATTERN_JOY],
            set(),
        )
        assert len(findings) == 1

    def test_allowlist_skips(self):
        findings = scan_filename(
            "scripts/joy_daily_checkin.py",
            [BLOCKLIST_PATTERN_JOY],
            {"joy"},
        )
        assert len(findings) == 0

    def test_clean_filename_passes(self):
        findings = scan_filename(
            "scripts/daily_butler.py",
            [BLOCKLIST_PATTERN_JOY],
            set(),
        )
        assert len(findings) == 0

    def test_partial_word_not_matched(self):
        """'joyful' should not match \\bjoy\\b."""
        findings = scan_filename(
            "scripts/joyful_app.py",
            [BLOCKLIST_PATTERN_JOY],
            set(),
        )
        assert len(findings) == 0

    def test_phone_in_filename(self):
        findings = scan_filename(
            "data/85200001234_logs.txt",
            [PHONE_PATTERN],
            set(),
        )
        assert len(findings) == 1
        assert findings[0]["category"] == "PHONE_NUMBER"

    def test_multiple_patterns(self):
        findings = scan_filename(
            "data/joy_alice_config.py",
            [BLOCKLIST_PATTERN_JOY, BLOCKLIST_PATTERN_ALICE],
            set(),
        )
        assert len(findings) == 2

    def test_context_field_contains_filename(self):
        findings = scan_filename(
            "scripts/joy_daily.py",
            [BLOCKLIST_PATTERN_JOY],
            set(),
        )
        assert "(filename:" in findings[0]["context"]


# ---------------------------------------------------------------------------
# scan_content (existing functionality — regression tests)
# ---------------------------------------------------------------------------


class TestScanContent:
    """Verify content scanning basics still work."""

    def test_phone_in_content(self):
        findings = scan_content(
            "test.py",
            "phone = '85200001234'",
            [PHONE_PATTERN],
            set(),
        )
        assert len(findings) == 1
        assert findings[0]["category"] == "PHONE_NUMBER"

    def test_allowlist_skips_line(self):
        findings = scan_content(
            "test.py",
            "phone = '85200001234'",
            [PHONE_PATTERN],
            {"85200001234"},
        )
        assert len(findings) == 0

    def test_jid_pattern(self):
        findings = scan_content(
            "test.py",
            "jid = '85200001234@s.whatsapp.net'",
            [JID_PATTERN],
            set(),
        )
        assert len(findings) == 1
        assert findings[0]["category"] == "WHATSAPP_JID"

    def test_clean_content(self):
        findings = scan_content(
            "test.py",
            "def hello():\n    return 'world'",
            [PHONE_PATTERN, BLOCKLIST_PATTERN_JOY],
            set(),
        )
        assert len(findings) == 0
