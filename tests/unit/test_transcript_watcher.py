"""Tests for adapters.zulip_adapter.transcript_watcher."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adapters.zulip_adapter.transcript_watcher import (
    TranscriptWatcher,
    ZulipPoster,
    _extract_tool_uses,
    _format_tool_status,
    discover_transcript,
)


# ---------------------------------------------------------------------------
# _extract_tool_uses
# ---------------------------------------------------------------------------

class TestExtractToolUses:
    def test_bash_tool_use(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}
                ],
            }
        })
        result = _extract_tool_uses(line)
        assert len(result) == 1
        assert result[0] == ("Bash", {"command": "npm test"})

    def test_multiple_tool_uses(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}},
                    {"type": "tool_use", "name": "Glob", "input": {"pattern": "*.ts"}},
                ],
            }
        })
        result = _extract_tool_uses(line)
        assert len(result) == 2
        assert result[0][0] == "Read"
        assert result[1][0] == "Glob"

    def test_non_assistant_role_ignored(self):
        line = json.dumps({
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "output"},
                ],
            }
        })
        assert _extract_tool_uses(line) == []

    def test_text_content_no_tool_use(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hello world"},
                ],
            }
        })
        assert _extract_tool_uses(line) == []

    def test_mixed_content(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x.py"}},
                    {"type": "text", "text": "Done"},
                ],
            }
        })
        result = _extract_tool_uses(line)
        assert len(result) == 1
        assert result[0][0] == "Edit"

    def test_invalid_json(self):
        assert _extract_tool_uses("not json") == []

    def test_empty_line(self):
        assert _extract_tool_uses("") == []

    def test_no_message_key(self):
        assert _extract_tool_uses(json.dumps({"type": "progress"})) == []

    def test_content_not_list(self):
        line = json.dumps({
            "message": {"role": "assistant", "content": "plain string"}
        })
        assert _extract_tool_uses(line) == []


# ---------------------------------------------------------------------------
# _format_tool_status
# ---------------------------------------------------------------------------

class TestFormatToolStatus:
    def test_bash_with_description(self):
        result = _format_tool_status("Bash", {"command": "npm test", "description": "Run tests"})
        assert "Run tests" in result
        assert "\u2699\ufe0f" in result  # ⚙️

    def test_bash_command_only(self):
        result = _format_tool_status("Bash", {"command": "npm test"})
        assert "npm test" in result

    def test_read_shows_filename_only(self):
        result = _format_tool_status("Read", {"file_path": "/home/user/project/src/main.py"})
        assert "main.py" in result
        assert "/home/user" not in result

    def test_edit(self):
        result = _format_tool_status("Edit", {"file_path": "/a/b/config.ts"})
        assert "config.ts" in result
        assert "\u270f\ufe0f" in result  # ✏️

    def test_grep(self):
        result = _format_tool_status("Grep", {"pattern": "TODO.*fix"})
        assert "TODO.*fix" in result

    def test_agent(self):
        result = _format_tool_status("Agent", {"description": "Explore codebase"})
        assert "Explore codebase" in result

    def test_unknown_tool(self):
        result = _format_tool_status("CustomTool", {})
        assert "CustomTool" in result
        assert "\U0001f527" in result  # 🔧

    def test_long_input_truncated(self):
        long_cmd = "x" * 200
        result = _format_tool_status("Bash", {"command": long_cmd})
        assert "..." in result
        assert len(result) < 200


# ---------------------------------------------------------------------------
# discover_transcript
# ---------------------------------------------------------------------------

class TestDiscoverTranscript:
    def test_exact_session_id(self, tmp_path):
        claude_home = tmp_path / ".claude"
        projects_dir = claude_home / "projects" / "-home-user-project"
        projects_dir.mkdir(parents=True)
        transcript = projects_dir / "abc-123.jsonl"
        transcript.write_text("{}\n")

        result = discover_transcript(
            "/home/user/project", "abc-123", claude_home=claude_home
        )
        assert result == transcript

    def test_fallback_most_recent(self, tmp_path):
        claude_home = tmp_path / ".claude"
        projects_dir = claude_home / "projects" / "-home-user-project"
        projects_dir.mkdir(parents=True)
        old = projects_dir / "old-session.jsonl"
        old.write_text("{}\n")
        import time
        time.sleep(0.01)
        new = projects_dir / "new-session.jsonl"
        new.write_text("{}\n")

        result = discover_transcript(
            "/home/user/project", "nonexistent", claude_home=claude_home
        )
        assert result == new

    def test_no_claude_dir(self, tmp_path):
        claude_home = tmp_path / ".claude"
        result = discover_transcript(
            "/home/user/project", "abc-123", claude_home=claude_home
        )
        assert result is None


# ---------------------------------------------------------------------------
# TranscriptWatcher
# ---------------------------------------------------------------------------

class TestTranscriptWatcher:
    @pytest.fixture
    def transcript_file(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text("")
        return f

    @pytest.fixture
    def mock_poster(self):
        poster = MagicMock(spec=ZulipPoster)
        poster.post.return_value = 42  # message_id
        poster.update.return_value = True
        return poster

    @pytest.mark.asyncio
    async def test_detects_new_tool_use(self, transcript_file, mock_poster):
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())

        # Wait for watcher to start and seek to end
        await asyncio.sleep(0.15)

        # Append a tool_use entry
        entry = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry + "\n")

        # Wait for detection
        await asyncio.sleep(0.3)

        watcher.stop()
        await task

        # Should have posted a tool status
        assert mock_poster.post.called
        call_args = mock_poster.post.call_args[0][0]
        assert "ls" in call_args

    @pytest.mark.asyncio
    async def test_ignores_tool_result(self, transcript_file, mock_poster):
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

        # Append a tool_result (should be ignored)
        entry = json.dumps({
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "output here"}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry + "\n")

        await asyncio.sleep(0.3)
        watcher.stop()
        await task

        mock_poster.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_ack_message_updated_on_first_tool(
        self, transcript_file, mock_poster
    ):
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )
        watcher.send_ack()

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

        entry = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry + "\n")

        await asyncio.sleep(0.3)
        watcher.stop()
        await task

        # First tool should UPDATE the ACK message, not post new
        mock_poster.update.assert_called_once()
        assert mock_poster.update.call_args[0][0] == 42  # message_id from ACK

    @pytest.mark.asyncio
    async def test_stop_is_clean(self, transcript_file, mock_poster):
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)
        watcher.stop()
        await task  # Should not hang

    @pytest.mark.asyncio
    async def test_missing_file_no_crash(self, tmp_path, mock_poster):
        watcher = TranscriptWatcher(
            tmp_path / "nonexistent.jsonl", mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.3)
        watcher.stop()
        await task  # Should not crash

    @pytest.mark.asyncio
    async def test_ignores_pre_existing_content(
        self, transcript_file, mock_poster
    ):
        # Write content BEFORE watcher starts
        entry = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "old"}}
                ],
            }
        })
        transcript_file.write_text(entry + "\n")

        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.3)
        watcher.stop()
        await task

        # Should NOT have posted anything (pre-existing content ignored)
        mock_poster.post.assert_not_called()
