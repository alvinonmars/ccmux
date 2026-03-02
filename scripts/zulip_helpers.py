#!/usr/bin/env python3
"""Zulip API helper for the main assistant.

CLI tool to manage Zulip streams. Stdlib only (urllib).
Reads bot credentials from ~/.ccmux/secrets/zulip_bot.env.

Usage:
    zulip_helpers.py create-stream <stream_name>
    zulip_helpers.py delete-stream <stream_name>
    zulip_helpers.py list-streams
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_CREDENTIALS_FILE = os.path.expanduser("~/.ccmux/secrets/zulip_bot.env")


def _load_credentials(cred_file: str | None = None) -> tuple[str, str, str]:
    """Load site, email, api_key from credentials file.

    Returns (site, email, api_key).
    """
    cred_file = cred_file or DEFAULT_CREDENTIALS_FILE
    site = ""
    email = ""
    api_key = ""

    def _strip_quotes(v: str) -> str:
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        return v

    with open(cred_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("ZULIP_SITE="):
                site = _strip_quotes(line.split("=", 1)[1])
            elif line.startswith("ZULIP_BOT_EMAIL="):
                email = _strip_quotes(line.split("=", 1)[1])
            elif line.startswith("ZULIP_BOT_API_KEY="):
                api_key = _strip_quotes(line.split("=", 1)[1])

    if not all([site, email, api_key]):
        # Fall back to env vars
        site = site or os.environ.get("ZULIP_SITE", "")
        email = email or os.environ.get("ZULIP_BOT_EMAIL", "")
        api_key = api_key or os.environ.get("ZULIP_BOT_API_KEY", "")

    if not all([site, email, api_key]):
        print("Error: missing Zulip credentials", file=sys.stderr)
        sys.exit(1)

    return site, email, api_key


def _api_call(
    site: str,
    email: str,
    api_key: str,
    method: str,
    endpoint: str,
    data: dict | None = None,
) -> dict:
    """Make authenticated Zulip API call. Returns parsed JSON response."""
    cred = base64.b64encode(f"{email}:{api_key}".encode()).decode()
    url = f"{site}/api/v1{endpoint}"

    if data is not None:
        encoded = urllib.parse.urlencode(data, doseq=True).encode()
    else:
        encoded = None

    req = urllib.request.Request(url, data=encoded, method=method)
    req.add_header("Authorization", f"Basic {cred}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"result": "error", "msg": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"result": "error", "msg": str(e)}


def create_stream(name: str, cred_file: str | None = None) -> int:
    """Create a Zulip stream and subscribe the bot."""
    site, email, api_key = _load_credentials(cred_file)

    result = _api_call(
        site,
        email,
        api_key,
        "POST",
        "/users/me/subscriptions",
        {
            "subscriptions": json.dumps([{"name": name}]),
        },
    )

    if result.get("result") == "success":
        already = result.get("already_subscribed", {})
        if already:
            print(f"Stream #{name} already exists (bot already subscribed)")
        else:
            print(f"Stream #{name} created, bot subscribed")
        return 0

    print(f"Error: {result.get('msg', 'unknown error')}", file=sys.stderr)
    return 1


def delete_stream(name: str, cred_file: str | None = None) -> int:
    """Archive/deactivate a Zulip stream."""
    site, email, api_key = _load_credentials(cred_file)

    # First, find stream ID
    result = _api_call(site, email, api_key, "GET", "/streams")
    if result.get("result") != "success":
        print(f"Error listing streams: {result.get('msg')}", file=sys.stderr)
        return 1

    stream_id = None
    for s in result.get("streams", []):
        if s.get("name") == name:
            stream_id = s.get("stream_id")
            break

    if stream_id is None:
        print(f"Stream #{name} not found", file=sys.stderr)
        return 1

    result = _api_call(
        site, email, api_key, "DELETE", f"/streams/{stream_id}"
    )
    if result.get("result") == "success":
        print(f"Stream #{name} archived")
        return 0

    print(f"Error: {result.get('msg', 'unknown error')}", file=sys.stderr)
    return 1


def list_streams(cred_file: str | None = None) -> int:
    """List bot-subscribed streams."""
    site, email, api_key = _load_credentials(cred_file)

    result = _api_call(
        site, email, api_key, "GET", "/users/me/subscriptions"
    )
    if result.get("result") != "success":
        print(f"Error: {result.get('msg')}", file=sys.stderr)
        return 1

    subs = result.get("subscriptions", [])
    if not subs:
        print("No subscribed streams")
        return 0

    for s in sorted(subs, key=lambda x: x.get("name", "")):
        print(f"  #{s['name']} (id={s.get('stream_id', '?')})")

    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) < 1:
        print("Usage: zulip_helpers.py <command> [args]", file=sys.stderr)
        print("Commands: create-stream, delete-stream, list-streams", file=sys.stderr)
        return 1

    command = argv[0]

    if command == "create-stream":
        if len(argv) < 2:
            print("Usage: zulip_helpers.py create-stream <name>", file=sys.stderr)
            return 1
        return create_stream(argv[1])

    elif command == "delete-stream":
        if len(argv) < 2:
            print("Usage: zulip_helpers.py delete-stream <name>", file=sys.stderr)
            return 1
        return delete_stream(argv[1])

    elif command == "list-streams":
        return list_streams()

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
