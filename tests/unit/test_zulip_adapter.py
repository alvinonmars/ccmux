"""Unit tests for the Zulip adapter: config, injector, process_mgr, adapter, helpers.

Covers acceptance criteria AC-1 through AC-6b from docs/zulip-implementation-plan.md.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

from adapters.zulip_adapter.process_mgr import CreateMode


# ============================================================================
# AC-1: ccmux-init (scripts/ccmux_init.py)
# ============================================================================


class TestCcmuxInit:
    """AC-1: Project init tool."""

    def _import_init(self):
        """Import ccmux_init module."""
        from scripts import ccmux_init
        # Reload to avoid stale module state
        import importlib
        return importlib.reload(ccmux_init)

    def test_ac1_1_init_empty_project(self, tmp_path: Path) -> None:
        """AC-1.1: Init on empty project dir creates settings, CLAUDE.md, .gitignore."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()
        # Create a git dir so pre-commit can be installed
        (project / ".git" / "hooks").mkdir(parents=True)

        result = mod.main([str(project)])
        assert result == 0

        # Stop hook installed
        settings = json.loads((project / ".claude" / "settings.json").read_text())
        assert "hooks" in settings
        assert "Stop" in settings["hooks"]
        stop_hooks = settings["hooks"]["Stop"]
        assert len(stop_hooks) >= 1

        # CLAUDE.md created
        assert (project / "CLAUDE.md").exists()

        # .gitignore updated
        assert ".claude/" in (project / ".gitignore").read_text()

    def test_ac1_2_init_preserves_existing_hooks(self, tmp_path: Path) -> None:
        """AC-1.2: Init on project with existing settings.json preserves existing hooks."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()
        claude_dir = project / ".claude"
        claude_dir.mkdir()

        existing = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "/usr/bin/existing-hook"}]}
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing))

        result = mod.main([str(project)])
        assert result == 0

        settings = json.loads((claude_dir / "settings.json").read_text())
        stop_hooks = settings["hooks"]["Stop"]
        # Both existing and new hook should be present
        assert len(stop_hooks) == 2
        commands = []
        for wrapper in stop_hooks:
            for h in wrapper.get("hooks", []):
                commands.append(h.get("command", ""))
        assert "/usr/bin/existing-hook" in commands
        assert any("zulip_relay_hook" in c for c in commands)

    def test_ac1_3_init_no_overwrite_claude_md(self, tmp_path: Path) -> None:
        """AC-1.3: Init on project with existing CLAUDE.md does NOT overwrite."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# My Custom Rules\n")

        mod.main([str(project)])
        assert (project / "CLAUDE.md").read_text() == "# My Custom Rules\n"

    def test_ac1_4_init_no_overwrite_precommit(self, tmp_path: Path) -> None:
        """AC-1.4: Init on project with existing pre-commit hook does NOT overwrite."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()
        hooks_dir = project / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)
        precommit = hooks_dir / "pre-commit"
        precommit.write_text("#!/bin/bash\necho existing\n")

        mod.main([str(project)])
        assert precommit.read_text() == "#!/bin/bash\necho existing\n"

    def test_ac1_5_init_gitignore_already_has_entry(self, tmp_path: Path) -> None:
        """AC-1.5: Init on project with .gitignore containing all entries does NOT modify."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".gitignore").write_text("node_modules/\n.claude/\n.zulip-uploads/\n")

        mod.main([str(project)])
        assert (project / ".gitignore").read_text() == "node_modules/\n.claude/\n.zulip-uploads/\n"

    def test_ac1_6_idempotent(self, tmp_path: Path) -> None:
        """AC-1.6: Running init twice produces identical state."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".git" / "hooks").mkdir(parents=True)

        mod.main([str(project)])
        # Capture state after first run
        settings1 = (project / ".claude" / "settings.json").read_text()
        claude_md1 = (project / "CLAUDE.md").read_text()
        gitignore1 = (project / ".gitignore").read_text()

        mod.main([str(project)])
        assert (project / ".claude" / "settings.json").read_text() == settings1
        assert (project / "CLAUDE.md").read_text() == claude_md1
        assert (project / ".gitignore").read_text() == gitignore1

    def test_ac1_7_non_git_directory(self, tmp_path: Path) -> None:
        """AC-1.7: Non-git directory skips pre-commit hook; other actions still run."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()

        result = mod.main([str(project)])
        assert result == 0

        # No .git means no pre-commit hook
        assert not (project / ".git").exists()
        # But settings.json and CLAUDE.md should exist
        assert (project / ".claude" / "settings.json").exists()
        assert (project / "CLAUDE.md").exists()

    def test_ac1_8_whatsapp_mcp_capability(self, tmp_path: Path) -> None:
        """AC-1.8: Init with whatsapp_mcp capability installs WhatsApp MCP."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()

        mod.main([str(project), "--capabilities", '{"whatsapp_mcp": true}'])

        mcp_path = project / ".mcp.json"
        assert mcp_path.exists()
        mcp_data = json.loads(mcp_path.read_text())
        assert "whatsapp" in mcp_data.get("mcpServers", {})

    def test_ac1_9_empty_capabilities(self, tmp_path: Path) -> None:
        """AC-1.9: Init with empty capabilities makes no .mcp.json modifications."""
        mod = self._import_init()
        project = tmp_path / "myproject"
        project.mkdir()

        mod.main([str(project), "--capabilities", "{}"])
        assert not (project / ".mcp.json").exists()


# ============================================================================
# AC-2: zulip_relay_hook.py
# ============================================================================


class TestZulipRelayHook:
    """AC-2: Stop hook for Zulip instances."""

    HOOK_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "zulip_relay_hook.py"

    def _run_hook(
        self,
        stdin_data: str,
        env_override: dict | None = None,
        tmp_path: Path | None = None,
    ) -> subprocess.CompletedProcess:
        """Run the relay hook script with given stdin and env."""
        env = {k: v for k, v in os.environ.items()}
        # Clear Zulip vars by default
        for key in ["ZULIP_STREAM", "ZULIP_TOPIC", "ZULIP_SITE",
                     "ZULIP_BOT_EMAIL", "ZULIP_BOT_API_KEY_FILE"]:
            env.pop(key, None)
        if env_override:
            env.update(env_override)

        return subprocess.run(
            [sys.executable, str(self.HOOK_SCRIPT)],
            input=stdin_data.encode(),
            capture_output=True,
            timeout=10,
            env=env,
        )

    def test_ac2_1_no_zulip_stream(self) -> None:
        """AC-2.1: ZULIP_STREAM not set → exits immediately, exit code 0."""
        result = self._run_hook('{"last_assistant_message": "hello"}')
        assert result.returncode == 0

    def test_ac2_3_empty_message(self, tmp_path: Path) -> None:
        """AC-2.3: Empty last_assistant_message → exits without API call."""
        cred_file = tmp_path / "cred.env"
        cred_file.write_text("ZULIP_BOT_API_KEY=test123\n")

        result = self._run_hook(
            '{"last_assistant_message": ""}',
            env_override={
                "ZULIP_STREAM": "test-stream",
                "ZULIP_TOPIC": "test-topic",
                "ZULIP_SITE": "https://zulip.example.com",
                "ZULIP_BOT_EMAIL": "bot@example.com",
                "ZULIP_BOT_API_KEY_FILE": str(cred_file),
            },
        )
        assert result.returncode == 0

    def test_ac2_4_malformed_json(self) -> None:
        """AC-2.4: Malformed JSON on stdin → exits gracefully, exit code 0."""
        result = self._run_hook(
            "not valid json",
            env_override={"ZULIP_STREAM": "test-stream"},
        )
        assert result.returncode == 0

    def test_ac2_7_missing_credentials(self) -> None:
        """AC-2.7: Missing credentials file → exits gracefully."""
        result = self._run_hook(
            '{"last_assistant_message": "hello"}',
            env_override={
                "ZULIP_STREAM": "test-stream",
                "ZULIP_BOT_API_KEY_FILE": "/nonexistent/cred.env",
            },
        )
        assert result.returncode == 0

    def test_ac2_8_stdlib_only(self) -> None:
        """AC-2.8: Script uses only stdlib imports."""
        content = self.HOOK_SCRIPT.read_text()
        # Check that there are no third-party imports
        import_lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        stdlib_modules = {
            "json", "os", "sys", "urllib.request", "urllib.parse",
            "base64", "urllib", "__future__", "re", "pathlib",
        }
        for line in import_lines:
            parts = line.replace("import ", "").replace("from ", "").split()
            module = parts[0].split(".")[0].rstrip(",")
            assert module in stdlib_modules, f"Non-stdlib import: {line}"

    def test_ac2_10_default_topic(self, tmp_path: Path) -> None:
        """AC-2.10: ZULIP_TOPIC not set → falls back to 'chat'."""
        # Verify source code contains the "chat" default for ZULIP_TOPIC
        source = self.HOOK_SCRIPT.read_text()
        assert 'os.environ.get("ZULIP_TOPIC", "chat")' in source, \
            "Default topic 'chat' not found in relay hook source"

        cred_file = tmp_path / "cred.env"
        cred_file.write_text("ZULIP_BOT_API_KEY=test123\n")

        # Verify the script runs without error when ZULIP_TOPIC is unset
        result = self._run_hook(
            '{"last_assistant_message": "hello"}',
            env_override={
                "ZULIP_STREAM": "test-stream",
                # No ZULIP_TOPIC set
                "ZULIP_SITE": "https://zulip.example.com",
                "ZULIP_BOT_EMAIL": "bot@example.com",
                "ZULIP_BOT_API_KEY_FILE": str(cred_file),
            },
        )
        # Script will fail at API call (no real server) but should not crash
        assert result.returncode == 0


# ============================================================================
# AC-3: injector.py
# ============================================================================


class TestInjector:
    """AC-3: FIFO-to-tmux injector."""

    def test_ac3_5_fifo_nonblocking(self, tmp_path: Path) -> None:
        """AC-3.5: FIFO uses O_RDWR | O_NONBLOCK — no EOF when no writer."""
        from adapters.zulip_adapter.injector import Injector

        fifo_path = tmp_path / "test.fifo"
        os.mkfifo(str(fifo_path))

        injector = Injector(str(fifo_path), "nonexistent-session")
        # Verify it can be constructed without blocking
        assert injector.fifo_path == str(fifo_path)
        assert injector._running is True

    def test_ac3_prompt_detection(self) -> None:
        """AC-3: Claude prompt pattern matches ❯."""
        from adapters.zulip_adapter.injector import CLAUDE_PROMPT_RE, SHELL_PROMPT_RE

        # Claude prompt
        assert CLAUDE_PROMPT_RE.search("some output\n❯ ")
        assert CLAUDE_PROMPT_RE.search("❯")
        assert CLAUDE_PROMPT_RE.search("text\n❯  ")

        # Shell prompt
        assert SHELL_PROMPT_RE.search("user@host:~$ ")
        assert SHELL_PROMPT_RE.search("root@host:~# ")
        assert not SHELL_PROMPT_RE.search("❯ ")

    def test_ac3_injection_gate_ready(self) -> None:
        """AC-3: InjectionGate.is_ready() returns True when Claude prompt visible and terminal idle."""
        from adapters.zulip_adapter.injector import InjectionGate

        gate = InjectionGate("test-session")

        with patch("adapters.zulip_adapter.injector._tmux_client_activity") as mock_activity, \
             patch("adapters.zulip_adapter.injector._tmux_capture") as mock_capture:
            # Terminal idle for 10 seconds, Claude prompt visible
            mock_activity.return_value = time.time() - 10
            mock_capture.return_value = "Some output\n❯ "
            assert gate.is_ready() is True

    def test_ac3_injection_gate_not_ready_typing(self) -> None:
        """AC-3.3: Terminal active → not ready."""
        from adapters.zulip_adapter.injector import InjectionGate

        gate = InjectionGate("test-session")

        with patch("adapters.zulip_adapter.injector._tmux_client_activity") as mock_activity, \
             patch("adapters.zulip_adapter.injector._tmux_capture") as mock_capture:
            # Terminal active (just now)
            mock_activity.return_value = time.time()
            mock_capture.return_value = "Some output\n❯ "
            assert gate.is_ready() is False

    def test_ac3_injection_gate_not_ready_generating(self) -> None:
        """AC-3.2: Claude generating (no prompt) → not ready."""
        from adapters.zulip_adapter.injector import InjectionGate

        gate = InjectionGate("test-session")

        with patch("adapters.zulip_adapter.injector._tmux_client_activity") as mock_activity, \
             patch("adapters.zulip_adapter.injector._tmux_capture") as mock_capture:
            mock_activity.return_value = time.time() - 10
            mock_capture.return_value = "Working on something..."
            assert gate.is_ready() is False

    def test_ac3_7_claude_dead_detection(self) -> None:
        """AC-3.7: Shell prompt without Claude prompt → Claude exited."""
        from adapters.zulip_adapter.injector import InjectionGate

        gate = InjectionGate("test-session")

        with patch("adapters.zulip_adapter.injector._tmux_capture") as mock_capture:
            # Shell prompt visible, no Claude prompt
            mock_capture.return_value = "user@host:~$ "
            assert gate.is_claude_dead() is True

            # Claude prompt visible → not dead
            mock_capture.return_value = "Some output\n❯ "
            assert gate.is_claude_dead() is False

            # Both visible → not dead (Claude prompt takes priority)
            mock_capture.return_value = "$ old prompt\n❯ "
            assert gate.is_claude_dead() is False

    def test_ac3_inject_text(self) -> None:
        """AC-3.1: _inject_text sends via tmux send-keys."""
        from adapters.zulip_adapter.injector import _inject_text

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _inject_text("test-session", "hello world")
            assert result is True
            assert mock_run.call_count == 2  # send-keys -l + Enter

            # First call: send-keys -l with the text
            text_call = mock_run.call_args_list[0]
            text_args = text_call[0][0]  # positional args list
            assert "send-keys" in text_args
            assert "-l" in text_args
            assert "hello world" in text_args
            assert "test-session" in text_args

            # Second call: send Enter key
            enter_call = mock_run.call_args_list[1]
            enter_args = enter_call[0][0]
            assert "send-keys" in enter_args
            assert "Enter" in enter_args

    def test_ac3_inject_text_fails_on_nonzero_rc(self) -> None:
        """AC-3.1b: _inject_text returns False when send-keys fails."""
        from adapters.zulip_adapter.injector import _inject_text

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stderr=b"no session: test-session"
            )
            result = _inject_text("test-session", "hello")
            assert result is False
            # Only one call — fails on the first send-keys
            assert mock_run.call_count == 1


# ============================================================================
# AC-4: config.py
# ============================================================================


class TestConfig:
    """AC-4: Stream config loader."""

    def _write_ccmux_toml(self, path: Path, zulip_section: dict) -> None:
        """Write a minimal ccmux.toml with [zulip] section."""
        lines = ["[zulip]"]
        for k, v in zulip_section.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            else:
                lines.append(f"{k} = {v}")
        (path / "ccmux.toml").write_text("\n".join(lines) + "\n")

    def test_ac4_1_valid_streams(self, tmp_path: Path) -> None:
        """AC-4.1: Valid streams directory loads all stream.toml fields."""
        from adapters.zulip_adapter.config import load, StreamConfig

        # Create credential file
        cred_file = tmp_path / "cred.env"
        cred_file.write_text("ZULIP_BOT_API_KEY=test\n")

        # Create streams dir with one stream
        streams_dir = tmp_path / "streams"
        stream_dir = streams_dir / "test-project"
        stream_dir.mkdir(parents=True)
        (stream_dir / "stream.toml").write_text(
            'project_path = "/home/user/project"\nchannel = "zulip"\n'
        )

        # Create env template
        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text("# empty\n")

        # Write ccmux.toml
        self._write_ccmux_toml(tmp_path, {
            "site": "https://zulip.example.com",
            "bot_email": "bot@example.com",
            "bot_credentials": str(cred_file),
            "streams_dir": str(streams_dir),
            "env_template": str(env_tpl),
        })

        cfg = load(project_root=tmp_path)
        assert "test-project" in cfg.streams
        assert cfg.streams["test-project"].project_path == Path("/home/user/project")
        assert cfg.streams["test-project"].channel == "zulip"

    def test_ac4_2_empty_streams_dir(self, tmp_path: Path) -> None:
        """AC-4.2: Empty streams directory returns empty dict."""
        from adapters.zulip_adapter.config import load

        cred_file = tmp_path / "cred.env"
        cred_file.write_text("ZULIP_BOT_API_KEY=test\n")

        streams_dir = tmp_path / "streams"
        streams_dir.mkdir()

        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text("# empty\n")

        self._write_ccmux_toml(tmp_path, {
            "site": "https://zulip.example.com",
            "bot_email": "bot@example.com",
            "bot_credentials": str(cred_file),
            "streams_dir": str(streams_dir),
            "env_template": str(env_tpl),
        })

        cfg = load(project_root=tmp_path)
        assert cfg.streams == {}

    def test_ac4_3_reads_zulip_section(self, tmp_path: Path) -> None:
        """AC-4.3: Reads site, bot_email, bot_credentials correctly."""
        from adapters.zulip_adapter.config import load

        cred_file = tmp_path / "cred.env"
        cred_file.write_text("ZULIP_BOT_API_KEY=test\n")

        streams_dir = tmp_path / "streams"
        streams_dir.mkdir()

        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text("# empty\n")

        self._write_ccmux_toml(tmp_path, {
            "site": "https://zulip.example.com",
            "bot_email": "bot@example.com",
            "bot_credentials": str(cred_file),
            "streams_dir": str(streams_dir),
            "env_template": str(env_tpl),
        })

        cfg = load(project_root=tmp_path)
        assert cfg.site == "https://zulip.example.com"
        assert cfg.bot_email == "bot@example.com"
        assert cfg.bot_credentials == cred_file

    def test_ac4_4_missing_zulip_section(self, tmp_path: Path) -> None:
        """AC-4.4: Missing [zulip] section raises clear error."""
        from adapters.zulip_adapter.config import load

        (tmp_path / "ccmux.toml").write_text('[project]\nname = "test"\n')
        with pytest.raises(ValueError, match="zulip"):
            load(project_root=tmp_path)

    def test_ac4_5_hot_reload(self, tmp_path: Path) -> None:
        """AC-4.5: New stream.toml added while running is detected."""
        from adapters.zulip_adapter.config import load, scan_streams

        cred_file = tmp_path / "cred.env"
        cred_file.write_text("ZULIP_BOT_API_KEY=test\n")

        streams_dir = tmp_path / "streams"
        streams_dir.mkdir()

        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text("# empty\n")

        self._write_ccmux_toml(tmp_path, {
            "site": "https://zulip.example.com",
            "bot_email": "bot@example.com",
            "bot_credentials": str(cred_file),
            "streams_dir": str(streams_dir),
            "env_template": str(env_tpl),
        })

        cfg = load(project_root=tmp_path)
        assert len(cfg.streams) == 0

        # Add a new stream
        new_stream = streams_dir / "new-project"
        new_stream.mkdir()
        (new_stream / "stream.toml").write_text(
            'project_path = "/home/user/new"\nchannel = "zulip"\n'
        )

        # Force mtime change detection
        cfg._streams_mtime = 0.0
        scan_streams(cfg)
        assert "new-project" in cfg.streams

    def test_ac4_non_zulip_channel_filtered(self, tmp_path: Path) -> None:
        """Streams with channel != 'zulip' are filtered out."""
        from adapters.zulip_adapter.config import load

        cred_file = tmp_path / "cred.env"
        cred_file.write_text("ZULIP_BOT_API_KEY=test\n")

        streams_dir = tmp_path / "streams"
        # WhatsApp stream should be excluded
        wa_stream = streams_dir / "system3"
        wa_stream.mkdir(parents=True)
        (wa_stream / "stream.toml").write_text(
            'project_path = "/home/user/project"\nchannel = "whatsapp"\n'
        )
        # Zulip stream should be included
        z_stream = streams_dir / "dev"
        z_stream.mkdir(parents=True)
        (z_stream / "stream.toml").write_text(
            'project_path = "/home/user/project"\nchannel = "zulip"\n'
        )

        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text("# empty\n")

        self._write_ccmux_toml(tmp_path, {
            "site": "https://zulip.example.com",
            "bot_email": "bot@example.com",
            "bot_credentials": str(cred_file),
            "streams_dir": str(streams_dir),
            "env_template": str(env_tpl),
        })

        cfg = load(project_root=tmp_path)
        assert "dev" in cfg.streams
        assert "system3" not in cfg.streams


# ============================================================================
# AC-5: process_mgr.py
# ============================================================================


class TestProcessMgr:
    """AC-5: Instance lifecycle manager."""

    def _make_cfg(self, tmp_path: Path) -> "ZulipAdapterConfig":
        from adapters.zulip_adapter.config import ZulipAdapterConfig, StreamConfig

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        streams = tmp_path / "streams"
        streams.mkdir()
        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text(
            "export HTTP_PROXY=http://127.0.0.1:8118\n"
            "export ZULIP_STREAM=${STREAM_NAME}\n"
            "export ZULIP_TOPIC=${TOPIC_NAME}\n"
        )
        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=test\n")

        return ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=streams,
            env_template=env_tpl,
            runtime_dir=runtime,
        )

    def test_ac5_clean_stale_pids(self, tmp_path: Path) -> None:
        """AC-5: clean_stale_pids removes all PID files under runtime_dir."""
        from adapters.zulip_adapter.process_mgr import ProcessManager

        cfg = self._make_cfg(tmp_path)

        # Create some stale PID files
        pid_dir = cfg.runtime_dir / "stream1" / "topic1"
        pid_dir.mkdir(parents=True)
        (pid_dir / "pid").write_text("12345")

        pid_dir2 = cfg.runtime_dir / "stream2" / "topic2"
        pid_dir2.mkdir(parents=True)
        (pid_dir2 / "pid").write_text("67890")

        mgr = ProcessManager(cfg)
        count = mgr.clean_stale_pids()
        assert count == 2
        assert not (pid_dir / "pid").exists()
        assert not (pid_dir2 / "pid").exists()

    def test_ac5_is_alive_no_pid(self, tmp_path: Path) -> None:
        """AC-5: is_alive returns False when no PID file exists."""
        from adapters.zulip_adapter.process_mgr import ProcessManager

        cfg = self._make_cfg(tmp_path)
        mgr = ProcessManager(cfg)
        assert mgr.is_alive("stream1", "topic1") is False

    def test_ac5_is_alive_dead_process(self, tmp_path: Path) -> None:
        """AC-5.3: Stale PID (dead process) returns False."""
        from adapters.zulip_adapter.process_mgr import ProcessManager

        cfg = self._make_cfg(tmp_path)
        pid_dir = cfg.runtime_dir / "stream1" / "topic1"
        pid_dir.mkdir(parents=True)
        # Use a PID that's very unlikely to exist
        (pid_dir / "pid").write_text("999999999")

        mgr = ProcessManager(cfg)
        # Explicitly mock _tmux_has_session to avoid real tmux calls
        # and ensure test doesn't depend on short-circuit evaluation order
        with patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=False):
            assert mgr.is_alive("stream1", "topic1") is False

    def test_ac5_is_alive_running_process(self, tmp_path: Path) -> None:
        """AC-5.3b: Live PID + live tmux session → returns True."""
        from adapters.zulip_adapter.process_mgr import ProcessManager

        cfg = self._make_cfg(tmp_path)
        pid_dir = cfg.runtime_dir / "stream1" / "topic1"
        pid_dir.mkdir(parents=True)
        # Use our own PID (guaranteed alive)
        (pid_dir / "pid").write_text(str(os.getpid()))

        mgr = ProcessManager(cfg)
        with patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=True):
            assert mgr.is_alive("stream1", "topic1") is True

    def test_ac5_4_env_template_parsed(self, tmp_path: Path) -> None:
        """AC-5.4: env_template.sh loaded correctly (non-placeholder vars)."""
        from adapters.zulip_adapter.process_mgr import _parse_env_template

        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text(textwrap.dedent("""\
            # Network proxy
            export HTTP_PROXY=http://127.0.0.1:8118
            export HTTPS_PROXY=http://127.0.0.1:8118
            export NO_PROXY=localhost,127.0.0.1

            # Template variables (should be skipped)
            export ZULIP_STREAM=${STREAM_NAME}
            export ZULIP_TOPIC=${TOPIC_NAME}

            # Static Zulip vars
            export ZULIP_SITE=https://zulip.example.com
        """))

        env = _parse_env_template(env_tpl)
        assert env["HTTP_PROXY"] == "http://127.0.0.1:8118"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:8118"
        assert env["NO_PROXY"] == "localhost,127.0.0.1"
        assert env["ZULIP_SITE"] == "https://zulip.example.com"
        # Template placeholders should be skipped
        assert "ZULIP_STREAM" not in env
        assert "ZULIP_TOPIC" not in env

    def test_ac5_5_tmux_session_name(self) -> None:
        """AC-5.5: tmux session name follows stream--topic format."""
        from adapters.zulip_adapter.process_mgr import _tmux_session_name

        assert _tmux_session_name("ccmux-dev", "fix-auth") == "ccmux-dev--fix-auth"
        assert _tmux_session_name("ipo-analysis", "weekly") == "ipo-analysis--weekly"

    def test_ac5_get_fifo(self, tmp_path: Path) -> None:
        """AC-5: FIFO path follows runtime_dir/stream/topic/in.zulip."""
        from adapters.zulip_adapter.process_mgr import ProcessManager

        cfg = self._make_cfg(tmp_path)
        mgr = ProcessManager(cfg)
        fifo = mgr.get_fifo("ccmux-dev", "fix-auth")
        assert fifo == cfg.runtime_dir / "ccmux-dev" / "fix-auth" / "in.zulip"


# ============================================================================
# AC-6: adapter.py
# ============================================================================


class TestAdapter:
    """AC-6: Inbound Zulip adapter."""

    def _make_adapter(self, tmp_path: Path) -> "ZulipAdapter":
        from adapters.zulip_adapter.adapter import ZulipAdapter
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        streams = tmp_path / "streams"
        streams.mkdir()
        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text("# empty\n")
        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=testkey123\n")

        cfg = ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=streams,
            env_template=env_tpl,
            runtime_dir=runtime,
        )

        return ZulipAdapter(cfg)

    def test_ac6_api_key_loading(self, tmp_path: Path) -> None:
        """AC-6: API key loaded from credentials file."""
        adapter = self._make_adapter(tmp_path)
        assert adapter.api_key == "testkey123"

    def test_ac6_2_unregistered_stream_ignored(self, tmp_path: Path) -> None:
        """AC-6.2: Message in unregistered stream is ignored silently."""
        adapter = self._make_adapter(tmp_path)

        event = {
            "type": "message",
            "message": {
                "type": "stream",
                "display_recipient": "unknown-stream",
                "subject": "topic",
                "content": "hello",
                "sender_email": "user@example.com",
                "sender_full_name": "User",
            },
        }

        with patch.object(adapter.process_mgr, "ensure_instance") as mock_ensure:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(adapter._handle_message(event))
            finally:
                loop.close()
            # Must not attempt to create an instance for unregistered stream
            mock_ensure.assert_not_called()

    def test_ac6_3_bot_echo_ignored(self, tmp_path: Path) -> None:
        """AC-6.3: Bot's own message is ignored."""
        adapter = self._make_adapter(tmp_path)

        event = {
            "type": "message",
            "message": {
                "type": "stream",
                "display_recipient": "test-stream",
                "subject": "topic",
                "content": "hello",
                "sender_email": "bot@example.com",  # Same as bot email
                "sender_full_name": "Bot",
            },
        }

        with patch.object(adapter.process_mgr, "ensure_instance") as mock_ensure:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(adapter._handle_message(event))
            finally:
                loop.close()
            # Must not create instance for bot's own messages
            mock_ensure.assert_not_called()

    def test_ac6_write_to_fifo(self, tmp_path: Path) -> None:
        """AC-6: _write_to_fifo writes message to FIFO."""
        adapter = self._make_adapter(tmp_path)

        fifo_path = tmp_path / "test.fifo"
        os.mkfifo(str(fifo_path))

        # Open FIFO for reading (non-blocking) so write doesn't block
        read_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            result = adapter._write_to_fifo(fifo_path, "test message")
            assert result is True

            data = os.read(read_fd, 4096)
            assert data == b"test message\0"
        finally:
            os.close(read_fd)

    def test_ac6_write_to_fifo_nonexistent(self, tmp_path: Path) -> None:
        """AC-6: FIFO write to nonexistent path returns False."""
        adapter = self._make_adapter(tmp_path)

        fifo_path = tmp_path / "no_such.fifo"
        # Path doesn't exist — open() fails with OSError
        result = adapter._write_to_fifo(fifo_path, "test message")
        assert result is False

    def test_ac6_write_to_fifo_with_sentinel(self, tmp_path: Path) -> None:
        """AC-6: FIFO write succeeds when sentinel fd is open (production path)."""
        adapter = self._make_adapter(tmp_path)

        fifo_path = tmp_path / "test.fifo"
        os.mkfifo(str(fifo_path))

        # Simulate sentinel fd (process_mgr opens this after mkfifo)
        sentinel_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            result = adapter._write_to_fifo(fifo_path, "test message")
            assert result is True

            # Data readable from the sentinel fd
            data = os.read(sentinel_fd, 4096)
            assert data == b"test message\0"
        finally:
            os.close(sentinel_fd)

    def test_ac6_write_to_fifo_multiline_preserved(self, tmp_path: Path) -> None:
        """AC-6: Multi-line messages preserved via NUL-delimited framing."""
        adapter = self._make_adapter(tmp_path)

        fifo_path = tmp_path / "test.fifo"
        os.mkfifo(str(fifo_path))
        sentinel_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)

        try:
            # Multi-line message (code block with newlines)
            msg = "[12:00 zulip] Here is code:\ndef hello():\n    print('hi')"
            result = adapter._write_to_fifo(fifo_path, msg)
            assert result is True

            # Read raw data — NUL delimited, newlines preserved
            raw = os.read(sentinel_fd, 8192)
            assert raw == (msg + "\0").encode("utf-8")

            # Simulate injector splitting on NUL
            frames = raw.split(b"\0")
            assert frames[0].decode("utf-8") == msg
        finally:
            os.close(sentinel_fd)

    def test_ac6_write_to_fifo_backslash_preserved(self, tmp_path: Path) -> None:
        """AC-6: Backslashes in messages preserved via NUL-delimited framing."""
        adapter = self._make_adapter(tmp_path)

        fifo_path = tmp_path / "test.fifo"
        os.mkfifo(str(fifo_path))
        sentinel_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)

        try:
            msg = r"path is C:\Users\test\new_file.txt"
            result = adapter._write_to_fifo(fifo_path, msg)
            assert result is True

            raw = os.read(sentinel_fd, 8192)
            # NUL-delimited, content preserved exactly
            frames = raw.split(b"\0")
            assert frames[0].decode("utf-8") == msg
        finally:
            os.close(sentinel_fd)

    def test_ac6_auth_header_built(self, tmp_path: Path) -> None:
        """AC-6: Auth header correctly built from email + API key."""
        import base64
        adapter = self._make_adapter(tmp_path)

        expected = base64.b64encode(b"bot@example.com:testkey123").decode()
        assert adapter._auth_header == f"Basic {expected}"


