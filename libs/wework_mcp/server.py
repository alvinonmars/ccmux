"""FastMCP server for WeChat Work outbound messaging.

Provides tools for Claude Code to send messages and query history.
Transport: stdio (Claude Code forks this process per session).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from libs.wework_mcp.api import WeWorkAPI
from libs.wework_mcp.history import append_message, list_messages

log = logging.getLogger(__name__)

mcp = FastMCP("wework")

# Lazy-init API client (loaded on first tool call)
_api: WeWorkAPI | None = None
_agent_id: str = ""


def _get_api() -> WeWorkAPI:
    """Get or create the WeWork API client (loads secrets on first call)."""
    global _api, _agent_id
    if _api is not None:
        return _api

    from adapters.wework_notifier.config import load

    cfg = load()
    _api = WeWorkAPI(cfg.corp_id, cfg.secret, cfg.agent_id)
    _agent_id = cfg.agent_id
    return _api


@mcp.tool()
def wework_send_message(user_id: str, content: str) -> str:
    """Send a text message to a WeChat Work user.

    Args:
        user_id: WeWork user ID (e.g. "user123").
        content: Message text to send.

    Returns:
        Status message.
    """
    api = _get_api()
    try:
        api.send_text(user_id, content)
    except Exception as exc:
        return f"Failed to send: {exc}"

    # Log to history
    append_message({
        "msg_id": "",
        "msg_type": "text",
        "from_user": "bot",
        "to_user": user_id,
        "content": content,
        "create_time": datetime.now(timezone.utc).isoformat(),
        "direction": "outbound",
        "agent_id": _agent_id,
    })

    return f"Message sent to {user_id}"


@mcp.tool()
def wework_send_file(user_id: str, file_path: str) -> str:
    """Send a file to a WeChat Work user.

    Args:
        user_id: WeWork user ID.
        file_path: Absolute path to the file to send.

    Returns:
        Status message.
    """
    api = _get_api()
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    try:
        media_id = api.upload_media("file", file_path)
        api.send_file(user_id, media_id)
    except Exception as exc:
        return f"Failed to send file: {exc}"

    append_message({
        "msg_id": "",
        "msg_type": "file",
        "from_user": "bot",
        "to_user": user_id,
        "content": f"[file:{path.name}]",
        "create_time": datetime.now(timezone.utc).isoformat(),
        "direction": "outbound",
        "agent_id": _agent_id,
    })

    return f"File {path.name} sent to {user_id}"


@mcp.tool()
def wework_list_messages(
    after: str = "",
    before: str = "",
    from_user: str = "",
    limit: int = 50,
    query: str = "",
) -> list[dict]:
    """List WeChat Work message history.

    Args:
        after: ISO timestamp — only messages after this time.
        before: ISO timestamp — only messages before this time.
        from_user: Filter by sender user ID.
        limit: Max messages to return (default 50).
        query: Substring search in message content.

    Returns:
        List of message records, most recent first.
    """
    return list_messages(
        after=after or None,
        before=before or None,
        from_user=from_user or None,
        limit=limit,
        query=query or None,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
