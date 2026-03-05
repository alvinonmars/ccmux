"""Unit tests for zulip_relay_hook.py — outbound message formatting."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# The relay hook is a script, import its internals
from scripts.zulip_relay_hook import _process_send_file_markers, _safe_resolve


class TestSafeResolve:
    """Path resolution safety."""

    def test_resolves_relative_path(self, tmp_path: Path) -> None:
        result = _safe_resolve(tmp_path, "file.txt")
        assert result == (tmp_path / "file.txt").resolve()

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        assert _safe_resolve(tmp_path, "/etc/passwd") is None

    def test_rejects_traversal(self, tmp_path: Path) -> None:
        assert _safe_resolve(tmp_path, "../../../etc/passwd") is None


class TestProcessSendFileMarkers:
    """Tests for [send-file:] marker processing."""

    def test_strips_markers_without_project_path(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = _process_send_file_markers(
                "Hello [send-file: test.png] world",
                "https://zulip.example.com",
                "cred",
            )
            assert result == "Hello  world"

    def test_image_file_uses_inline_markdown(self, tmp_path: Path) -> None:
        """Image files should use ![name](uri) for inline display."""
        img = tmp_path / "screenshot.png"
        img.write_bytes(b"fake png data")

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(tmp_path)}), \
             patch("scripts.zulip_relay_hook._upload_file", return_value="/user_uploads/1/ab/screenshot.png"):
            result = _process_send_file_markers(
                "[send-file: screenshot.png]",
                "https://zulip.example.com",
                "cred",
            )
            assert result == "![screenshot.png](/user_uploads/1/ab/screenshot.png)"

    def test_non_image_file_uses_link_markdown(self, tmp_path: Path) -> None:
        """Non-image files should use [name](uri) as a download link."""
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"fake pdf data")

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(tmp_path)}), \
             patch("scripts.zulip_relay_hook._upload_file", return_value="/user_uploads/1/ab/report.pdf"):
            result = _process_send_file_markers(
                "[send-file: report.pdf]",
                "https://zulip.example.com",
                "cred",
            )
            assert result == "[report.pdf](/user_uploads/1/ab/report.pdf)"

    @pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"])
    def test_all_image_extensions_inline(self, tmp_path: Path, ext: str) -> None:
        """All recognized image extensions produce inline markdown."""
        f = tmp_path / f"image{ext}"
        f.write_bytes(b"data")
        uri = f"/user_uploads/1/ab/image{ext}"

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(tmp_path)}), \
             patch("scripts.zulip_relay_hook._upload_file", return_value=uri):
            result = _process_send_file_markers(
                f"[send-file: image{ext}]",
                "https://zulip.example.com",
                "cred",
            )
            assert result.startswith("!")

    def test_upload_failure_returns_empty(self, tmp_path: Path) -> None:
        """Failed upload produces empty string, not a broken link."""
        f = tmp_path / "fail.png"
        f.write_bytes(b"data")

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(tmp_path)}), \
             patch("scripts.zulip_relay_hook._upload_file", return_value=None):
            result = _process_send_file_markers(
                "[send-file: fail.png]",
                "https://zulip.example.com",
                "cred",
            )
            assert result == ""

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing file produces empty string."""
        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(tmp_path)}):
            result = _process_send_file_markers(
                "[send-file: missing.txt]",
                "https://zulip.example.com",
                "cred",
            )
            assert result == ""

    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        """Path traversal attempts are rejected."""
        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(tmp_path)}):
            result = _process_send_file_markers(
                "[send-file: ../../../etc/passwd]",
                "https://zulip.example.com",
                "cred",
            )
            assert result == ""

    def test_mixed_content_with_file_marker(self, tmp_path: Path) -> None:
        """Text around file markers is preserved."""
        img = tmp_path / "chart.jpg"
        img.write_bytes(b"data")

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(tmp_path)}), \
             patch("scripts.zulip_relay_hook._upload_file", return_value="/uploads/chart.jpg"):
            result = _process_send_file_markers(
                "Here is the chart: [send-file: chart.jpg] — see above",
                "https://zulip.example.com",
                "cred",
            )
            assert "![chart.jpg]" in result
            assert "Here is the chart:" in result
            assert "see above" in result