# ============================================================================
# AC-6b: zulip_helpers.py
# ============================================================================


class TestZulipHelpers:
    """AC-6b: Zulip API helper CLI."""

    HELPER_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "zulip_helpers.py"

    def test_ac6b_5_invalid_credentials(self, tmp_path: Path, monkeypatch) -> None:
        """AC-6b.5: Invalid credentials → clear error."""
        cred_file = tmp_path / "empty_cred.env"
        cred_file.write_text("# no credentials\n")

        # Clear Zulip env vars to prevent fallback from masking the error
        for key in ("ZULIP_SITE", "ZULIP_BOT_EMAIL", "ZULIP_BOT_API_KEY"):
            monkeypatch.delenv(key, raising=False)

        from scripts.zulip_helpers import _load_credentials

        with pytest.raises(SystemExit):
            _load_credentials(str(cred_file))

    def test_ac6b_6_stdlib_only(self) -> None:
        """AC-6b.6: Script uses only stdlib imports."""
        content = self.HELPER_SCRIPT.read_text()
        import_lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        stdlib_modules = {
            "json", "os", "sys", "urllib.parse", "urllib.request",
            "base64", "urllib", "__future__",
        }
        for line in import_lines:
            parts = line.replace("import ", "").replace("from ", "").split()
            module = parts[0].split(".")[0].rstrip(",")
            assert module in stdlib_modules, f"Non-stdlib import: {line}"

    def test_ac6b_usage_no_args(self) -> None:
        """AC-6b: No args prints usage."""
        result = subprocess.run(
            [sys.executable, str(self.HELPER_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1
        assert "Usage" in result.stderr

    def test_ac6b_unknown_command(self) -> None:
        """AC-6b: Unknown command prints error."""
        result = subprocess.run(
            [sys.executable, str(self.HELPER_SCRIPT), "unknown-cmd"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1
        assert "Unknown command" in result.stderr

    def test_ac6b_create_stream_no_name(self) -> None:
        """AC-6b: create-stream without name prints usage."""
        result = subprocess.run(
            [sys.executable, str(self.HELPER_SCRIPT), "create-stream"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1


# ============================================================================
# AC-8: Non-functional requirements
# ============================================================================


class TestNonFunctional:
    """AC-8: Non-functional requirements."""

    def test_ac8_3_relay_hook_stdlib(self) -> None:
        """AC-8.3: zulip_relay_hook.py uses only stdlib (verified via import check)."""
        hook_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "zulip_relay_hook.py"
        content = hook_path.read_text()

        # Should not contain any third-party imports
        assert "import requests" not in content
        assert "import aiohttp" not in content
        assert "import httpx" not in content

    def test_ac8_4_ccmux_init_idempotent(self, tmp_path: Path) -> None:
        """AC-8.4: Three consecutive ccmux-init runs produce identical state."""
        from scripts import ccmux_init
        import importlib
        mod = importlib.reload(ccmux_init)

        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".git" / "hooks").mkdir(parents=True)

        mod.main([str(project)])
        state1 = {
            "settings": (project / ".claude" / "settings.json").read_text(),
            "claude_md": (project / "CLAUDE.md").read_text(),
            "gitignore": (project / ".gitignore").read_text(),
        }

        mod.main([str(project)])
        state2 = {
            "settings": (project / ".claude" / "settings.json").read_text(),
            "claude_md": (project / "CLAUDE.md").read_text(),
            "gitignore": (project / ".gitignore").read_text(),
        }

        mod.main([str(project)])
        state3 = {
            "settings": (project / ".claude" / "settings.json").read_text(),
            "claude_md": (project / "CLAUDE.md").read_text(),
            "gitignore": (project / ".gitignore").read_text(),
        }

        assert state1 == state2 == state3


# ============================================================================
# Regression tests for review fixes
# ============================================================================


class TestReviewFixes:
    """Tests for bugs found during scenario review."""

    def test_fix1_sentinel_fd_prevents_first_message_drop(self, tmp_path: Path) -> None:
        """Fix 1: Sentinel fd keeps pipe buffer alive so first message is not dropped.

        Production path: ProcessManager opens O_RDONLY sentinel after mkfifo.
        Adapter writes with O_WRONLY (blocking) — succeeds because sentinel is a reader.
        Data survives in kernel buffer until injector reads it.

        Note: Without a sentinel, O_WRONLY blocks forever (no reader), so we only
        test the with-sentinel path. The no-reader case is covered by
        test_ac6_write_to_fifo_nonexistent (OSError path).
        """
        from adapters.zulip_adapter.adapter import ZulipAdapter
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=test\n")
        cfg = ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=tmp_path / "streams",
            env_template=tmp_path / "env.sh",
            runtime_dir=tmp_path / "runtime",
        )
        (tmp_path / "streams").mkdir()
        (tmp_path / "env.sh").write_text("# empty\n")
        (tmp_path / "runtime").mkdir()

        adapter = ZulipAdapter(cfg)

        fifo_path = tmp_path / "test.fifo"
        os.mkfifo(str(fifo_path))

        # Open sentinel (simulates what ProcessManager does after mkfifo)
        sentinel_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        try:
            # With sentinel → write succeeds
            result = adapter._write_to_fifo(fifo_path, "first message")
            assert result is True

            # Data survives in buffer, readable via sentinel
            data = os.read(sentinel_fd, 4096)
            assert data == b"first message\0"
        finally:
            os.close(sentinel_fd)

    def test_fix2_topic_sanitization(self) -> None:
        """Fix 2: Topic names with special chars are sanitized for tmux/filesystem."""
        from adapters.zulip_adapter.process_mgr import _tmux_session_name, _sanitize_name

        # Colons (tmux window separator)
        assert ":" not in _tmux_session_name("dev", "fix: auth bug")
        # Dots (tmux pane separator) — hash suffix appended since chars were replaced
        result = _sanitize_name("v2.0")
        assert "." not in result
        assert result.startswith("v2_0_")
        # Spaces
        assert " " not in _tmux_session_name("dev", "fix auth bug")
        # Parentheses
        assert "(" not in _sanitize_name("test (draft)")
        # Hash
        assert "#" not in _sanitize_name("issue #42")
        # Normal names unchanged (no unsafe chars → no hash suffix)
        assert _sanitize_name("fix-auth-bug") == "fix-auth-bug"
        assert _sanitize_name("ccmux-dev") == "ccmux-dev"
        # Non-ASCII names get unique hashes to avoid collisions
        assert _sanitize_name("测试") != _sanitize_name("你好")
        assert _sanitize_name("测试") != _sanitize_name("会议")

    def test_fix2_runtime_dir_sanitized(self, tmp_path: Path) -> None:
        """Fix 2: Runtime directory paths are sanitized."""
        from adapters.zulip_adapter.process_mgr import ProcessManager
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=test\n")
        cfg = ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=tmp_path / "streams",
            env_template=tmp_path / "env.sh",
            runtime_dir=tmp_path / "runtime",
        )
        (tmp_path / "streams").mkdir()
        (tmp_path / "env.sh").write_text("# empty\n")
        (tmp_path / "runtime").mkdir()

        mgr = ProcessManager(cfg)
        fifo = mgr.get_fifo("ccmux-dev", "fix: auth bug")
        # Path should not contain colons or spaces
        assert ":" not in str(fifo)
        assert " " not in str(fifo)
        assert "fix__auth_bug" in str(fifo)

    def test_fix3_injector_has_pid_file(self) -> None:
        """Fix 3: Injector accepts pid_file parameter for cleanup on exit."""
        from adapters.zulip_adapter.injector import Injector

        injector = Injector("/tmp/test.fifo", "test-session", pid_file="/tmp/test.pid")
        assert injector.pid_file == "/tmp/test.pid"

    def test_fix3_injector_cleans_pid(self, tmp_path: Path) -> None:
        """Fix 3: Injector deletes PID file when it exits."""
        from adapters.zulip_adapter.injector import Injector

        pid_file = tmp_path / "pid"
        pid_file.write_text("12345")
        fifo_path = tmp_path / "test.fifo"
        os.mkfifo(str(fifo_path))

        injector = Injector(str(fifo_path), "nonexistent-session", pid_file=str(pid_file))

        # Mock _tmux_has_session to return False immediately (avoids hang if
        # tmux is running with a matching session name)
        with patch("adapters.zulip_adapter.injector._tmux_has_session", return_value=False):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(injector.run())
            finally:
                loop.close()

        # PID file should be cleaned up
        assert not pid_file.exists()

    def test_fix5_dead_injector_detected(self, tmp_path: Path) -> None:
        """Fix 5: ensure_instance detects dead injector task."""
        from adapters.zulip_adapter.process_mgr import ProcessManager
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=test\n")
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        cfg = ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=tmp_path / "streams",
            env_template=tmp_path / "env.sh",
            runtime_dir=runtime,
        )
        (tmp_path / "streams").mkdir()
        (tmp_path / "env.sh").write_text("# empty\n")

        mgr = ProcessManager(cfg)

        # Simulate a dead injector task
        async def _dead_task():
            pass

        loop = asyncio.new_event_loop()
        task = loop.create_task(_dead_task())
        loop.run_until_complete(task)  # Let it complete

        mgr._injector_tasks["stream/topic"] = task
        assert task.done() is True

        # Create fake alive PID + tmux for is_alive to return True
        pid_dir = runtime / "stream" / "topic"
        pid_dir.mkdir(parents=True)
        (pid_dir / "pid").write_text(str(os.getpid()))  # Our own PID (alive)
        fifo = pid_dir / "in.zulip"
        os.mkfifo(str(fifo))

        # is_alive returns True (PID alive), but injector task is done
        # ensure_instance should detect the dead injector and trigger lazy create
        with patch.object(mgr, "is_alive", return_value=True), \
             patch.object(mgr, "_lazy_create", return_value=(fifo, CreateMode.FIRST_TIME)) as mock_create:
            from adapters.zulip_adapter.config import StreamConfig
            sc = StreamConfig(name="stream", project_path=Path("/tmp"))
            loop.run_until_complete(mgr.ensure_instance("stream", "topic", sc))
            mock_create.assert_called_once()

        loop.close()

    def test_ensure_instance_fallback_pid_missing(self, tmp_path: Path) -> None:
        """Fix: PID file missing but injector task running + tmux alive → no recreate."""
        from adapters.zulip_adapter.process_mgr import ProcessManager
        from adapters.zulip_adapter.config import ZulipAdapterConfig, StreamConfig

        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=test\n")
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        cfg = ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=tmp_path / "streams",
            env_template=tmp_path / "env.sh",
            runtime_dir=runtime,
        )
        mgr = ProcessManager(cfg)

        # Create FIFO (no PID file → is_alive returns False)
        fifo_dir = cfg.runtime_dir / "stream" / "topic"
        fifo_dir.mkdir(parents=True)
        fifo = fifo_dir / "in.zulip"
        os.mkfifo(str(fifo))

        # Simulate a live injector task (not done)
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        mgr._injector_tasks["stream/topic"] = future

        sc = StreamConfig(name="stream", project_path=Path("/tmp"))

        try:
            with patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=True), \
                 patch.object(mgr, "_lazy_create") as mock_create:
                result_fifo, create_mode = loop.run_until_complete(
                    mgr.ensure_instance("stream", "topic", sc)
                )
                # Fallback path: no recreate, NONE mode
                mock_create.assert_not_called()
                assert create_mode == CreateMode.NONE
                assert result_fifo == fifo
        finally:
            future.cancel()
            loop.close()

    def test_fix6_load_api_key_missing_key(self, tmp_path: Path) -> None:
        """Fix 6: _load_api_key raises ValueError when key line is absent."""
        from adapters.zulip_adapter.adapter import _load_api_key

        cred = tmp_path / "no_key.env"
        cred.write_text("ZULIP_SITE=https://example.com\nZULIP_BOT_EMAIL=bot@example.com\n")

        with pytest.raises(ValueError, match="ZULIP_BOT_API_KEY not found"):
            _load_api_key(cred)

    def test_fix12_load_api_key_strips_quotes(self, tmp_path: Path) -> None:
        """Fix 12: _load_api_key strips surrounding quotes from value."""
        from adapters.zulip_adapter.adapter import _load_api_key

        cred = tmp_path / "quoted.env"
        cred.write_text('ZULIP_BOT_API_KEY="myapikey123"\n')
        assert _load_api_key(cred) == "myapikey123"

        cred2 = tmp_path / "single_quoted.env"
        cred2.write_text("ZULIP_BOT_API_KEY='myapikey456'\n")
        assert _load_api_key(cred2) == "myapikey456"

        cred3 = tmp_path / "unquoted.env"
        cred3.write_text("ZULIP_BOT_API_KEY=plainkey789\n")
        assert _load_api_key(cred3) == "plainkey789"

    def test_fix7_env_template_missing_file(self) -> None:
        """Fix 7: _parse_env_template returns empty dict for nonexistent file."""
        from adapters.zulip_adapter.process_mgr import _parse_env_template

        result = _parse_env_template(Path("/nonexistent/env_template.sh"))
        assert result == {}

    def test_fix10_env_template_strips_quotes(self, tmp_path: Path) -> None:
        """Fix 10: _parse_env_template strips surrounding quotes from values."""
        from adapters.zulip_adapter.process_mgr import _parse_env_template

        env_file = tmp_path / "env_template.sh"
        env_file.write_text(
            'export ZULIP_SITE="https://zulip.example.com"\n'
            "export HTTP_PROXY='http://127.0.0.1:8118'\n"
            "export PLAIN_VALUE=no_quotes\n"
        )
        result = _parse_env_template(env_file)
        assert result["ZULIP_SITE"] == "https://zulip.example.com"
        assert result["HTTP_PROXY"] == "http://127.0.0.1:8118"
        assert result["PLAIN_VALUE"] == "no_quotes"

    def test_fix_env_template_expands_tilde(self, tmp_path: Path) -> None:
        """Tilde paths in env_template.sh are expanded to absolute paths."""
        from adapters.zulip_adapter.process_mgr import _parse_env_template

        env_file = tmp_path / "env_template.sh"
        env_file.write_text(
            "export ZULIP_BOT_API_KEY_FILE=~/.ccmux/secrets/zulip_bot.env\n"
            "export HTTP_PROXY=http://127.0.0.1:8118\n"
        )
        result = _parse_env_template(env_file)
        # Tilde must be expanded — must not start with ~
        assert not result["ZULIP_BOT_API_KEY_FILE"].startswith("~")
        assert result["ZULIP_BOT_API_KEY_FILE"].endswith("/.ccmux/secrets/zulip_bot.env")
        # Non-tilde values are unchanged
        assert result["HTTP_PROXY"] == "http://127.0.0.1:8118"

    def test_fix11_old_injector_pid_cleared_on_recreate(self, tmp_path: Path) -> None:
        """Fix 11: Old injector's pid_file cleared before cancel to prevent race.

        When _lazy_create replaces an existing injector, it must set the old
        injector's pid_file to None before cancelling, so the old injector's
        finally block doesn't delete the new PID file.
        """
        from adapters.zulip_adapter.injector import Injector
        from adapters.zulip_adapter.process_mgr import ProcessManager
        from adapters.zulip_adapter.config import ZulipAdapterConfig, StreamConfig

        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=test\n")
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        cfg = ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=tmp_path / "streams",
            env_template=tmp_path / "env.sh",
            runtime_dir=runtime,
        )
        (tmp_path / "streams").mkdir()
        (tmp_path / "env.sh").write_text("# empty\n")
        mgr = ProcessManager(cfg)

        # Set up an existing injector with a pid_file
        old_injector = Injector("/tmp/old.fifo", "old-session", pid_file="/tmp/old.pid")
        mgr._injectors["stream/topic"] = old_injector

        # Create a dummy completed task so _lazy_create runs
        loop = asyncio.new_event_loop()
        async def _noop():
            pass
        old_task = loop.create_task(_noop())
        loop.run_until_complete(old_task)
        mgr._injector_tasks["stream/topic"] = old_task

        sc = StreamConfig(name="test", project_path=tmp_path)

        # Mock tmux to succeed so _lazy_create reaches the injector replacement code
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            # display-message uses text=True, returns str; others return bytes
            if kwargs.get("text"):
                result.stdout = "12345"
            else:
                result.stdout = b"12345"
            result.stderr = b""
            return result

        try:
            with patch("subprocess.run", side_effect=mock_run), \
                 patch("adapters.zulip_adapter.process_mgr.CCMUX_INIT_SCRIPT",
                       tmp_path / "nonexistent"):
                loop.run_until_complete(mgr._lazy_create("stream", "topic", sc))

            # The old injector's pid_file should have been cleared by _lazy_create
            assert old_injector.pid_file is None
        finally:
            mgr.stop_all()
            loop.close()

    def test_fix8_bad_event_queue_id_uses_code_field(self, tmp_path: Path) -> None:
        """Fix 8: _get_events detects BAD_EVENT_QUEUE_ID from code field."""
        from adapters.zulip_adapter.adapter import ZulipAdapter

        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=testkey123\n")
        cfg = MagicMock()
        cfg.site = "https://zulip.example.com"
        cfg.bot_email = "bot@example.com"
        cfg.bot_credentials = cred

        adapter = ZulipAdapter.__new__(ZulipAdapter)
        adapter.cfg = cfg
        adapter.api_key = "testkey123"
        adapter._auth_header = adapter._build_auth()
        adapter.process_mgr = MagicMock()

        # Simulate BAD_EVENT_QUEUE_ID response (code field, not msg field)
        bad_queue_response = {
            "result": "error",
            "code": "BAD_EVENT_QUEUE_ID",
            "msg": "Bad event queue ID: 1234567890:12345",
        }
        with patch.object(adapter, "_api_call", return_value=bad_queue_response):
            with pytest.raises(ConnectionError, match="Event queue expired"):
                adapter._get_events("test-queue", 0)

    def test_fix9_instance_dir_sanitized(self, tmp_path: Path) -> None:
        """Fix 9: instance_dir path uses sanitized names to prevent traversal."""
        from adapters.zulip_adapter.process_mgr import (
            ProcessManager, _sanitize_name, _runtime_dir, _fifo_path,
        )
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        streams_dir = tmp_path / "streams"
        streams_dir.mkdir()
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=test\n")
        env_tpl = tmp_path / "env.sh"
        env_tpl.write_text("# empty\n")

        cfg = ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=streams_dir,
            env_template=env_tpl,
            runtime_dir=runtime,
        )

        # Malicious topic with path traversal chars
        malicious_topic = "../../etc/passwd"
        sanitized = _sanitize_name(malicious_topic)

        # Verify sanitization removes path traversal
        assert ".." not in sanitized
        assert "/" not in sanitized

        # Verify actual production functions produce safe paths
        rt = _runtime_dir(cfg, "test-stream", malicious_topic)
        assert str(rt).startswith(str(runtime))
        assert ".." not in str(rt)

        fifo = _fifo_path(cfg, "test-stream", malicious_topic)
        assert str(fifo).startswith(str(runtime))
        assert ".." not in str(fifo)


