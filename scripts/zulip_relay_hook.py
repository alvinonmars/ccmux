#!/usr/bin/env python3
"""Stop hook for Zulip instances.

Reads Claude's latest output from stdin JSON, posts to Zulip stream+topic.
Stream and topic are env vars set at tmux creation time — no file-based routing.
Stdlib only — no venv dependencies.

Environment: ZULIP_STREAM, ZULIP_TOPIC, ZULIP_SITE, ZULIP_BOT_EMAIL, ZULIP_BOT_API_KEY_FILE
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request


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
    if not key_file or not os.path.exists(key_file):
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
