"""Transcript JSONL watcher for Zulip intermediate output.

Monitors a Claude Code transcript file for new assistant entries and posts
status updates to Zulip. Gives users real-time visibility into what Claude
is doing during a turn (before the Stop hook fires the final response).

Architecture:
    - Runs as an asyncio task per active session
    - Polls the transcript file for new lines (os.stat size + seek)
    - Parses assistant entries → posts status summaries to Zulip
    - Accumulates all status lines into a single Zulip message (updated in place)
    - Stops when signaled (Stop hook or session end)

Integration point: ProcessManager starts/stops a TranscriptWatcher
per instance after lazy_create.

Stdlib + urllib only (matches zulip_relay_hook.py pattern).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# How often to check the transcript file for new lines (seconds)
POLL_INTERVAL = 2.0

# Maximum length of tool input to show in status updates
MAX_INPUT_DISPLAY = 120

# Tool name → emoji mapping for Zulip status messages
TOOL_EMOJI = {
    "Bash": "\u2699\ufe0f",       # ⚙️
    "Read": "\U0001f4c2",          # 📂
    "Glob": "\U0001f50d",          # 🔍
    "Grep": "\U0001f50d",          # 🔍
    "Edit": "\u270f\ufe0f",       # ✏️
    "Write": "\U0001f4dd",         # 📝
    "Agent": "\U0001f916",         # 🤖
    "WebSearch": "\U0001f310",     # 🌐
    "WebFetch": "\U0001f310",      # 🌐
}
DEFAULT_EMOJI = "\U0001f527"       # 🔧


def _format_tool_status(tool_name: str, tool_input: dict) -> str:
    """Format a tool_use entry into a brief human-readable status line."""
    emoji = TOOL_EMOJI.get(tool_name, DEFAULT_EMOJI)

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        display = desc if desc else cmd
        if len(display) > MAX_INPUT_DISPLAY:
            display = display[:MAX_INPUT_DISPLAY] + "..."
        return f"{emoji} Running: `{display}`"

    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        # Show only filename, not full path
        short = Path(path).name if path else "?"
        return f"{emoji} Reading: `{short}`"

    if tool_name in ("Glob", "Grep"):
        pattern = tool_input.get("pattern", "")
        return f"{emoji} Searching: `{pattern}`"

    if tool_name == "Edit":
        path = tool_input.get("file_path", "")
        short = Path(path).name if path else "?"
        return f"{emoji} Editing: `{short}`"

    if tool_name == "Write":
        path = tool_input.get("file_path", "")
        short = Path(path).name if path else "?"
        return f"{emoji} Writing: `{short}`"

    if tool_name == "Agent":
        desc = tool_input.get("description", tool_input.get("prompt", ""))
        if len(desc) > MAX_INPUT_DISPLAY:
            desc = desc[:MAX_INPUT_DISPLAY] + "..."
        return f"{emoji} Agent: {desc}"

    if tool_name in ("WebSearch", "WebFetch"):
        query = tool_input.get("query", tool_input.get("url", ""))
        if len(query) > MAX_INPUT_DISPLAY:
            query = query[:MAX_INPUT_DISPLAY] + "..."
        return f"{emoji} Web: `{query}`"

    # Generic fallback
    return f"{emoji} {tool_name}"


def _extract_tool_uses(line: str) -> list[tuple[str, dict]]:
    """Extract (tool_name, tool_input) pairs from a transcript JSONL line.

    Returns empty list if the line is not an assistant tool_use message.
    """
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []

    msg = record.get("message", {})
    if msg.get("role") != "assistant":
        return []

    content = msg.get("content", [])
    if not isinstance(content, list):
        return []

    results = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name", "")
            inp = block.get("input", {})
            if name:
                results.append((name, inp))
    return results


def _is_assistant_text(line: str) -> bool:
    """Return True if the line is an assistant message with text content (no tools)."""
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False

    msg = record.get("message", {})
    if msg.get("role") != "assistant":
        return False

    content = msg.get("content", [])
    if not isinstance(content, list):
        return False

    has_text = False
    has_tool = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text", "").strip():
            has_text = True
        if block.get("type") == "tool_use":
            has_tool = True

    return has_text and not has_tool


class ZulipPoster:
    """Lightweight Zulip API client for posting status messages.

    Reads credentials from environment variables (same as zulip_relay_hook.py).
    All methods are synchronous (urllib); callers in async contexts should use
    run_in_executor().
    """

    def __init__(
        self,
        site: str,
        email: str,
        api_key: str,
        stream: str,
        topic: str,
    ):
        self.site = site
        self.stream = stream
        self.topic = topic
        self._cred = base64.b64encode(f"{email}:{api_key}".encode()).decode()
        # Bypass system proxy (Zulip is local)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    @classmethod
    def from_env(cls) -> ZulipPoster | None:
        """Create from ZULIP_* environment variables. Returns None if missing."""
        site = os.environ.get("ZULIP_SITE", "")
        email = os.environ.get("ZULIP_BOT_EMAIL", "")
        stream = os.environ.get("ZULIP_STREAM", "")
        topic = os.environ.get("ZULIP_TOPIC", "")

        key_file = os.path.expanduser(
            os.environ.get("ZULIP_BOT_API_KEY_FILE", "")
        )
        if not all([site, email, stream, topic, key_file]):
            return None
        if not os.path.exists(key_file):
            return None

        api_key = ""
        with open(key_file) as f:
            for line in f:
                if line.startswith("ZULIP_BOT_API_KEY="):
                    value = line.split("=", 1)[1].strip()
                    if (
                        len(value) >= 2
                        and value[0] == value[-1]
                        and value[0] in ('"', "'")
                    ):
                        value = value[1:-1]
                    api_key = value
        if not api_key:
            return None

        return cls(site, email, api_key, stream, topic)

    def post(self, content: str) -> int | None:
        """Post a message. Returns message_id on success, None on failure."""
        data = urllib.parse.urlencode({
            "type": "stream",
            "to": self.stream,
            "topic": self.topic,
            "content": content,
        }).encode()
        req = urllib.request.Request(
            f"{self.site}/api/v1/messages", data=data, method="POST"
        )
        req.add_header("Authorization", f"Basic {self._cred}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._opener.open(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
            if result.get("result") == "success":
                return result.get("id")
        except Exception as e:
            log.warning("Zulip post failed: %s", e)
        return None

    def update(self, message_id: int, content: str) -> bool:
        """Edit an existing message. Returns True on success."""
        data = urllib.parse.urlencode({"content": content}).encode()
        req = urllib.request.Request(
            f"{self.site}/api/v1/messages/{message_id}",
            data=data,
            method="PATCH",
        )
        req.add_header("Authorization", f"Basic {self._cred}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._opener.open(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
            return result.get("result") == "success"
        except Exception as e:
            log.warning("Zulip update failed: %s", e)
            return False


class TranscriptWatcher:
    """Watch a transcript JSONL file and post tool activity to Zulip.

    All status lines are accumulated into a single Zulip message that is
    updated in place, preventing topic flooding.

    Usage:
        watcher = TranscriptWatcher(transcript_path, poster)
        task = asyncio.create_task(watcher.run())
        # ... later ...
        watcher.stop()
        await task
    """

    def __init__(
        self,
        transcript_path: str | Path,
        poster: ZulipPoster,
        *,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.transcript_path = Path(transcript_path)
        self.poster = poster
        self.poll_interval = poll_interval
        self._running = True
        self._offset = 0  # File position to read from
        self._status_msg_id: int | None = None  # Single status message to update
        self._status_lines: list[str] = []  # Accumulated status lines

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._running = False

    async def send_ack(self) -> None:
        """Post initial ACK message to Zulip. Call after message injection."""
        loop = asyncio.get_running_loop()
        msg_id = await loop.run_in_executor(
            None, self.poster.post, "\u23f3 Working..."  # ⏳
        )
        if msg_id:
            self._status_msg_id = msg_id
            log.info("Posted ACK message: %d", msg_id)

    async def run(self) -> None:
        """Main loop: tail transcript file, extract tool_use, post to Zulip."""
        log.info("TranscriptWatcher started: %s", self.transcript_path)

        # Seek to end of file (only watch new entries)
        if self.transcript_path.exists():
            self._offset = self.transcript_path.stat().st_size

        try:
            while self._running:
                await asyncio.sleep(self.poll_interval)

                if not self.transcript_path.exists():
                    continue

                current_size = self.transcript_path.stat().st_size
                if current_size == self._offset:
                    continue  # No new data

                # File was truncated/replaced (new session) — reset to start
                if current_size < self._offset:
                    log.info(
                        "Transcript file shrunk (%d < %d), resetting offset",
                        current_size, self._offset,
                    )
                    self._offset = 0

                # Read new lines
                new_lines = self._read_new_lines()
                if not new_lines:
                    continue

                # Collect all status updates from this batch
                batch_lines: list[str] = []
                for line in new_lines:
                    # Check for tool_use entries
                    tool_uses = _extract_tool_uses(line)
                    if tool_uses:
                        # Batch all tools from a single assistant message together
                        for name, inp in tool_uses:
                            batch_lines.append(_format_tool_status(name, inp))
                    elif _is_assistant_text(line):
                        batch_lines.append("\U0001f4ac Responding...")  # 💬

                if batch_lines:
                    self._status_lines.extend(batch_lines)
                    await self._update_status_message()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("TranscriptWatcher error: %s", e)
        finally:
            log.info(
                "TranscriptWatcher stopped: %s (posted %d status updates)",
                self.transcript_path,
                len(self._status_lines),
            )

    def _read_new_lines(self) -> list[str]:
        """Read new complete lines from the transcript file."""
        try:
            with open(self.transcript_path, "r") as f:
                f.seek(self._offset)
                data = f.read()
                self._offset = f.tell()
        except OSError as e:
            log.warning("Failed to read transcript: %s", e)
            return []

        lines = []
        for line in data.splitlines():
            line = line.strip()
            if line:
                lines.append(line)
        return lines

    async def _update_status_message(self) -> None:
        """Update the single status message with all accumulated lines."""
        content = "\n".join(self._status_lines)
        loop = asyncio.get_running_loop()

        if self._status_msg_id:
            # Update existing message
            await loop.run_in_executor(
                None, self.poster.update, self._status_msg_id, content,
            )
        else:
            # No ACK was sent — create new message
            msg_id = await loop.run_in_executor(
                None, self.poster.post, content,
            )
            if msg_id:
                self._status_msg_id = msg_id


def discover_transcript(
    project_path: str | Path,
    session_id: str,
    *,
    claude_home: Path | None = None,
) -> Path | None:
    """Discover the transcript JSONL path for a given session.

    Claude Code stores transcripts at:
        ~/.claude/projects/-<path-with-dashes>/<session_id>.jsonl

    Args:
        project_path: The project directory (used to derive Claude's project hash).
        session_id: The Claude session UUID.
        claude_home: Override for ~/.claude (for testing).

    Returns the path if it exists, or None. Does NOT fall back to other sessions
    to avoid accidentally watching the wrong transcript.
    """
    project_path = Path(project_path).resolve()
    # Claude's project hash: absolute path with / replaced by -
    # e.g. /home/user/project → -home-user-project (leading / becomes -)
    project_hash = str(project_path).replace("/", "-")
    claude_dir = (claude_home or Path.home() / ".claude") / "projects" / project_hash
    transcript = claude_dir / f"{session_id}.jsonl"

    if transcript.exists():
        return transcript

    # Return the expected path even if it doesn't exist yet —
    # Claude may not have created it at startup time.
    # The watcher's run() loop handles non-existent files gracefully.
    return transcript