# ============================================================================
# Event queue staleness watchdog
# ============================================================================


class TestStaleQueueWatchdog:
    """Verify the staleness watchdog forces re-registration."""

    def _make_adapter(self, tmp_path: Path) -> "ZulipAdapter":
        from adapters.zulip_adapter.adapter import ZulipAdapter
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        runtime = tmp_path / "runtime"
        runtime.mkdir()
        streams = tmp_path / "streams"
        streams.mkdir()
        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text("# empty\n")
        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=testkey123\n")

        cfg = ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=streams,
            env_template=env_tpl,
            runtime_dir=runtime,
        )
        return ZulipAdapter(cfg)

    def test_stale_queue_triggers_reregistration(self, tmp_path: Path) -> None:
        """When no events arrive beyond STALE_QUEUE_TIMEOUT, run() re-registers."""
        import adapters.zulip_adapter.adapter as adapter_mod

        adapter = self._make_adapter(tmp_path)

        register_count = 0
        original_stale_timeout = adapter_mod.STALE_QUEUE_TIMEOUT

        def fake_register():
            nonlocal register_count
            register_count += 1
            if register_count >= 3:
                adapter._running = False
            return (f"queue-{register_count}", -1)

        # Return empty events (simulating a stale queue with no heartbeats)
        def fake_get_events(queue_id, last_event_id):
            return []

        # Use a tiny stale timeout so the test runs fast
        adapter_mod.STALE_QUEUE_TIMEOUT = 0.0

        try:
            with patch.object(adapter, "_register_event_queue", side_effect=fake_register):
                with patch.object(adapter, "_get_events", side_effect=fake_get_events):
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(adapter.run())
                    finally:
                        loop.close()

            # Should have registered at least 2 times (initial + re-register after stale)
            assert register_count >= 2, (
                f"Expected at least 2 registrations due to staleness, got {register_count}"
            )
        finally:
            adapter_mod.STALE_QUEUE_TIMEOUT = original_stale_timeout

    def test_events_reset_staleness_timer(self, tmp_path: Path) -> None:
        """When events arrive, the staleness timer is reset (no re-registration)."""
        import adapters.zulip_adapter.adapter as adapter_mod

        adapter = self._make_adapter(tmp_path)

        register_count = 0
        poll_count = 0

        def fake_register():
            nonlocal register_count
            register_count += 1
            return ("queue-1", -1)

        # Use a fake monotonic clock to control time precisely.
        # Each poll advances 50 s, but events arrive so the timer resets.
        # With threshold at 200 s the queue stays fresh as long as events come.
        fake_clock = [0.0]

        def fake_get_events(queue_id, last_event_id):
            nonlocal poll_count
            poll_count += 1
            fake_clock[0] += 50.0  # 50 s per poll, well under 200 s threshold
            if poll_count >= 5:
                adapter._running = False
                return []
            # Return a heartbeat event each time — this resets the staleness timer
            return [{"id": poll_count, "type": "heartbeat"}]

        original_timeout = adapter_mod.STALE_QUEUE_TIMEOUT
        adapter_mod.STALE_QUEUE_TIMEOUT = 200.0

        try:
            with patch.object(adapter, "_register_event_queue", side_effect=fake_register):
                with patch.object(adapter, "_get_events", side_effect=fake_get_events):
                    with patch("adapters.zulip_adapter.adapter.time") as mock_time:
                        mock_time.monotonic = lambda: fake_clock[0]
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(adapter.run())
                        finally:
                            loop.close()

            # Only 1 registration — heartbeats kept the queue alive
            assert register_count == 1, (
                f"Expected exactly 1 registration (heartbeats reset timer), got {register_count}"
            )
        finally:
            adapter_mod.STALE_QUEUE_TIMEOUT = original_timeout

    def test_stale_queue_log_message(self, tmp_path: Path) -> None:
        """Staleness watchdog logs a warning before re-registering."""
        import adapters.zulip_adapter.adapter as adapter_mod

        adapter = self._make_adapter(tmp_path)
        register_count = 0

        def fake_register():
            nonlocal register_count
            register_count += 1
            if register_count >= 2:
                adapter._running = False
            return (f"queue-{register_count}", -1)

        def fake_get_events(queue_id, last_event_id):
            return []

        adapter_mod.STALE_QUEUE_TIMEOUT = 0.0

        try:
            with patch.object(adapter, "_register_event_queue", side_effect=fake_register):
                with patch.object(adapter, "_get_events", side_effect=fake_get_events):
                    with patch.object(adapter_mod.log, "warning") as mock_warn:
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(adapter.run())
                        finally:
                            loop.close()

                        # Check that at least one warning about staleness was logged
                        stale_warnings = [
                            c for c in mock_warn.call_args_list
                            if "stale" in str(c).lower()
                        ]
                        assert len(stale_warnings) >= 1, (
                            f"Expected staleness warning, got: {mock_warn.call_args_list}"
                        )
        finally:
            adapter_mod.STALE_QUEUE_TIMEOUT = adapter_mod.POLL_TIMEOUT * 3


