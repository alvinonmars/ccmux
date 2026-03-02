"""Zulip adapter configuration.

Reads ccmux.toml [zulip] section and scans ~/.ccmux/streams/ for stream configs.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class StreamConfig:
    """Configuration for a single Zulip stream (from stream.toml)."""

    name: str
    project_path: Path
    channel: str = "zulip"
    capabilities: dict = field(default_factory=dict)


@dataclass
class ZulipAdapterConfig:
    """Top-level adapter configuration."""

    site: str
    bot_email: str
    bot_credentials: Path
    streams_dir: Path
    env_template: Path
    runtime_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "ccmux")
    streams: dict[str, StreamConfig] = field(default_factory=dict)
    _streams_mtime: float = 0.0


def load(project_root: Path | None = None) -> ZulipAdapterConfig:
    """Load Zulip adapter config from ccmux.toml [zulip] section."""
    if project_root is None:
        project_root = Path.cwd()

    toml_path = project_root / "ccmux.toml"
    data: dict = {}
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

    zulip = data.get("zulip", {})
    if not zulip:
        raise ValueError("ccmux.toml [zulip] section is required")

    site = zulip.get("site", "")
    if not site:
        raise ValueError("ccmux.toml [zulip] site is required")

    bot_email = zulip.get("bot_email", "")
    if not bot_email:
        raise ValueError("ccmux.toml [zulip] bot_email is required")

    bot_credentials = Path(os.path.expanduser(zulip.get("bot_credentials", "")))
    if not bot_credentials.exists():
        raise ValueError(f"Bot credentials not found: {bot_credentials}")

    streams_dir = Path(os.path.expanduser(zulip.get("streams_dir", "~/.ccmux/streams")))
    env_template = Path(
        os.path.expanduser(zulip.get("env_template", "~/.ccmux/instances/env_template.sh"))
    )

    runtime = data.get("runtime", {})
    runtime_dir = Path(runtime.get("dir", os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "ccmux"
    )))

    cfg = ZulipAdapterConfig(
        site=site,
        bot_email=bot_email,
        bot_credentials=bot_credentials,
        streams_dir=streams_dir,
        env_template=env_template,
        runtime_dir=runtime_dir,
    )

    scan_streams(cfg)
    return cfg


def _load_stream_toml(stream_dir: Path) -> StreamConfig | None:
    """Load a single stream.toml file."""
    toml_path = stream_dir / "stream.toml"
    if not toml_path.exists():
        return None

    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        log.warning("Failed to parse %s: %s", toml_path, e)
        return None

    project_path = data.get("project_path", "")
    if not project_path:
        log.warning("stream.toml missing project_path: %s", toml_path)
        return None

    return StreamConfig(
        name=stream_dir.name,
        project_path=Path(os.path.expanduser(project_path)),
        channel=data.get("channel", "zulip"),
        capabilities=data.get("capabilities", {}),
    )


def scan_streams(cfg: ZulipAdapterConfig) -> None:
    """Scan streams directory and load all stream.toml files.

    Uses mtime-based caching — only re-scans if the directory has changed.
    """
    if not cfg.streams_dir.exists():
        cfg.streams = {}
        return

    try:
        current_mtime = cfg.streams_dir.stat().st_mtime
    except OSError:
        return

    if current_mtime == cfg._streams_mtime and cfg.streams:
        return  # No change

    streams: dict[str, StreamConfig] = {}
    for entry in cfg.streams_dir.iterdir():
        if not entry.is_dir():
            continue
        stream_cfg = _load_stream_toml(entry)
        if stream_cfg and stream_cfg.channel == "zulip":
            streams[stream_cfg.name] = stream_cfg

    cfg.streams = streams
    cfg._streams_mtime = current_mtime
    log.info("Loaded %d stream(s): %s", len(streams), list(streams.keys()))
