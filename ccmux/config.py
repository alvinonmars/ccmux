"""Load and provide ccmux configuration from ccmux.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    project_name: str
    runtime_dir: Path
    idle_threshold: int
    silence_timeout: int
    mcp_port: int
    backoff_initial: int
    backoff_cap: int
    project_root: Path
    claude_proxy: str = ""  # HTTP proxy URL passed only to claude invocation; empty = no proxy

    @property
    def tmux_session(self) -> str:
        return f"ccmux-{self.project_name}"

    @property
    def control_sock(self) -> Path:
        return self.runtime_dir / "control.sock"

    @property
    def output_sock(self) -> Path:
        return self.runtime_dir / "output.sock"

    @property
    def hook_script(self) -> Path:
        return self.project_root / "ccmux" / "hook.py"

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/sse"

    @property
    def stdout_log(self) -> Path:
        return self.runtime_dir / "stdout.log"


def load(project_root: Path | None = None) -> Config:
    """Load config from ccmux.toml; all fields have defaults."""
    if project_root is None:
        project_root = Path.cwd()

    toml_path = project_root / "ccmux.toml"
    data: dict = {}
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

    project = data.get("project", {})
    runtime = data.get("runtime", {})
    timing = data.get("timing", {})
    mcp = data.get("mcp", {})
    recovery = data.get("recovery", {})
    claude = data.get("claude", {})

    project_name = project.get("name") or os.path.basename(project_root)

    # claude_proxy: explicit TOML value takes precedence; falls back to HTTP_PROXY env var.
    # Only passed to the claude invocation — ccmux itself does not use this proxy.
    claude_proxy = claude.get("proxy") or os.environ.get("HTTP_PROXY", "")

    return Config(
        project_name=project_name,
        runtime_dir=Path(runtime.get("dir", "/tmp/ccmux")),
        idle_threshold=timing.get("idle_threshold", 30),
        silence_timeout=timing.get("silence_timeout", 3),
        mcp_port=mcp.get("port", 9876),
        backoff_initial=recovery.get("backoff_initial", 1),
        backoff_cap=recovery.get("backoff_cap", 60),
        project_root=project_root.resolve(),
        claude_proxy=claude_proxy,
    )