# ============================================================================
# Closed-loop scenario tests
#
# These tests use REAL files, FIFOs, and config loading where possible.
# Only tmux and Zulip API calls are mocked (external systems).
# ============================================================================


class TestClosedLoopScenarios:
    """Scenario tests with real files/FIFOs. Only tmux/API are mocked."""

    def _make_env(self, tmp_path: Path) -> tuple:
        """Create a realistic file environment: ccmux.toml, streams, credentials.

        Returns (project_root, adapter).
        """
        from adapters.zulip_adapter.adapter import ZulipAdapter
        from adapters.zulip_adapter.config import load

        # Credentials file
        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=testkey123\n")

        # Streams directory with a real stream.toml
        streams = tmp_path / "streams"
        stream_dir = streams / "dev-project"
        stream_dir.mkdir(parents=True)
        (stream_dir / "stream.toml").write_text(
            f'project_path = "{tmp_path}"\nchannel = "zulip"\n'
        )

        # Env template
        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text(
            "export HTTP_PROXY=http://127.0.0.1:8118\n"
            "export ZULIP_STREAM=${STREAM_NAME}\n"
        )

        # ccmux.toml — runtime_dir scoped to tmp_path for test isolation
        runtime = tmp_path / "runtime"
        (tmp_path / "ccmux.toml").write_text(
            f'[zulip]\n'
            f'site = "https://zulip.example.com"\n'
            f'bot_email = "bot@example.com"\n'
            f'bot_credentials = "{cred}"\n'
            f'streams_dir = "{streams}"\n'
            f'env_template = "{env_tpl}"\n'
            f'\n[runtime]\n'
            f'dir = "{runtime}"\n'
        )

        cfg = load(project_root=tmp_path)
        adapter = ZulipAdapter(cfg)
        return tmp_path, adapter

    def _zulip_event(
        self, stream: str, topic: str, content: str,
        sender: str = "user@example.com",
    ) -> dict:
        """Build a Zulip message event dict matching the real API format."""
        return {
            "type": "message",
            "message": {
                "type": "stream",
                "display_recipient": stream,
                "subject": topic,
                "content": content,
                "sender_email": sender,
                "sender_full_name": "User",
            },
        }

    def test_config_to_fifo_write_real_files(self, tmp_path: Path) -> None:
        """Scenario 2: Load config from real files, create real FIFO, write data.

        Full chain: ccmux.toml → config.load() → ZulipAdapter → ProcessManager
        → create FIFO + sentinel → _write_to_fifo → data readable from pipe.

        Only tmux is mocked (can't create real tmux sessions in CI).
        """
        root, adapter = self._make_env(tmp_path)

        # Create FIFO and sentinel (simulating what _lazy_create does)
        runtime = adapter.cfg.runtime_dir / "dev-project" / "test-topic"
        runtime.mkdir(parents=True)
        fifo = runtime / "in.zulip"
        os.mkfifo(str(fifo))
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        try:
            # Adapter writes to the real FIFO
            result = adapter._write_to_fifo(fifo, "[12:34 zulip] fix the auth bug")
            assert result is True

            # Data is readable from sentinel fd (this is what injector would read)
            data = os.read(sentinel_fd, 4096)
            assert data == b"[12:34 zulip] fix the auth bug\0"
        finally:
            os.close(sentinel_fd)

    def test_message_routing_end_to_end(self, tmp_path: Path) -> None:
        """Scenario 2: Full _handle_message with real config, real FIFO.

        Message arrives → config loaded from files → stream matched →
        ensure_instance (mocked to return real FIFO) → data written to real FIFO.
        """
        root, adapter = self._make_env(tmp_path)

        # Pre-create FIFO with sentinel (simulating running instance)
        runtime = adapter.cfg.runtime_dir / "dev-project" / "fix-auth"
        runtime.mkdir(parents=True)
        fifo = runtime / "in.zulip"
        os.mkfifo(str(fifo))
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        # Write a PID file so is_alive can check it
        pid_file = runtime / "pid"
        pid_file.write_text(str(os.getpid()))

        event = self._zulip_event("dev-project", "fix-auth", "fix the auth bug")

        try:
            # Mock only: tmux has-session (true) and scan_streams (skip mtime check)
            with patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=True), \
                 patch.object(adapter.process_mgr, "ensure_instance", return_value=(fifo, CreateMode.NONE)), \
                 patch("adapters.zulip_adapter.adapter.scan_streams"):

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(adapter._handle_message(event))
                finally:
                    loop.close()

            # Read from real FIFO — data should be there
            data = os.read(sentinel_fd, 4096)
            decoded = data.decode("utf-8")
            assert "fix the auth bug" in decoded
            # Format: [yy/mm/dd hh:mm From zulip] content
            import re
            assert re.search(r"\[\d{2}/\d{2}/\d{2} \d{2}:\d{2} From zulip\]", decoded)
        finally:
            os.close(sentinel_fd)

    def test_first_message_session_started_notification(self, tmp_path: Path) -> None:
        """Scenario 2: First message to new topic → 'Session started' posted to Zulip.

        is_alive() returns False → _post_message called with session notification.
        """
        root, adapter = self._make_env(tmp_path)

        # Pre-create FIFO for the write to succeed
        runtime = adapter.cfg.runtime_dir / "dev-project" / "new-topic"
        runtime.mkdir(parents=True)
        fifo = runtime / "in.zulip"
        os.mkfifo(str(fifo))
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        event = self._zulip_event("dev-project", "new-topic", "hello world")

        try:
            with patch.object(adapter.process_mgr, "ensure_instance", return_value=(fifo, CreateMode.FIRST_TIME)), \
                 patch.object(adapter, "_post_message") as mock_post, \
                 patch("adapters.zulip_adapter.adapter.scan_streams"):

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(adapter._handle_message(event))
                finally:
                    loop.close()

            # Session started notification sent to correct stream+topic
            mock_post.assert_called_once()
            args = mock_post.call_args[0]
            assert args[0] == "dev-project"
            assert args[1] == "new-topic"
            assert "Session started" in args[2]

            # Message also written to FIFO
            data = os.read(sentinel_fd, 4096)
            assert b"hello world" in data
        finally:
            os.close(sentinel_fd)

    def test_warm_path_no_notification(self, tmp_path: Path) -> None:
        """Scenario 3: Message to running instance → no 'Session started' notification."""
        root, adapter = self._make_env(tmp_path)

        runtime = adapter.cfg.runtime_dir / "dev-project" / "active-topic"
        runtime.mkdir(parents=True)
        fifo = runtime / "in.zulip"
        os.mkfifo(str(fifo))
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        event = self._zulip_event("dev-project", "active-topic", "follow-up")

        try:
            with patch.object(adapter.process_mgr, "ensure_instance", return_value=(fifo, CreateMode.NONE)), \
                 patch.object(adapter, "_post_message") as mock_post, \
                 patch("adapters.zulip_adapter.adapter.scan_streams"):

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(adapter._handle_message(event))
                finally:
                    loop.close()

            # NO session notification
            mock_post.assert_not_called()
            # But message still written
            data = os.read(sentinel_fd, 4096)
            assert b"follow-up" in data
        finally:
            os.close(sentinel_fd)

    def test_two_streams_route_independently(self, tmp_path: Path) -> None:
        """Scenario 6/7: Two registered streams route to separate FIFOs."""
        from adapters.zulip_adapter.adapter import ZulipAdapter
        from adapters.zulip_adapter.config import load

        # Create credentials
        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=testkey123\n")

        # Two streams
        streams = tmp_path / "streams"
        for name in ["project-alpha", "project-beta"]:
            d = streams / name
            d.mkdir(parents=True)
            (d / "stream.toml").write_text(
                f'project_path = "{tmp_path}"\nchannel = "zulip"\n'
            )

        env_tpl = tmp_path / "env_template.sh"
        env_tpl.write_text("# empty\n")
        runtime = tmp_path / "runtime"
        (tmp_path / "ccmux.toml").write_text(
            f'[zulip]\n'
            f'site = "https://zulip.example.com"\n'
            f'bot_email = "bot@example.com"\n'
            f'bot_credentials = "{cred}"\n'
            f'streams_dir = "{streams}"\n'
            f'env_template = "{env_tpl}"\n'
            f'\n[runtime]\n'
            f'dir = "{runtime}"\n'
        )

        cfg = load(project_root=tmp_path)
        assert "project-alpha" in cfg.streams
        assert "project-beta" in cfg.streams
        adapter = ZulipAdapter(cfg)

        # Create separate FIFOs for each stream
        fifos = {}
        sentinels = {}
        for name in ["project-alpha", "project-beta"]:
            runtime = cfg.runtime_dir / name / "topic"
            runtime.mkdir(parents=True)
            fifo = runtime / "in.zulip"
            os.mkfifo(str(fifo))
            sentinels[name] = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
            fifos[name] = fifo

        try:
            for stream_name, msg_text in [
                ("project-alpha", "alpha message"),
                ("project-beta", "beta message"),
            ]:
                event = self._zulip_event(stream_name, "topic", msg_text)
                with patch.object(adapter.process_mgr, "ensure_instance",
                                  return_value=(fifos[stream_name], CreateMode.NONE)), \
                     patch("adapters.zulip_adapter.adapter.scan_streams"):

                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(adapter._handle_message(event))
                    finally:
                        loop.close()

            # Verify data arrived in correct FIFOs
            alpha_data = os.read(sentinels["project-alpha"], 4096).decode()
            beta_data = os.read(sentinels["project-beta"], 4096).decode()
            assert "alpha message" in alpha_data
            assert "beta message" in beta_data
            # No cross-contamination
            assert "beta" not in alpha_data
            assert "alpha" not in beta_data
        finally:
            for fd in sentinels.values():
                os.close(fd)

    def test_unregistered_stream_silently_ignored(self, tmp_path: Path) -> None:
        """Scenario 7: Message to unregistered stream is silently dropped."""
        root, adapter = self._make_env(tmp_path)

        event = self._zulip_event("unknown-stream", "topic", "hello")

        with patch("adapters.zulip_adapter.adapter.scan_streams"):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(adapter._handle_message(event))
            finally:
                loop.close()
        # No crash, no instance created

    def test_bot_echo_prevention(self, tmp_path: Path) -> None:
        """Bot's own messages are ignored (echo loop prevention)."""
        root, adapter = self._make_env(tmp_path)

        event = self._zulip_event(
            "dev-project", "topic", "I am the bot",
            sender="bot@example.com",  # Same as bot_email in config
        )

        with patch.object(adapter.process_mgr, "ensure_instance") as mock_ensure:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(adapter._handle_message(event))
            finally:
                loop.close()
            mock_ensure.assert_not_called()

    def test_private_message_ignored(self, tmp_path: Path) -> None:
        """Private/DM messages are ignored (only stream messages handled)."""
        root, adapter = self._make_env(tmp_path)

        event = {
            "type": "message",
            "message": {
                "type": "private",
                "content": "hello bot",
                "sender_email": "user@example.com",
                "sender_full_name": "User",
            },
        }

        with patch.object(adapter.process_mgr, "ensure_instance") as mock_ensure:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(adapter._handle_message(event))
            finally:
                loop.close()
            mock_ensure.assert_not_called()

    def test_empty_content_ignored(self, tmp_path: Path) -> None:
        """Message with empty content is ignored."""
        root, adapter = self._make_env(tmp_path)

        event = self._zulip_event("dev-project", "topic", "")

        with patch.object(adapter.process_mgr, "ensure_instance") as mock_ensure, \
             patch("adapters.zulip_adapter.adapter.scan_streams"):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(adapter._handle_message(event))
            finally:
                loop.close()
            mock_ensure.assert_not_called()

    def test_special_chars_in_topic_real_fifo(self, tmp_path: Path) -> None:
        """Scenario 8: Topic with special chars → sanitized path → real FIFO works."""
        from adapters.zulip_adapter.process_mgr import ProcessManager, _sanitize_name

        root, adapter = self._make_env(tmp_path)

        # Topic with colons, spaces, parens — common in Zulip
        topic = "fix: auth bug (draft)"
        sanitized = _sanitize_name(topic)
        assert ":" not in sanitized
        assert " " not in sanitized
        assert "(" not in sanitized

        # Create FIFO at the sanitized path
        runtime = adapter.cfg.runtime_dir / "dev-project" / sanitized
        runtime.mkdir(parents=True)
        fifo = runtime / "in.zulip"
        os.mkfifo(str(fifo))
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        try:
            # Write to the FIFO
            result = adapter._write_to_fifo(fifo, "[12:00 zulip] test message")
            assert result is True
            data = os.read(sentinel_fd, 4096)
            assert b"test message" in data
        finally:
            os.close(sentinel_fd)

    def test_sentinel_fd_lifecycle(self, tmp_path: Path) -> None:
        """Full sentinel fd lifecycle: create → write → read → stop_all → closed."""
        from adapters.zulip_adapter.process_mgr import ProcessManager
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        root, adapter = self._make_env(tmp_path)
        mgr = adapter.process_mgr

        # Create FIFO and sentinel (what _lazy_create does)
        fifo_path = tmp_path / "lifecycle.fifo"
        os.mkfifo(str(fifo_path))
        sentinel_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        mgr._sentinel_fds["test/topic"] = sentinel_fd

        # Write succeeds (sentinel keeps pipe alive)
        assert adapter._write_to_fifo(fifo_path, "hello") is True

        # Data readable
        data = os.read(sentinel_fd, 4096)
        assert data == b"hello\0"

        # stop_all closes sentinel
        mgr.stop_all()
        assert len(mgr._sentinel_fds) == 0
        with pytest.raises(OSError):
            os.fstat(sentinel_fd)

    def test_apply_markdown_false_in_registration(self, tmp_path: Path) -> None:
        """Fix 4: Event queue registration sends apply_markdown=false."""
        root, adapter = self._make_env(tmp_path)

        with patch.object(adapter, "_api_call", return_value={
            "result": "success",
            "queue_id": "test-queue",
            "last_event_id": -1,
        }) as mock_api:
            adapter._register_event_queue()
            data = mock_api.call_args[0][2]  # 3rd positional arg
            assert data["apply_markdown"] == "false"

    def test_stop_cascades_to_process_manager(self, tmp_path: Path) -> None:
        """Scenario 4: adapter.stop() → process_mgr.stop_all()."""
        root, adapter = self._make_env(tmp_path)

        with patch.object(adapter.process_mgr, "stop_all") as mock_stop:
            adapter.stop()
            assert adapter._running is False
            mock_stop.assert_called_once()

    def test_hot_reload_detects_new_stream(self, tmp_path: Path) -> None:
        """Scenario: New stream.toml added while adapter is running."""
        from adapters.zulip_adapter.config import scan_streams

        root, adapter = self._make_env(tmp_path)
        cfg = adapter.cfg

        # Initially: only "dev-project"
        assert "dev-project" in cfg.streams
        assert "new-project" not in cfg.streams

        # Add a new stream
        new_dir = cfg.streams_dir / "new-project"
        new_dir.mkdir()
        (new_dir / "stream.toml").write_text(
            f'project_path = "{tmp_path}"\nchannel = "zulip"\n'
        )

        # Force mtime invalidation and rescan
        cfg._streams_mtime = 0.0
        scan_streams(cfg)

        assert "new-project" in cfg.streams

    def test_tmux_new_session_failure_detected(self, tmp_path: Path) -> None:
        """Bug #10: tmux new-session failure → returns None, no injector started."""
        from adapters.zulip_adapter.config import StreamConfig

        root, adapter = self._make_env(tmp_path)
        mgr = adapter.process_mgr

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[0] == "tmux" and "new-session" in cmd:
                result.returncode = 1
                result.stderr = b"duplicate session"
            elif cmd[0] == "tmux" and "has-session" in cmd:
                result.returncode = 1  # No existing session
            else:
                result.returncode = 0
            result.stdout = b""
            return result

        sc = StreamConfig(name="test", project_path=tmp_path)

        with patch("subprocess.run", side_effect=mock_run), \
             patch("adapters.zulip_adapter.process_mgr.CCMUX_INIT_SCRIPT",
                   tmp_path / "nonexistent"):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(mgr._lazy_create("stream", "topic", sc))
                # tmux failed → returns None
                assert result is None
                # No injector task should be started (tmux failed)
                assert "stream/topic" not in mgr._injector_tasks
            finally:
                # Clean up sentinel fd
                mgr.stop_all()
                loop.close()

    def test_post_message_logs_api_error(self, tmp_path: Path) -> None:
        """Bug #20: _post_message logs errors without raising."""
        root, adapter = self._make_env(tmp_path)

        with patch.object(adapter, "_api_call", return_value={
            "result": "error", "msg": "Stream not found",
        }):
            # Should not raise
            adapter._post_message("nonexistent", "topic", "hello")

    def test_message_split_at_9500_chars(self) -> None:
        """AC-2.5: Relay hook splits messages at 9500 char boundary."""
        import re

        # Verify the actual source code uses 9500 as chunk size
        hook_script = Path(__file__).resolve().parent.parent.parent / "scripts" / "zulip_relay_hook.py"
        source = hook_script.read_text()
        match = re.search(r"i \+ (\d+)", source)
        assert match, "Chunk size pattern not found in relay hook source"
        chunk_size = int(match.group(1))
        assert chunk_size == 9500, f"Expected chunk size 9500, got {chunk_size}"

        # Verify splitting math with the source-derived constant
        content = "X" * 20000
        chunks = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]
        assert len(chunks) == 3
        assert len(chunks[0]) == 9500
        assert len(chunks[1]) == 9500
        assert len(chunks[2]) == 1000
        assert "".join(chunks) == content

    def test_injector_batches_queued_messages(self) -> None:
        """AC-3.4: Multiple queued messages joined with separator for injection.

        Drives through actual Injector.run() to verify the real batching logic.
        """
        from adapters.zulip_adapter import injector as inj_mod
        from adapters.zulip_adapter.injector import Injector

        injector = Injector("/tmp/test.fifo", "test-session")
        # Pre-load queue with multiple messages
        injector._queue = ["msg1", "msg2", "msg3"]

        def inject_and_stop(session, text):
            """Capture the inject call and stop the loop."""
            injector._running = False
            return True

        # Drive through actual run() with mocks to control the loop
        with patch.object(inj_mod, "_inject_text", side_effect=inject_and_stop) as mock_inject, \
             patch.object(inj_mod, "_tmux_has_session", return_value=True), \
             patch.object(injector.gate, "is_ready", return_value=True), \
             patch.object(injector.gate, "is_claude_dead", return_value=False), \
             patch("os.open", return_value=99), \
             patch("os.read", side_effect=BlockingIOError), \
             patch("os.close"):

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(injector.run())
            finally:
                loop.close()

        # Verify _inject_text called by run() with all messages joined by separator
        mock_inject.assert_called_once_with("test-session", "msg1\n---\nmsg2\n---\nmsg3")
        # Queue should be cleared after successful injection
        assert injector._queue == []

    def test_existing_code_unchanged(self) -> None:
        """AC-8.1: No modifications to ccmux/ or adapters/wa_notifier/."""
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", "ccmux/", "adapters/wa_notifier/"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            timeout=10,
        )
        changed = [f for f in result.stdout.strip().split("\n") if f]
        assert changed == [], f"Existing code modified: {changed}"

    def test_multi_topic_same_stream_isolation(self) -> None:
        """Scenario 6: Same stream, different topics → different paths and sessions."""
        from adapters.zulip_adapter.process_mgr import (
            _tmux_session_name, _runtime_dir, _fifo_path,
        )
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        cfg = MagicMock()
        cfg.runtime_dir = Path("/tmp/ccmux")

        # Two topics in same stream
        fifo_a = _fifo_path(cfg, "ccmux-dev", "fix-auth")
        fifo_b = _fifo_path(cfg, "ccmux-dev", "add-tests")
        assert fifo_a != fifo_b

        session_a = _tmux_session_name("ccmux-dev", "fix-auth")
        session_b = _tmux_session_name("ccmux-dev", "add-tests")
        assert session_a != session_b
        assert session_a == "ccmux-dev--fix-auth"
        assert session_b == "ccmux-dev--add-tests"


