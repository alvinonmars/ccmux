#!/usr/bin/env python3
"""Stop hook for Zulip instances.

Reads Claude's latest output from stdin JSON, posts to Zulip stream+topic.
Detects [send-file: path] markers and uploads files before posting.
Stream and topic are env vars set at tmux creation time — no file-based routing.
Stdlib only — no venv dependencies.

Environment: ZULIP_STREAM, ZULIP_TOPIC, ZULIP_SITE, ZULIP_BOT_EMAIL,
             ZULIP_BOT_API_KEY_FILE, ZULIP_PROJECT_PATH
"""
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Matches [send-file: path/to/file] markers in Claude output
SEND_FILE_RE = re.compile(r"\[send-file:\s*([^\]]+)\]")


def _safe_resolve(base: Path, relative: str) -> Path | None:
    """Resolve path under base, preventing traversal. Stdlib only."""
    if os.path.isabs(relative):
        return None
    resolved = (base / relative).resolve()
    base_resolved = base.resolve()
    if resolved == base_resolved:
        return resolved
    if str(resolved).startswith(str(base_resolved) + os.sep):
        return resolved
    return None


def _upload_file(site: str, cred: str, filepath: Path) -> str | None:
    """Upload file to Zulip. Returns URI on success, None on failure."""
    boundary = "----ccmux-relay-upload"
    filename = filepath.name
    try:
        file_data = filepath.read_bytes()
    except OSError as e:
        print(f"zulip_relay_hook: read failed: {e}", file=sys.stderr)
        return None

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n"
        f"\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{site}/api/v1/user_uploads", data=body, method="POST"
    )
    req.add_header("Authorization", f"Basic {cred}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
        if result.get("result") == "success":
            return result.get("uri", "")
        print(f"zulip_relay_hook: upload error: {result.get('msg')}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"zulip_relay_hook: upload failed: {e}", file=sys.stderr)
        return None


def _process_send_file_markers(content: str, site: str, cred: str) -> str:
    """Replace [send-file: path] markers with uploaded file links."""
    project_path_str = os.environ.get("ZULIP_PROJECT_PATH", "")
    if not project_path_str:
        # No project path — strip markers but don't upload
        return SEND_FILE_RE.sub("", content).strip()

    project_path = Path(project_path_str)

    def _replace(match: re.Match) -> str:
        filepath = match.group(1).strip()
        resolved = _safe_resolve(project_path, filepath)
        if resolved is None:
            print(
                f"zulip_relay_hook: path rejected (outside project): {filepath}",
                file=sys.stderr,
            )
            return ""
        if not resolved.is_file():
            print(
                f"zulip_relay_hook: file not found: {resolved}",
                file=sys.stderr,
            )
            return ""
        uri = _upload_file(site, cred, resolved)
        if uri:
            return f"[{resolved.name}]({uri})"
        return ""

    return SEND_FILE_RE.sub(_replace, content).strip()


def main() -> None:
    stream = os.environ.get("ZULIP_STREAM")
    topic = os.environ.get("ZULIP_TOPIC", "chat")
    if not stream:
        return  # Not a Zulip instance — skip

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    content = data.get("last_assistant_message", "")
    if not content:
        return

    key_file = os.path.expanduser(
        os.environ.get("ZULIP_BOT_API_KEY_FILE", "")
    )
    if not key_file:
        return
    if not os.path.exists(key_file):
        print(
            f"zulip_relay_hook: ZULIP_BOT_API_KEY_FILE not found: {key_file}",
            file=sys.stderr,
        )
        return
    api_key = ""
    with open(key_file) as f:
        for line in f:
            if line.startswith("ZULIP_BOT_API_KEY="):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                api_key = value
    if not api_key:
        return

    site = os.environ.get("ZULIP_SITE", "")
    email = os.environ.get("ZULIP_BOT_EMAIL", "")
    if not site or not email:
        return

    cred = base64.b64encode(f"{email}:{api_key}".encode()).decode()

    # Process [send-file:] markers before posting
    if SEND_FILE_RE.search(content):
        content = _process_send_file_markers(content, site, cred)

    if not content:
        return

    # Split long messages (Zulip 10000 char limit)
    chunks = [content[i : i + 9500] for i in range(0, len(content), 9500)]

    for chunk in chunks:
        post_data = urllib.parse.urlencode(
            {
                "type": "stream",
                "to": stream,
                "topic": topic,
                "content": chunk,
            }
        ).encode()
        req = urllib.request.Request(
            f"{site}/api/v1/messages", data=post_data, method="POST"
        )
        req.add_header("Authorization", f"Basic {cred}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"zulip_relay_hook: post failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
