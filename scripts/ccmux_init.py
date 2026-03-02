#!/usr/bin/env python3
"""Bootstrap a project directory for ccmux integration.

Idempotent — safe to run repeatedly. Only adds missing items, never
overwrites existing config.

Usage:
    ccmux-init <project_path> [--capabilities '{"whatsapp_mcp": true}']
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RELAY_HOOK_SCRIPT = Path(__file__).resolve().parent / "zulip_relay_hook.py"

CLAUDE_MD_TEMPLATE = """\
# Project Developer

You are a developer working on this project. Focus on code quality, testing, and clean implementation.

## Rules

- All output in English: code, comments, docs, commit messages
- Do not access personal data, contacts, or messaging services
- Do not send messages to any external channel — your output is automatically relayed
"""

PRIVACY_CHECK_SCRIPT = Path(__file__).resolve().parent / "privacy_check.py"


def _read_settings(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _is_relay_hook(wrapper: dict, command: str) -> bool:
    """Return True if wrapper contains the relay hook command."""
    if not isinstance(wrapper, dict):
        return False
    hooks = wrapper.get("hooks", [])
    return any(
        isinstance(h, dict) and h.get("command") == command
        for h in hooks
    )


def install_stop_hook(project_path: Path) -> bool:
    """Register zulip_relay_hook.py as a Stop hook. Returns True if modified."""
    settings_path = project_path / ".claude" / "settings.json"
    command = str(RELAY_HOOK_SCRIPT)
    hook_entry = {"type": "command", "command": command}
    wrapper = {"hooks": [hook_entry]}

    settings = _read_settings(settings_path)
    hooks_section: dict = settings.setdefault("hooks", {})
    stop_list: list = hooks_section.setdefault("Stop", [])

    # Check if already installed
    if any(_is_relay_hook(w, command) for w in stop_list):
        return False

    stop_list.append(wrapper)
    _write_settings(settings_path, settings)
    return True


def install_precommit_hook(project_path: Path) -> bool:
    """Symlink pre-commit hook to privacy_check.py. Returns True if modified."""
    git_dir = project_path / ".git"
    if not git_dir.is_dir():
        return False

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    precommit = hooks_dir / "pre-commit"

    if precommit.exists():
        return False

    if PRIVACY_CHECK_SCRIPT.exists():
        precommit.symlink_to(PRIVACY_CHECK_SCRIPT)
        return True
    return False


def ensure_gitignore(project_path: Path) -> bool:
    """Ensure .claude/ and .zulip-uploads/ are in .gitignore. Returns True if modified."""
    gitignore = project_path / ".gitignore"
    entries = [".claude/", ".zulip-uploads/"]

    if gitignore.exists():
        content = gitignore.read_text()
        lines = content.splitlines()
        missing = [e for e in entries if e not in lines]
        if not missing:
            return False
        if content and not content.endswith("\n"):
            content += "\n"
        for e in missing:
            content += e + "\n"
        gitignore.write_text(content)
        return True

    gitignore.write_text("\n".join(entries) + "\n")
    return True


def write_claude_md(project_path: Path) -> bool:
    """Write minimal CLAUDE.md if absent. Returns True if created."""
    claude_md = project_path / "CLAUDE.md"
    if claude_md.exists():
        return False
    claude_md.write_text(CLAUDE_MD_TEMPLATE)
    return True


def apply_capabilities(project_path: Path, capabilities: dict) -> bool:
    """Apply per-stream capabilities. Returns True if modified."""
    if not capabilities:
        return False

    modified = False

    if capabilities.get("whatsapp_mcp"):
        mcp_path = project_path / ".mcp.json"
        mcp_data = {}
        if mcp_path.exists():
            try:
                mcp_data = json.loads(mcp_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        servers = mcp_data.setdefault("mcpServers", {})
        if "whatsapp" not in servers:
            servers["whatsapp"] = {
                "command": "whatsapp-mcp",
                "args": [],
            }
            _write_settings(mcp_path, mcp_data)
            modified = True

    return modified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap a project directory for ccmux integration."
    )
    parser.add_argument("project_path", type=Path, help="Path to the project directory")
    parser.add_argument(
        "--capabilities",
        type=str,
        default="{}",
        help='JSON object of capabilities, e.g. \'{"whatsapp_mcp": true}\'',
    )
    args = parser.parse_args(argv)

    project_path = args.project_path.resolve()
    if not project_path.is_dir():
        print(f"Error: {project_path} is not a directory", file=sys.stderr)
        return 1

    try:
        capabilities = json.loads(args.capabilities)
    except json.JSONDecodeError:
        print(f"Error: invalid capabilities JSON: {args.capabilities}", file=sys.stderr)
        return 1

    actions = []

    if install_stop_hook(project_path):
        actions.append("Stop hook installed")

    if install_precommit_hook(project_path):
        actions.append("Pre-commit hook installed")

    if ensure_gitignore(project_path):
        actions.append(".gitignore updated")

    if write_claude_md(project_path):
        actions.append("CLAUDE.md created")

    if apply_capabilities(project_path, capabilities):
        actions.append(f"Capabilities applied: {list(capabilities.keys())}")

    if actions:
        for a in actions:
            print(f"  ✓ {a}")
    else:
        print("  (no changes needed)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