# ============================================================================
# AC-9: File handler (adapters/zulip_adapter/file_handler.py)
# ============================================================================


class TestFileHandler:
    """AC-9: file_handler.py pure functions."""

    def _import(self):
        from adapters.zulip_adapter import file_handler
        return file_handler

    # --- extract_attachments ---

    def test_ac9_1_extract_single_attachment(self) -> None:
        """AC-9.1: Single file attachment parsed correctly."""
        fh = self._import()
        content = "Here is the file [report.pdf](/user_uploads/1/ab/report.pdf)"
        result = fh.extract_attachments(content)
        assert result == [("report.pdf", "/user_uploads/1/ab/report.pdf")]

    def test_ac9_2_extract_multiple_attachments(self) -> None:
        """AC-9.2: Multiple attachments in one message."""
        fh = self._import()
        content = (
            "Files: [a.txt](/user_uploads/1/a.txt) "
            "and [b.png](/user_uploads/2/b.png)"
        )
        result = fh.extract_attachments(content)
        assert len(result) == 2
        assert result[0] == ("a.txt", "/user_uploads/1/a.txt")
        assert result[1] == ("b.png", "/user_uploads/2/b.png")

    def test_ac9_3_extract_no_attachments(self) -> None:
        """AC-9.3: Plain text message has no attachments."""
        fh = self._import()
        result = fh.extract_attachments("Just a normal message")
        assert result == []

    def test_ac9_4_extract_inline_image(self) -> None:
        """AC-9.4: Inline image ![name](/user_uploads/...) detected."""
        fh = self._import()
        content = "Look at this ![screenshot.png](/user_uploads/1/ss.png)"
        result = fh.extract_attachments(content)
        assert result == [("screenshot.png", "/user_uploads/1/ss.png")]

    def test_ac9_4b_mixed_text_and_attachments(self) -> None:
        """AC-9.4b: Message with text and attachment, text preserved."""
        fh = self._import()
        content = "Check this out [doc.pdf](/user_uploads/1/doc.pdf) thanks"
        result = fh.extract_attachments(content)
        assert len(result) == 1
        stripped = fh.strip_attachment_links(content)
        assert "doc.pdf" in stripped
        assert "/user_uploads/" not in stripped

    # --- safe_resolve ---

    def test_ac9_5_safe_resolve_normal(self, tmp_path: Path) -> None:
        """AC-9.5: Normal relative path resolves correctly."""
        fh = self._import()
        result = fh.safe_resolve(tmp_path, "subdir/file.txt")
        assert result is not None
        assert str(result).startswith(str(tmp_path.resolve()))

    def test_ac9_6_safe_resolve_traversal(self, tmp_path: Path) -> None:
        """AC-9.6: ../../../etc/passwd traversal blocked."""
        fh = self._import()
        result = fh.safe_resolve(tmp_path, "../../../etc/passwd")
        assert result is None

    def test_ac9_7_safe_resolve_absolute(self, tmp_path: Path) -> None:
        """AC-9.7: Absolute path rejected."""
        fh = self._import()
        result = fh.safe_resolve(tmp_path, "/etc/passwd")
        assert result is None

    def test_ac9_8_safe_resolve_base_equals_path(self, tmp_path: Path) -> None:
        """AC-9.8: Path that resolves to base itself is allowed."""
        fh = self._import()
        result = fh.safe_resolve(tmp_path, ".")
        assert result is not None
        assert result == tmp_path.resolve()

    # --- sanitize_filename ---

    def test_ac9_9_sanitize_separators(self) -> None:
        """AC-9.9: Path separators stripped from filename."""
        fh = self._import()
        assert "/" not in fh.sanitize_filename("path/to/file.txt")
        assert "\\" not in fh.sanitize_filename("path\\to\\file.txt")

    def test_ac9_10_sanitize_special_chars(self) -> None:
        """AC-9.10: Special characters removed."""
        fh = self._import()
        result = fh.sanitize_filename('file<>:"|?*.txt')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert '"' not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result

    def test_ac9_11_sanitize_leading_dots(self) -> None:
        """AC-9.11: Leading dots removed to prevent hidden files."""
        fh = self._import()
        assert not fh.sanitize_filename("..hidden").startswith(".")
        assert not fh.sanitize_filename(".bashrc").startswith(".")

    def test_ac9_12_sanitize_empty(self) -> None:
        """AC-9.12: Empty filename becomes 'unnamed'."""
        fh = self._import()
        assert fh.sanitize_filename("") == "unnamed"
        assert fh.sanitize_filename("///") == "unnamed"
        assert fh.sanitize_filename("...") == "unnamed"

    # --- download_file ---

    def test_ac9_13_download_success(self, tmp_path: Path) -> None:
        """AC-9.13: Successful download writes file to dest."""
        fh = self._import()
        dest = tmp_path / "downloads" / "file.txt"
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"file content"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        opener = MagicMock()
        opener.open.return_value = mock_resp

        ok = fh.download_file(
            opener, "https://zulip.example.com", "Basic abc",
            "/user_uploads/1/file.txt", dest
        )
        assert ok is True
        assert dest.exists()
        assert dest.read_bytes() == b"file content"

    def test_ac9_14_download_http_error(self, tmp_path: Path) -> None:
        """AC-9.14: HTTP error returns False, no crash."""
        fh = self._import()
        dest = tmp_path / "fail.txt"
        opener = MagicMock()
        opener.open.side_effect = urllib.request.URLError("404 Not Found")

        ok = fh.download_file(
            opener, "https://zulip.example.com", "Basic abc",
            "/user_uploads/1/missing.txt", dest
        )
        assert ok is False
        assert not dest.exists()

    def test_ac9_15_download_creates_parent_dirs(self, tmp_path: Path) -> None:
        """AC-9.15: Parent directories created automatically."""
        fh = self._import()
        dest = tmp_path / "a" / "b" / "c" / "file.txt"
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"data"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        opener = MagicMock()
        opener.open.return_value = mock_resp

        ok = fh.download_file(
            opener, "https://zulip.example.com", "Basic abc",
            "/user_uploads/1/f.txt", dest
        )
        assert ok is True
        assert dest.parent.exists()


