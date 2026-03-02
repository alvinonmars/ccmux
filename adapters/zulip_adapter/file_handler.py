"""File handling for the Zulip adapter.

Pure functions for path validation, filename sanitization, attachment parsing,
file download, and file upload. All file I/O is validated against a project
base path to prevent path traversal and symlink escape.

Uses stdlib only (no third-party dependencies).
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# Matches Zulip attachment links: [name](/user_uploads/...) and ![name](/user_uploads/...)
# Group 1 = display name, Group 2 = server path (e.g. /user_uploads/1/ab/file.pdf)
ATTACHMENT_RE = re.compile(
    r"!?\[([^\]]*)\]\((/user_uploads/[^)]+)\)"
)

# Characters not allowed in filenames (POSIX + Windows safety)
_UNSAFE_FILENAME_RE = re.compile(r'[/\\<>:"|?*\x00]')


def safe_resolve(base: Path, relative: str) -> Path | None:
    """Resolve a relative path under base dir, preventing path traversal.

    Returns the resolved Path if it is strictly within ``base`` (or equals
    ``base`` itself). Returns None if the resolved path escapes ``base``
    — whether via ``../``, absolute paths, or symlink indirection.
    """
    # Reject absolute paths early
    if os.path.isabs(relative):
        return None

    resolved = (base / relative).resolve()
    base_resolved = base.resolve()

    if resolved == base_resolved:
        return resolved
    if str(resolved).startswith(str(base_resolved) + os.sep):
        return resolved
    return None


def sanitize_filename(name: str) -> str:
    """Sanitize a filename for safe local storage.

    Strips path separators, null bytes, and special characters.
    Leading dots are removed to prevent hidden files. Empty result
    falls back to ``"unnamed"``.
    """
    clean = _UNSAFE_FILENAME_RE.sub("", name)
    clean = clean.lstrip(".")
    return clean or "unnamed"


def extract_attachments(content: str) -> list[tuple[str, str]]:
    """Extract file attachments from Zulip message content.

    Returns list of (display_name, server_path) tuples.
    Matches both ``[name](/user_uploads/...)`` and ``![name](/user_uploads/...)``.
    """
    return ATTACHMENT_RE.findall(content)


def strip_attachment_links(content: str) -> str:
    """Remove raw /user_uploads/ links from message text.

    Replaces each attachment markdown link with just the display name
    so the FIFO message stays readable without useless server URLs.
    """
    return ATTACHMENT_RE.sub(r"\1", content).strip()


def download_file(
    opener: urllib.request.OpenerDirector,
    site: str,
    auth_header: str,
    server_path: str,
    dest: Path,
    timeout: int = 30,
) -> bool:
    """Download a file from Zulip to a local path.

    Args:
        opener: urllib opener (with proxy settings as needed).
        site: Zulip server base URL (e.g. "https://chat.example.com").
        auth_header: HTTP Basic auth header value.
        server_path: Path from Zulip (e.g. "/user_uploads/1/ab/file.pdf").
        dest: Local destination path (must be pre-validated via safe_resolve).
        timeout: Download timeout in seconds.

    Returns True on success, False on any error.
    """
    url = f"{site}{server_path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth_header)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with opener.open(req, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
        log.info("Downloaded: %s → %s", server_path, dest)
        return True
    except Exception as e:
        log.warning("Download failed (%s): %s", server_path, e)
        return False


def upload_file(
    opener: urllib.request.OpenerDirector,
    site: str,
    auth_header: str,
    filepath: Path,
    timeout: int = 60,
) -> str | None:
    """Upload a local file to Zulip via /api/v1/user_uploads.

    Uses stdlib multipart/form-data encoding (no requests dependency).

    Returns the server URI on success (e.g. "/user_uploads/1/ab/file.pdf"),
    or None on failure.
    """
    boundary = "----ccmux-upload-boundary"
    filename = filepath.name
    file_data = filepath.read_bytes()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n"
        f"\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    url = f"{site}/api/v1/user_uploads"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", auth_header)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    try:
        import json

        with opener.open(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
        if result.get("result") == "success":
            uri = result.get("uri", "")
            log.info("Uploaded: %s → %s", filepath, uri)
            return uri
        log.warning("Upload API error: %s", result.get("msg", "unknown"))
        return None
    except Exception as e:
        log.warning("Upload failed (%s): %s", filepath, e)
        return None
