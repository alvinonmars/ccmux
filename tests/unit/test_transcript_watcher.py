"""Tests for adapters.zulip_adapter.transcript_watcher."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from adapters.zulip_adapter.transcript_watcher import (
    MAX_STATUS_MESSAGE_CHARS,
    TranscriptWatcher,
    ZulipPoster,
    _extract_tool_uses,
    _format_tool_status,
    _is_assistant_text,
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
# _is_assistant_text
# ---------------------------------------------------------------------------

class TestIsAssistantText:
    def test_text_only_message(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello world"}],
            }
        })
        assert _is_assistant_text(line) is True

    def test_tool_use_message(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                ],
            }
        })
        assert _is_assistant_text(line) is False

    def test_mixed_text_and_tool(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}},
                ],
            }
        })
        assert _is_assistant_text(line) is False

    def test_user_message_ignored(self):
        line = json.dumps({
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}],
            }
        })
        assert _is_assistant_text(line) is False

    def test_empty_text_ignored(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "  "}],
            }
        })
        assert _is_assistant_text(line) is False

    def test_invalid_json(self):
        assert _is_assistant_text("not json") is False


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

    def test_read_shows_full_path(self):
        result = _format_tool_status("Read", {"file_path": "/home/user/project/src/main.py"})
        assert "/home/user/project/src/main.py" in result

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

    def test_long_input_preserved(self):
        """Long inputs are no longer truncated (message chaining handles overflow)."""
        long_cmd = "x" * 200
        result = _format_tool_status("Bash", {"command": long_cmd})
        assert long_cmd in result


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

    def test_nonexistent_returns_expected_path(self, tmp_path):
        """When session JSONL doesn't exist, return expected path (not None)."""
        claude_home = tmp_path / ".claude"
        projects_dir = claude_home / "projects" / "-home-user-project"
        projects_dir.mkdir(parents=True)

        result = discover_transcript(
            "/home/user/project", "nonexistent-id", claude_home=claude_home
        )
        assert result is not None
        assert result.name == "nonexistent-id.jsonl"
        assert not result.exists()

    def test_no_claude_dir_returns_expected_path(self, tmp_path):
        """Even without .claude dir, return expected path for watcher to wait on."""
        claude_home = tmp_path / ".claude"
        result = discover_transcript(
            "/home/user/project", "abc-123", claude_home=claude_home
        )
        assert result is not None
        assert result.name == "abc-123.jsonl"


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

        # No ACK → should post a new message with accumulated content
        assert mock_poster.post.called
        call_args = mock_poster.post.call_args[0][0]
        assert "ls" in call_args

    @pytest.mark.asyncio
    async def test_shows_tool_result(self, transcript_file, mock_poster):
        """Tool results are displayed in the status message."""
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

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

        assert mock_poster.post.called
        content = mock_poster.post.call_args[0][0]
        assert "output here" in content

    @pytest.mark.asyncio
    async def test_ack_message_updated_not_new_post(
        self, transcript_file, mock_poster
    ):
        """With ACK message, all tool updates go to the same message via update()."""
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )
        await watcher.send_ack()

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

        # First tool
        entry1 = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry1 + "\n")

        await asyncio.sleep(0.3)

        # Second tool
        entry2 = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry2 + "\n")

        await asyncio.sleep(0.3)

        watcher.stop()
        await task

        # Both updates should go to the ACK message (id=42), not new posts
        assert mock_poster.update.call_count >= 2
        for call in mock_poster.update.call_args_list:
            assert call[0][0] == 42  # All updates to ACK message id

        # The post() after ACK should NOT have been called for tool status
        # (only the initial ACK post)
        assert mock_poster.post.call_count == 1  # Only the ACK itself

    @pytest.mark.asyncio
    async def test_accumulated_status_content(
        self, transcript_file, mock_poster
    ):
        """Status lines accumulate — second update contains both lines."""
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )
        await watcher.send_ack()

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

        entry1 = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry1 + "\n")
        await asyncio.sleep(0.3)

        entry2 = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry2 + "\n")
        await asyncio.sleep(0.3)

        watcher.stop()
        await task

        # Last update should contain both status lines
        last_content = mock_poster.update.call_args_list[-1][0][1]
        assert "a.py" in last_content
        assert "ls" in last_content

    @pytest.mark.asyncio
    async def test_parallel_tools_batched(self, transcript_file, mock_poster):
        """Multiple tool_use blocks in one assistant message are batched."""
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

        # Single assistant message with 3 parallel tool calls
        entry = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/b.py"}},
                    {"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}},
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry + "\n")

        await asyncio.sleep(0.3)
        watcher.stop()
        await task

        # Should post ONE message containing all 3 tool statuses
        assert mock_poster.post.call_count == 1
        content = mock_poster.post.call_args[0][0]
        assert "a.py" in content
        assert "b.py" in content
        assert "TODO" in content

    @pytest.mark.asyncio
    async def test_file_truncation_resets_offset(
        self, transcript_file, mock_poster
    ):
        """When transcript file shrinks (new session), offset resets and reads new data."""
        # Write large initial content so offset is high
        old_entries = []
        for i in range(10):
            old_entries.append(json.dumps({
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": f"old-command-{i}-padding" + "x" * 50}}
                    ],
                }
            }))
        transcript_file.write_text("\n".join(old_entries) + "\n")

        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )
        # Watcher starts, seeks to end of large file
        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.2)

        # Truncate and write small new content — clearly smaller than offset
        new_entry = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "new"}}
                ],
            }
        })
        transcript_file.write_text(new_entry + "\n")

        await asyncio.sleep(0.4)
        watcher.stop()
        await task

        # Should have detected the new content after truncation
        assert mock_poster.post.called
        content = mock_poster.post.call_args[0][0]
        assert "new" in content

    @pytest.mark.asyncio
    async def test_assistant_text_shows_content(
        self, transcript_file, mock_poster
    ):
        """Assistant text-only messages show actual text content."""
        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

        entry = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Here is my analysis..."}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry + "\n")

        await asyncio.sleep(0.3)
        watcher.stop()
        await task

        assert mock_poster.post.called
        content = mock_poster.post.call_args[0][0]
        assert "Here is my analysis" in content

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
        mock_poster.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_chaining_on_overflow(
        self, transcript_file, mock_poster
    ):
        """When content exceeds MAX_STATUS_MESSAGE_CHARS, chain to new message."""
        # Use incrementing message IDs for each post() call
        msg_ids = iter(range(100, 200))
        mock_poster.post.side_effect = lambda _: next(msg_ids)

        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

        # Generate enough tool calls to exceed MAX_STATUS_MESSAGE_CHARS
        lines_needed = (MAX_STATUS_MESSAGE_CHARS // 40) + 5
        entries = []
        for i in range(lines_needed):
            entries.append(json.dumps({
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": f"command-{i:04d}-{'x' * 20}"}}
                    ],
                }
            }))
        with open(transcript_file, "a") as f:
            f.write("\n".join(entries) + "\n")

        await asyncio.sleep(0.5)
        watcher.stop()
        await task

        # Should have created multiple messages (chaining)
        assert mock_poster.post.call_count >= 2, (
            f"Expected chaining (>=2 posts), got {mock_poster.post.call_count}"
        )

        # All content should be preserved across the chain
        all_content = ""
        for call in mock_poster.post.call_args_list:
            all_content += call[0][0] + "\n"
        for call in mock_poster.update.call_args_list:
            all_content += call[0][1] + "\n"

        # Spot-check: first and last commands should appear somewhere
        assert "command-0000" in all_content
        assert f"command-{lines_needed - 1:04d}" in all_content

    @pytest.mark.asyncio
    async def test_single_oversized_line_handled(
        self, transcript_file, mock_poster
    ):
        """A single line exceeding MAX_STATUS_MESSAGE_CHARS is hard-truncated at message level."""
        mock_poster.post.return_value = 42

        watcher = TranscriptWatcher(
            transcript_file, mock_poster, poll_interval=0.1
        )

        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.15)

        # Single tool call with a huge pattern (exceeds message limit)
        huge_pattern = "x" * (MAX_STATUS_MESSAGE_CHARS + 500)
        entry = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Grep",
                     "input": {"pattern": huge_pattern}}
                ],
            }
        })
        with open(transcript_file, "a") as f:
            f.write(entry + "\n")

        await asyncio.sleep(0.3)
        watcher.stop()
        await task

        # Should have posted — message-level truncation keeps it under limit
        assert mock_poster.post.called
        content = mock_poster.post.call_args[0][0]
        assert len(content) <= MAX_STATUS_MESSAGE_CHARS