# ============================================================================
# AC-9a: Inbound integration (adapter.py with file attachments)
# ============================================================================


class TestInboundFileHandling:
    """AC-9a: Inbound file handling in adapter._handle_message()."""

    def _make_adapter(self, tmp_path: Path):
        """Create minimal adapter for testing _handle_message with files."""
        from adapters.zulip_adapter.config import (
            ZulipAdapterConfig, StreamConfig,
        )
        from adapters.zulip_adapter.adapter import ZulipAdapter

        cred_file = tmp_path / "creds"
        cred_file.write_text('ZULIP_BOT_API_KEY=testkey123')

        cfg = ZulipAdapterConfig(
            site="https://zulip.test",
            bot_email="bot@test.com",
            bot_credentials=cred_file,
            streams_dir=tmp_path / "streams",
            env_template=tmp_path / "env.sh",
            runtime_dir=tmp_path / "runtime",
        )
        cfg.streams_dir.mkdir()
        cfg.runtime_dir.mkdir()

        project = tmp_path / "project"
        project.mkdir()
        cfg.streams["dev"] = StreamConfig(
            name="dev", project_path=project,
        )

        adapter = ZulipAdapter(cfg)
        return adapter, project

    def _make_event(self, stream, topic, content):
        return {
            "message": {
                "type": "stream",
                "display_recipient": stream,
                "subject": topic,
                "content": content,
                "sender_full_name": "User",
                "sender_email": "user@test.com",
            }
        }

    def _run_handle(self, adapter, event, fifo, written, mock_opener_resp=None):
        """Run _handle_message with mocks for ensure_instance, FIFO, scan_streams."""
        import contextlib

        cms = [
            patch.object(adapter.process_mgr, "ensure_instance",
                         return_value=(fifo, CreateMode.NONE)),
            patch.object(adapter, "_write_to_fifo",
                         side_effect=lambda p, m: (written.append(m), True)[-1]),
            patch("adapters.zulip_adapter.adapter.scan_streams"),
        ]
        if mock_opener_resp is not None:
            cms.append(
                patch.object(adapter._opener, "open", return_value=mock_opener_resp)
            )

        with contextlib.ExitStack() as stack:
            for cm in cms:
                stack.enter_context(cm)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(adapter._handle_message(event))
            finally:
                loop.close()

    def _mock_download_resp(self, data: bytes = b"data"):
        mock_resp = MagicMock()
        mock_resp.read.return_value = data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_ac9a_1_single_attachment_downloaded(self, tmp_path: Path) -> None:
        """AC-9a.1: Single attachment downloaded + FIFO notification."""
        adapter, project = self._make_adapter(tmp_path)
        content = "Here [report.pdf](/user_uploads/1/report.pdf)"

        fifo = tmp_path / "runtime" / "dev" / "test" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))

        written = []
        self._run_handle(
            adapter, self._make_event("dev", "test", content),
            fifo, written, self._mock_download_resp(b"pdf data"),
        )

        assert len(written) == 1
        assert "[File: .zulip-uploads/" in written[0]
        dl_path = project / ".zulip-uploads" / "test" / "report.pdf"
        assert dl_path.exists()

    def test_ac9a_2_text_plus_attachment(self, tmp_path: Path) -> None:
        """AC-9a.2: Text + attachment both appear in FIFO."""
        adapter, project = self._make_adapter(tmp_path)
        content = "Check this [doc.pdf](/user_uploads/1/doc.pdf) please"

        fifo = tmp_path / "runtime" / "dev" / "test" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))

        written = []
        self._run_handle(
            adapter, self._make_event("dev", "test", content),
            fifo, written, self._mock_download_resp(),
        )

        msg = written[0]
        assert "[File:" in msg
        assert "Check this" in msg
        assert "/user_uploads/" not in msg

    def test_ac9a_3_attachment_only(self, tmp_path: Path) -> None:
        """AC-9a.3: Attachment-only message → file notification only."""
        adapter, project = self._make_adapter(tmp_path)
        content = "[data.csv](/user_uploads/1/data.csv)"

        fifo = tmp_path / "runtime" / "dev" / "test" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))

        written = []
        self._run_handle(
            adapter, self._make_event("dev", "test", content),
            fifo, written, self._mock_download_resp(b"csv"),
        )

        msg = written[0]
        assert "[File:" in msg

    def test_ac9a_4_download_fails_text_still_sent(self, tmp_path: Path) -> None:
        """AC-9a.4: Download fails → text still sent."""
        adapter, project = self._make_adapter(tmp_path)
        content = "Here [f.txt](/user_uploads/1/f.txt) text"

        fifo = tmp_path / "runtime" / "dev" / "test" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))

        written = []
        with patch.object(adapter.process_mgr, "ensure_instance",
                          return_value=(fifo, CreateMode.NONE)), \
             patch.object(adapter, "_write_to_fifo",
                          side_effect=lambda p, m: (written.append(m), True)[-1]), \
             patch("adapters.zulip_adapter.adapter.scan_streams"), \
             patch.object(adapter._opener, "open",
                          side_effect=urllib.request.URLError("fail")):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    adapter._handle_message(self._make_event("dev", "test", content))
                )
            finally:
                loop.close()

        assert len(written) == 1
        assert "text" in written[0]

    def test_ac9a_5_traversal_filename_sanitized(self, tmp_path: Path) -> None:
        """AC-9a.5: Path traversal filename is sanitized."""
        from adapters.zulip_adapter.file_handler import sanitize_filename
        result = sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result

    def test_ac9a_6_multiple_attachments(self, tmp_path: Path) -> None:
        """AC-9a.6: Multiple attachments all handled."""
        adapter, project = self._make_adapter(tmp_path)
        content = "[a.txt](/user_uploads/1/a.txt) [b.txt](/user_uploads/2/b.txt)"

        fifo = tmp_path / "runtime" / "dev" / "test" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))

        written = []
        self._run_handle(
            adapter, self._make_event("dev", "test", content),
            fifo, written, self._mock_download_resp(),
        )

        msg = written[0]
        assert msg.count("[File:") == 2


# ============================================================================
# AC-9b: Outbound integration (zulip_relay_hook.py with send-file)
# ============================================================================


