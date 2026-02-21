"""WhatsApp notifier configuration loaded from ccmux.toml [whatsapp] section."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class WANotifierConfig:
    db_path: Path
    poll_interval: int = 30
    allowed_chats: list[str] = field(default_factory=list)
    ignore_groups: bool = True
    runtime_dir: Path = Path("/tmp/ccmux")


def load(project_root: Path | None = None) -> WANotifierConfig:
    """Load WhatsApp notifier config from ccmux.toml [whatsapp] section."""
    if project_root is None:
        project_root = Path.cwd()

    toml_path = project_root / "ccmux.toml"
    data: dict = {}
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

    wa = data.get("whatsapp", {})
    runtime = data.get("runtime", {})

    db_path_str = wa.get("db_path", "")
    if not db_path_str:
        raise ValueError(
            "ccmux.toml [whatsapp] db_path is required. "
            "Set it to the whatsapp-mcp SQLite database path."
        )

    poll_interval = wa.get("poll_interval", 30)
    if poll_interval < 1:
        raise ValueError(
            "ccmux.toml [whatsapp] poll_interval must be >= 1 second."
        )

    return WANotifierConfig(
        db_path=Path(db_path_str),
        poll_interval=poll_interval,
        allowed_chats=wa.get("allowed_chats", []),
        ignore_groups=wa.get("ignore_groups", True),
        runtime_dir=Path(runtime.get("dir", "/tmp/ccmux")),
    )
