"""JSONL message history store for WeChat Work messages.

Storage: ~/.ccmux/data/wework/messages.jsonl
Record schema:
    {
        "msg_id": str,
        "msg_type": str,        # "text", "image", "voice", "video", "file"
        "from_user": str,       # sender user ID
        "to_user": str,         # recipient user ID
        "content": str,         # text content or media placeholder
        "create_time": str,     # ISO timestamp
        "direction": str,       # "inbound" or "outbound"
        "agent_id": str,
    }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from ccmux.paths import WEWORK_MESSAGES, WEWORK_DIR

log = logging.getLogger(__name__)


def append_message(msg: dict) -> None:
    """Append a single message record to the JSONL history file."""
    WEWORK_DIR.mkdir(parents=True, exist_ok=True)
    with open(WEWORK_MESSAGES, "a") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def list_messages(
    *,
    after: str | None = None,
    before: str | None = None,
    from_user: str | None = None,
    limit: int = 50,
    query: str | None = None,
) -> list[dict]:
    """Read and filter messages from JSONL history.

    Args:
        after: ISO timestamp — only return messages after this time.
        before: ISO timestamp — only return messages before this time.
        from_user: Filter by sender user ID.
        limit: Max number of messages to return (most recent first).
        query: Substring search in message content.

    Returns:
        List of message dicts, most recent first, up to `limit`.
    """
    if not WEWORK_MESSAGES.exists():
        return []

    messages: list[dict] = []
    for line in WEWORK_MESSAGES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        ct = msg.get("create_time", "")
        if after and ct < after:
            continue
        if before and ct > before:
            continue
        if from_user and msg.get("from_user") != from_user:
            continue
        if query and query.lower() not in msg.get("content", "").lower():
            continue

        messages.append(msg)

    # Most recent first
    messages.sort(key=lambda m: m.get("create_time", ""), reverse=True)
    return messages[:limit]