class TestOutboundFileUpload:
    """AC-9b: Outbound file upload in zulip_relay_hook.py."""

    def _import_hook(self):
        import importlib
        from scripts import zulip_relay_hook
        return importlib.reload(zulip_relay_hook)

    def test_ac9b_1_send_file_uploaded(self, tmp_path: Path) -> None:
        """AC-9b.1: [send-file:] marker → file uploaded, marker replaced."""
        hook = self._import_hook()
        project = tmp_path / "project"
        project.mkdir()
        test_file = project / "output.txt"
        test_file.write_text("hello")

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(project)}):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "result": "success", "uri": "/user_uploads/1/output.txt"
            }).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)

            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = hook._process_send_file_markers(
                    "Here is the file [send-file: output.txt]",
                    "https://zulip.test", "Basic abc"
                )

            assert "[output.txt](/user_uploads/1/output.txt)" in result
            assert "[send-file:" not in result

    def test_ac9b_2_no_markers_unchanged(self) -> None:
        """AC-9b.2: No markers → content unchanged (backwards compatible)."""
        hook = self._import_hook()
        assert not hook.SEND_FILE_RE.search("Just a normal message")

    def test_ac9b_3_path_outside_project_rejected(self, tmp_path: Path) -> None:
        """AC-9b.3: Path outside project → rejected."""
        hook = self._import_hook()
        project = tmp_path / "project"
        project.mkdir()

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(project)}):
            result = hook._process_send_file_markers(
                "[send-file: ../../etc/shadow]",
                "https://zulip.test", "Basic abc"
            )
        assert result == ""

    def test_ac9b_4_file_not_found_skipped(self, tmp_path: Path) -> None:
        """AC-9b.4: File not found → marker removed."""
        hook = self._import_hook()
        project = tmp_path / "project"
        project.mkdir()

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(project)}):
            result = hook._process_send_file_markers(
                "[send-file: nonexistent.txt]",
                "https://zulip.test", "Basic abc"
            )
        assert result == ""

    def test_ac9b_5_multiple_markers(self, tmp_path: Path) -> None:
        """AC-9b.5: Multiple markers all processed."""
        hook = self._import_hook()
        project = tmp_path / "project"
        project.mkdir()
        (project / "a.txt").write_text("a")
        (project / "b.txt").write_text("b")

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(project)}):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "result": "success", "uri": "/user_uploads/1/file"
            }).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)

            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = hook._process_send_file_markers(
                    "[send-file: a.txt] and [send-file: b.txt]",
                    "https://zulip.test", "Basic abc"
                )

        assert "[send-file:" not in result
        assert "/user_uploads/" in result

    def test_ac9b_6_project_path_unset(self) -> None:
        """AC-9b.6: ZULIP_PROJECT_PATH unset → markers stripped."""
        hook = self._import_hook()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZULIP_PROJECT_PATH", None)
            result = hook._process_send_file_markers(
                "test [send-file: f.txt] end",
                "https://zulip.test", "Basic abc"
            )
        assert "[send-file:" not in result

    def test_ac9b_7_upload_api_error(self, tmp_path: Path) -> None:
        """AC-9b.7: Upload API error → marker removed, no crash."""
        hook = self._import_hook()
        project = tmp_path / "project"
        project.mkdir()
        (project / "f.txt").write_text("data")

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(project)}):
            with patch("urllib.request.urlopen",
                       side_effect=urllib.request.URLError("fail")):
                result = hook._process_send_file_markers(
                    "text [send-file: f.txt] end",
                    "https://zulip.test", "Basic abc"
                )

        assert "[send-file:" not in result

    def test_ac9b_8_stdlib_only(self) -> None:
        """AC-9b.8: Relay hook uses only stdlib imports."""
        hook_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "zulip_relay_hook.py"
        source = hook_path.read_text()
        # Should not import requests, aiohttp, or other third-party libs
        assert "import requests" not in source
        assert "import aiohttp" not in source
        assert "import httpx" not in source


# ============================================================================
# AC-9c: Security tests
# ============================================================================


class TestFileHandlerSecurity:
    """AC-9c: Security boundary tests for file handling."""

    def _import(self):
        from adapters.zulip_adapter import file_handler
        return file_handler

    def test_ac9c_1_inbound_traversal_rejected(self, tmp_path: Path) -> None:
        """AC-9c.1: Inbound ../../etc/passwd filename → rejected."""
        fh = self._import()
        result = fh.safe_resolve(tmp_path, "../../etc/passwd")
        assert result is None

    def test_ac9c_2_outbound_absolute_rejected(self, tmp_path: Path) -> None:
        """AC-9c.2: Outbound absolute path /etc/passwd → rejected."""
        fh = self._import()
        result = fh.safe_resolve(tmp_path, "/etc/passwd")
        assert result is None

    def test_ac9c_3_outbound_relative_traversal(self, tmp_path: Path) -> None:
        """AC-9c.3: Outbound relative traversal ../../../etc/shadow → rejected."""
        fh = self._import()
        result = fh.safe_resolve(tmp_path, "../../../etc/shadow")
        assert result is None

    def test_ac9c_4_null_bytes_sanitized(self) -> None:
        """AC-9c.4: Null bytes in filename → sanitized."""
        fh = self._import()
        result = fh.sanitize_filename("file\x00.txt")
        assert "\x00" not in result
        assert result == "file.txt"

    def test_ac9c_5_symlink_outside_project(self, tmp_path: Path) -> None:
        """AC-9c.5: Symlink pointing outside project → rejected by resolve."""
        fh = self._import()
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        external_file = outside / "external.txt"
        external_file.write_text("data")

        # Create symlink inside project pointing outside
        link = project / "link.txt"
        link.symlink_to(external_file)

        result = fh.safe_resolve(project, "link.txt")
        # resolve() follows symlink → outside project → rejected
        assert result is None


# ============================================================================
# AC-9d: Config/Init tests
# ============================================================================


class TestFileHandlerConfig:
    """AC-9d: Configuration and initialization for file handling."""

    def test_ac9d_1_project_path_in_tmux_env(self, tmp_path: Path) -> None:
        """AC-9d.1: ZULIP_PROJECT_PATH set in tmux env during lazy create."""
        from adapters.zulip_adapter.config import StreamConfig
        from adapters.zulip_adapter.process_mgr import ProcessManager, _parse_env_template

        # Read actual source to verify env var is set
        import inspect
        from adapters.zulip_adapter import process_mgr
        source = inspect.getsource(process_mgr.ProcessManager._lazy_create)
        assert 'ZULIP_PROJECT_PATH' in source

    def test_ac9d_2_gitignore_zulip_uploads(self, tmp_path: Path) -> None:
        """AC-9d.2: .zulip-uploads/ added to .gitignore by ccmux-init."""
        from scripts import ccmux_init
        import importlib
        mod = importlib.reload(ccmux_init)

        project = tmp_path / "project"
        project.mkdir()

        mod.ensure_gitignore(project)
        content = (project / ".gitignore").read_text()
        assert ".zulip-uploads/" in content.splitlines()

    def test_ac9d_3_gitignore_idempotent(self, tmp_path: Path) -> None:
        """AC-9d.3: Running ensure_gitignore twice is idempotent."""
        from scripts import ccmux_init
        import importlib
        mod = importlib.reload(ccmux_init)

        project = tmp_path / "project"
        project.mkdir()

        assert mod.ensure_gitignore(project) is True  # First: modified
        assert mod.ensure_gitignore(project) is False  # Second: no change

        content = (project / ".gitignore").read_text()
        lines = content.splitlines()
        assert lines.count(".zulip-uploads/") == 1
        assert lines.count(".claude/") == 1


# ============================================================================
# AC-9e: Closed-loop scenario tests
# ============================================================================


class TestFileHandlerClosedLoop:
    """AC-9e: End-to-end closed-loop tests."""

    def test_ac9e_1_inbound_end_to_end(self, tmp_path: Path) -> None:
        """AC-9e.1: Mock event → download → file on disk + notification in FIFO."""
        from adapters.zulip_adapter.config import (
            ZulipAdapterConfig, StreamConfig,
        )
        from adapters.zulip_adapter.adapter import ZulipAdapter

        cred_file = tmp_path / "creds"
        cred_file.write_text('ZULIP_BOT_API_KEY=testkey')

        cfg = ZulipAdapterConfig(
            site="https://zulip.test",
            bot_email="bot@test.com",
            bot_credentials=cred_file,
            streams_dir=tmp_path / "streams",
            env_template=tmp_path / "env.sh",
            runtime_dir=tmp_path / "runtime",
        )
        cfg.streams_dir.mkdir()
        cfg.runtime_dir.mkdir()

        project = tmp_path / "project"
        project.mkdir()
        cfg.streams["dev"] = StreamConfig(name="dev", project_path=project)

        adapter = ZulipAdapter(cfg)

        fifo = tmp_path / "runtime" / "dev" / "mytopic" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))

        written = []

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"PDF_CONTENT_HERE"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        event = {
            "message": {
                "type": "stream",
                "display_recipient": "dev",
                "subject": "mytopic",
                "content": "Review this [spec.pdf](/user_uploads/42/ab/spec.pdf)",
                "sender_full_name": "Alice",
                "sender_email": "alice@test.com",
            }
        }

        with patch.object(adapter.process_mgr, "ensure_instance",
                          return_value=(fifo, CreateMode.NONE)), \
             patch.object(adapter, "_write_to_fifo",
                          side_effect=lambda p, m: (written.append(m), True)[-1]), \
             patch.object(adapter._opener, "open", return_value=mock_resp), \
             patch("adapters.zulip_adapter.adapter.scan_streams"):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(adapter._handle_message(event))
            finally:
                loop.close()

        # File on disk
        dl_path = project / ".zulip-uploads" / "mytopic" / "spec.pdf"
        assert dl_path.exists()
        assert dl_path.read_bytes() == b"PDF_CONTENT_HERE"

        # FIFO notification
        assert len(written) == 1
        assert "[File: .zulip-uploads/mytopic/spec.pdf]" in written[0]
        assert "Review this" in written[0]

    def test_ac9e_2_outbound_end_to_end(self, tmp_path: Path) -> None:
        """AC-9e.2: Hook with send-file → mock upload → correct message."""
        hook_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "zulip_relay_hook.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("hook", hook_path)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)

        project = tmp_path / "project"
        project.mkdir()
        (project / "result.csv").write_text("a,b,c")

        upload_resp = MagicMock()
        upload_resp.read.return_value = json.dumps({
            "result": "success", "uri": "/user_uploads/99/result.csv"
        }).encode()
        upload_resp.__enter__ = lambda s: s
        upload_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"ZULIP_PROJECT_PATH": str(project)}):
            with patch("urllib.request.urlopen", return_value=upload_resp):
                result = hook._process_send_file_markers(
                    "Here are the results: [send-file: result.csv]",
                    "https://zulip.test", "Basic abc"
                )

        assert "[result.csv](/user_uploads/99/result.csv)" in result
        assert "Here are the results:" in result

    def test_ac9e_3_round_trip(self, tmp_path: Path) -> None:
        """AC-9e.3: Download inbound, reference outbound → both paths validate."""
        from adapters.zulip_adapter.file_handler import safe_resolve

        project = tmp_path / "project"
        project.mkdir()

        # Inbound: validate download destination
        inbound_dest = safe_resolve(project, ".zulip-uploads/topic/report.pdf")
        assert inbound_dest is not None
        assert str(inbound_dest).startswith(str(project.resolve()))

        # Outbound: validate send-file reference
        outbound_src = safe_resolve(project, ".zulip-uploads/topic/report.pdf")
        assert outbound_src is not None
        assert str(outbound_src).startswith(str(project.resolve()))

        # Both resolve to the same path
        assert inbound_dest == outbound_src


# ============================================================================
# Session Resume (process_mgr + adapter integration)
# ============================================================================


class TestSessionResume:
    """Tests for session resume on restart: CreateMode, instance.toml,
    session JSONL detection, claude command flags, and user notifications."""

    # -- Helper methods --

    def _make_cfg(self, tmp_path: Path) -> "ZulipAdapterConfig":
        from adapters.zulip_adapter.config import ZulipAdapterConfig

        cred = tmp_path / "cred.env"
        cred.write_text("ZULIP_BOT_API_KEY=test\n")
        streams = tmp_path / "streams"
        streams.mkdir()
        env = tmp_path / "env.sh"
        env.write_text("# empty\n")
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        return ZulipAdapterConfig(
            site="https://zulip.example.com",
            bot_email="bot@example.com",
            bot_credentials=cred,
            streams_dir=streams,
            env_template=env,
            runtime_dir=runtime,
        )

    def _make_stream_cfg(self, tmp_path: Path) -> "StreamConfig":
        from adapters.zulip_adapter.config import StreamConfig

        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        return StreamConfig(name="test-stream", project_path=project)

    def _make_adapter(self, tmp_path: Path):
        from adapters.zulip_adapter.adapter import ZulipAdapter

        cfg = self._make_cfg(tmp_path)
        cfg.streams = {"test-stream": self._make_stream_cfg(tmp_path)}
        return ZulipAdapter(cfg)

    def _zulip_event(self, stream, topic, content, sender="user@test.com"):
        return {
            "type": "message",
            "message": {
                "type": "stream",
                "display_recipient": stream,
                "subject": topic,
                "content": content,
                "sender_full_name": "User",
                "sender_email": sender,
            },
        }

    # -- _read_instance_toml / _write_instance_toml tests --

    def test_read_instance_toml_corrupt(self, tmp_path: Path) -> None:
        """Corrupt instance.toml returns empty dict."""
        from adapters.zulip_adapter.process_mgr import _read_instance_toml

        toml_file = tmp_path / "instance.toml"
        toml_file.write_text("this is not valid [[[ toml ===")
        result = _read_instance_toml(toml_file)
        assert result == {}

    def test_read_instance_toml_missing(self, tmp_path: Path) -> None:
        """Missing instance.toml returns empty dict."""
        from adapters.zulip_adapter.process_mgr import _read_instance_toml

        result = _read_instance_toml(tmp_path / "nonexistent.toml")
        assert result == {}

    def test_write_read_roundtrip(self, tmp_path: Path) -> None:
        """Data survives write + read."""
        from adapters.zulip_adapter.process_mgr import (
            _read_instance_toml,
            _write_instance_toml,
        )

        toml_file = tmp_path / "instance.toml"
        data = {
            "session_id": "abc-123-def",
            "created_at": "2026-03-03T09:00:00+08:00",
        }
        _write_instance_toml(toml_file, data)
        result = _read_instance_toml(toml_file)
        assert result["session_id"] == "abc-123-def"
        assert result["created_at"] == "2026-03-03T09:00:00+08:00"

    # -- _claude_session_dir / _session_jsonl_exists tests --

    def test_claude_session_dir_path(self, tmp_path: Path) -> None:
        """Session dir matches Claude Code convention: slashes replaced by dashes."""
        from adapters.zulip_adapter.process_mgr import _claude_session_dir

        project = Path("/home/user/projects/myapp")
        result = _claude_session_dir(project)
        assert result == Path.home() / ".claude" / "projects" / "-home-user-projects-myapp"

    def test_session_jsonl_exists_true(self, tmp_path: Path) -> None:
        """Returns True when session JSONL file exists."""
        from adapters.zulip_adapter.process_mgr import _session_jsonl_exists

        project = tmp_path / "project"
        project.mkdir()

        with patch(
            "adapters.zulip_adapter.process_mgr._claude_session_dir",
            return_value=tmp_path,
        ):
            # Create the JSONL file
            (tmp_path / "test-session-id.jsonl").write_text("{}")
            assert _session_jsonl_exists("test-session-id", project) is True

    def test_session_jsonl_exists_false(self, tmp_path: Path) -> None:
        """Returns False when session JSONL file does not exist."""
        from adapters.zulip_adapter.process_mgr import _session_jsonl_exists

        project = tmp_path / "project"
        project.mkdir()

        with patch(
            "adapters.zulip_adapter.process_mgr._claude_session_dir",
            return_value=tmp_path,
        ):
            assert _session_jsonl_exists("nonexistent-id", project) is False

    # -- _lazy_create session mode determination tests --

    def test_first_time_generates_session_id(self, tmp_path: Path) -> None:
        """New topic with no instance.toml gets a UUID session_id written."""
        from adapters.zulip_adapter.process_mgr import (
            ProcessManager,
            _read_instance_toml,
            _sanitize_name,
        )

        cfg = self._make_cfg(tmp_path)
        sc = self._make_stream_cfg(tmp_path)
        mgr = ProcessManager(cfg)

        # Mock all subprocess calls to avoid real tmux
        with patch("adapters.zulip_adapter.process_mgr.subprocess") as mock_sub, \
             patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=False), \
             patch("adapters.zulip_adapter.process_mgr._session_jsonl_exists", return_value=False):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="12345", stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    mgr._lazy_create("test-stream", "topic1", sc)
                )
            finally:
                loop.close()

        assert result is not None
        fifo, mode = result
        assert mode == CreateMode.FIRST_TIME

        # Verify instance.toml was written with session_id
        instance_toml = cfg.streams_dir / _sanitize_name("test-stream") / "topic1" / "instance.toml"
        data = _read_instance_toml(instance_toml)
        assert "session_id" in data
        assert len(data["session_id"]) == 36  # UUID format

    def test_resume_with_existing_session(self, tmp_path: Path) -> None:
        """Existing session_id + JSONL exists → RESUMED mode, uses --resume flag."""
        from adapters.zulip_adapter.process_mgr import (
            ProcessManager,
            _sanitize_name,
            _write_instance_toml,
        )

        cfg = self._make_cfg(tmp_path)
        sc = self._make_stream_cfg(tmp_path)
        mgr = ProcessManager(cfg)

        # Pre-create instance.toml with session_id
        instance_dir = cfg.streams_dir / _sanitize_name("test-stream") / "topic1"
        instance_dir.mkdir(parents=True)
        _write_instance_toml(
            instance_dir / "instance.toml",
            {"session_id": "existing-uuid-1234", "created_at": "2026-03-03T09:00:00+08:00"},
        )

        with patch("adapters.zulip_adapter.process_mgr.subprocess") as mock_sub, \
             patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=False), \
             patch("adapters.zulip_adapter.process_mgr._session_jsonl_exists", return_value=True):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="12345", stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    mgr._lazy_create("test-stream", "topic1", sc)
                )
            finally:
                loop.close()

        assert result is not None
        fifo, mode = result
        assert mode == CreateMode.RESUMED

    def test_fallback_when_jsonl_missing(self, tmp_path: Path) -> None:
        """session_id exists but JSONL is gone → FALLBACK mode, new UUID generated."""
        from adapters.zulip_adapter.process_mgr import (
            ProcessManager,
            _read_instance_toml,
            _sanitize_name,
            _write_instance_toml,
        )

        cfg = self._make_cfg(tmp_path)
        sc = self._make_stream_cfg(tmp_path)
        mgr = ProcessManager(cfg)

        # Pre-create instance.toml with session_id
        instance_dir = cfg.streams_dir / _sanitize_name("test-stream") / "topic1"
        instance_dir.mkdir(parents=True)
        _write_instance_toml(
            instance_dir / "instance.toml",
            {"session_id": "old-uuid-gone", "created_at": "2026-03-03T09:00:00+08:00"},
        )

        with patch("adapters.zulip_adapter.process_mgr.subprocess") as mock_sub, \
             patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=False), \
             patch("adapters.zulip_adapter.process_mgr._session_jsonl_exists", return_value=False):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="12345", stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    mgr._lazy_create("test-stream", "topic1", sc)
                )
            finally:
                loop.close()

        assert result is not None
        fifo, mode = result
        assert mode == CreateMode.FALLBACK

        # Verify new session_id was written (different from old one)
        data = _read_instance_toml(instance_dir / "instance.toml")
        assert data["session_id"] != "old-uuid-gone"
        assert len(data["session_id"]) == 36

    def test_old_instance_toml_migration(self, tmp_path: Path) -> None:
        """instance.toml without session_id → treated as FIRST_TIME."""
        from adapters.zulip_adapter.process_mgr import (
            ProcessManager,
            _sanitize_name,
        )

        cfg = self._make_cfg(tmp_path)
        sc = self._make_stream_cfg(tmp_path)
        mgr = ProcessManager(cfg)

        # Pre-create instance.toml WITHOUT session_id (old format)
        instance_dir = cfg.streams_dir / _sanitize_name("test-stream") / "topic1"
        instance_dir.mkdir(parents=True)
        (instance_dir / "instance.toml").write_text(
            'created_at = "2026-03-01T09:00:00+08:00"\n'
        )

        with patch("adapters.zulip_adapter.process_mgr.subprocess") as mock_sub, \
             patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=False):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="12345", stderr=b"")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    mgr._lazy_create("test-stream", "topic1", sc)
                )
            finally:
                loop.close()

        assert result is not None
        _, mode = result
        assert mode == CreateMode.FIRST_TIME

    # -- Claude command flag tests --

    def test_claude_command_resume_flag(self, tmp_path: Path) -> None:
        """RESUMED mode → tmux send-keys includes --resume."""
        from adapters.zulip_adapter.process_mgr import (
            ProcessManager,
            _sanitize_name,
            _write_instance_toml,
        )

        cfg = self._make_cfg(tmp_path)
        sc = self._make_stream_cfg(tmp_path)
        mgr = ProcessManager(cfg)

        instance_dir = cfg.streams_dir / _sanitize_name("test-stream") / "topic1"
        instance_dir.mkdir(parents=True)
        _write_instance_toml(
            instance_dir / "instance.toml",
            {"session_id": "resume-uuid", "created_at": "2026-03-03T09:00:00+08:00"},
        )

        send_keys_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "tmux" and len(cmd) > 1 and cmd[1] == "send-keys":
                send_keys_calls.append(cmd)
            return MagicMock(returncode=0, stdout="12345", stderr=b"")

        with patch("adapters.zulip_adapter.process_mgr.subprocess") as mock_sub, \
             patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=False), \
             patch("adapters.zulip_adapter.process_mgr._session_jsonl_exists", return_value=True):
            mock_sub.run.side_effect = mock_run
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(mgr._lazy_create("test-stream", "topic1", sc))
            finally:
                loop.close()

        assert len(send_keys_calls) == 1
        cmd_str = send_keys_calls[0][4]  # The command string sent via send-keys
        assert "--resume resume-uuid" in cmd_str
        assert "--dangerously-skip-permissions" in cmd_str

    def test_claude_command_session_id_flag(self, tmp_path: Path) -> None:
        """FIRST_TIME mode → tmux send-keys includes --session-id."""
        from adapters.zulip_adapter.process_mgr import ProcessManager

        cfg = self._make_cfg(tmp_path)
        sc = self._make_stream_cfg(tmp_path)
        mgr = ProcessManager(cfg)

        send_keys_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "tmux" and len(cmd) > 1 and cmd[1] == "send-keys":
                send_keys_calls.append(cmd)
            return MagicMock(returncode=0, stdout="12345", stderr=b"")

        with patch("adapters.zulip_adapter.process_mgr.subprocess") as mock_sub, \
             patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=False), \
             patch("adapters.zulip_adapter.process_mgr._session_jsonl_exists", return_value=False):
            mock_sub.run.side_effect = mock_run
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(mgr._lazy_create("test-stream", "topic1", sc))
            finally:
                loop.close()

        assert len(send_keys_calls) == 1
        cmd_str = send_keys_calls[0][4]
        assert "--session-id" in cmd_str
        assert "--resume" not in cmd_str
        assert "--dangerously-skip-permissions" in cmd_str

    def test_session_id_in_env_vars(self, tmp_path: Path) -> None:
        """ZULIP_SESSION_ID env var set in tmux session."""
        from adapters.zulip_adapter.process_mgr import ProcessManager

        cfg = self._make_cfg(tmp_path)
        sc = self._make_stream_cfg(tmp_path)
        mgr = ProcessManager(cfg)

        new_session_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "tmux" and len(cmd) > 1 and cmd[1] == "new-session":
                new_session_calls.append(cmd)
            return MagicMock(returncode=0, stdout="12345", stderr=b"")

        with patch("adapters.zulip_adapter.process_mgr.subprocess") as mock_sub, \
             patch("adapters.zulip_adapter.process_mgr._tmux_has_session", return_value=False), \
             patch("adapters.zulip_adapter.process_mgr._session_jsonl_exists", return_value=False):
            mock_sub.run.side_effect = mock_run
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(mgr._lazy_create("test-stream", "topic1", sc))
            finally:
                loop.close()

        assert len(new_session_calls) == 1
        cmd_str = " ".join(new_session_calls[0])
        assert "ZULIP_SESSION_ID=" in cmd_str

    # -- Adapter notification tests --

    def test_resumed_notification(self, tmp_path: Path) -> None:
        """RESUMED → 'Session resumed' posted to Zulip."""
        adapter = self._make_adapter(tmp_path)

        fifo = tmp_path / "runtime" / "test-stream" / "topic" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        event = self._zulip_event("test-stream", "topic", "hello")

        try:
            with patch.object(
                adapter.process_mgr, "ensure_instance",
                return_value=(fifo, CreateMode.RESUMED),
            ), \
                 patch.object(adapter, "_post_message") as mock_post, \
                 patch("adapters.zulip_adapter.adapter.scan_streams"):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(adapter._handle_message(event))
                finally:
                    loop.close()

            # Notification posted
            assert mock_post.call_count == 1
            args = mock_post.call_args[0]
            assert "resumed" in args[2].lower()
            assert "context restored" in args[2].lower()
        finally:
            os.close(sentinel_fd)

    def test_fallback_notification_and_history(self, tmp_path: Path) -> None:
        """FALLBACK → notification + history fetched + injected via FIFO."""
        adapter = self._make_adapter(tmp_path)

        fifo = tmp_path / "runtime" / "test-stream" / "topic" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        event = self._zulip_event("test-stream", "topic", "hello")

        history_messages = [
            {
                "sender_full_name": "Alice",
                "sender_email": "alice@test.com",
                "content": "first message",
                "timestamp": 1709424000,
            },
            {
                "sender_full_name": "Bob",
                "sender_email": "bob@test.com",
                "content": "second message",
                "timestamp": 1709424060,
            },
        ]

        fifo_writes = []

        try:
            with patch.object(
                adapter.process_mgr, "ensure_instance",
                return_value=(fifo, CreateMode.FALLBACK),
            ), \
                 patch.object(adapter, "_post_message") as mock_post, \
                 patch.object(
                     adapter, "_fetch_topic_history", return_value=history_messages
                 ), \
                 patch.object(
                     adapter, "_write_to_fifo",
                     side_effect=lambda p, m: (fifo_writes.append(m), True)[-1],
                 ), \
                 patch("adapters.zulip_adapter.adapter.scan_streams"):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(adapter._handle_message(event))
                finally:
                    loop.close()

            # Two notifications: "Recovering..." and "Recovered N message(s)"
            assert mock_post.call_count == 2
            first_msg = mock_post.call_args_list[0][0][2]
            second_msg = mock_post.call_args_list[1][0][2]
            assert "Recovering context" in first_msg
            assert "Recovered 2 message(s)" in second_msg

            # History context injected via FIFO (first write), then the real message
            assert len(fifo_writes) == 2
            assert "[Context recovery]" in fifo_writes[0]
            assert "Alice" in fifo_writes[0]
            assert "hello" in fifo_writes[1]
        finally:
            os.close(sentinel_fd)

    def test_fallback_empty_history(self, tmp_path: Path) -> None:
        """FALLBACK with no history → notification but no FIFO injection."""
        adapter = self._make_adapter(tmp_path)

        fifo = tmp_path / "runtime" / "test-stream" / "topic" / "in.zulip"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(str(fifo))
        sentinel_fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)

        event = self._zulip_event("test-stream", "topic", "hello")

        try:
            with patch.object(
                adapter.process_mgr, "ensure_instance",
                return_value=(fifo, CreateMode.FALLBACK),
            ), \
                 patch.object(adapter, "_post_message") as mock_post, \
                 patch.object(adapter, "_fetch_topic_history", return_value=[]), \
                 patch("adapters.zulip_adapter.adapter.scan_streams"):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(adapter._handle_message(event))
                finally:
                    loop.close()

            # Only one notification (no "Recovered N" since history is empty)
            assert mock_post.call_count == 1
            assert "Recovering context" in mock_post.call_args_list[0][0][2]
        finally:
            os.close(sentinel_fd)

    def test_fetch_history_filters_bot(self, tmp_path: Path) -> None:
        """_fetch_topic_history filters out bot's own messages."""
        adapter = self._make_adapter(tmp_path)

        api_response = {
            "result": "success",
            "messages": [
                {
                    "sender_email": "user@test.com",
                    "sender_full_name": "User",
                    "content": "user msg",
                    "timestamp": 1709424000,
                },
                {
                    "sender_email": "bot@example.com",
                    "sender_full_name": "Bot",
                    "content": "bot msg",
                    "timestamp": 1709424060,
                },
            ],
        }

        with patch.object(adapter, "_api_call", return_value=api_response):
            result = adapter._fetch_topic_history("stream", "topic")

        assert len(result) == 1
        assert result[0]["content"] == "user msg"

    def test_fetch_history_api_failure(self, tmp_path: Path) -> None:
        """API failure → returns empty list gracefully."""
        adapter = self._make_adapter(tmp_path)

        with patch.object(
            adapter, "_api_call",
            return_value={"result": "error", "msg": "server error"},
        ):
            result = adapter._fetch_topic_history("stream", "topic")

        assert result == []

    def test_format_history_context(self, tmp_path: Path) -> None:
        """_format_history_context produces readable context block."""
        adapter = self._make_adapter(tmp_path)

        history = [
            {
                "sender_full_name": "Alice",
                "content": "What about the spec?",
                "timestamp": 1709424000,
            },
        ]

        result = adapter._format_history_context(history)
        assert "[Context recovery]" in result
        assert "Alice" in result
        assert "What about the spec?" in result
        assert "[End of context recovery" in result
