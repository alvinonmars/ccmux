"""PadAgent configuration (from ccmux.toml [pad] section)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PadAgentConfig:
    """Configuration for the pad monitor."""

    child_name: str
    tailscale_ip: str
    adb_port: int = 5555
    monitor_interval: int = 30
    policy_path: Path = Path()
    state_path: Path = Path()
    usage_dir: Path = Path()
    dashboard_url: str = ""
    lock_url: str = ""
    runtime_dir: Path = Path()
    fk_password: str = ""
    pid_file: Path = Path()


def load(project_root: Path | None = None) -> PadAgentConfig:
    """Load config from ccmux.toml [pad] section.

    FK password loaded from secrets file, not toml.
    Per-child directories created on first load.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent

    # Import here to avoid circular deps at module level
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    toml_path = project_root / "ccmux.toml"
    if not toml_path.exists():
        raise FileNotFoundError(f"ccmux.toml not found at {toml_path}")

    with open(toml_path, "rb") as f:
        cfg = tomllib.load(f)

    pad = cfg.get("pad")
    if not pad:
        raise ValueError("Missing [pad] section in ccmux.toml")

    child_name = pad["child_name"]
    tailscale_ip = pad["tailscale_ip"]
    adb_port = pad.get("adb_port", 5555)
    monitor_interval = pad.get("monitor_interval", 30)
    dashboard_url = pad.get(
        "dashboard_url",
        "file:///sdcard/Android/data/de.ozerov.fully/files/kidpad/index.html",
    )
    lock_url = pad.get(
        "lock_url",
        "file:///sdcard/Android/data/de.ozerov.fully/files/kidpad/lock.html",
    )

    # Data paths
    from ccmux.paths import DATA_ROOT, SECRETS_ROOT, RUNTIME_DIR

    pad_dir = DATA_ROOT / "pad" / child_name.lower()
    usage_dir = pad_dir / "usage"

    # Ensure directories exist
    pad_dir.mkdir(parents=True, exist_ok=True)
    usage_dir.mkdir(parents=True, exist_ok=True)

    # FK password from secrets
    fk_pw_path = SECRETS_ROOT / "pad_fk_password"
    fk_password = ""
    if fk_pw_path.exists():
        fk_password = fk_pw_path.read_text().strip()
    else:
        log.warning("FK password file not found at %s", fk_pw_path)

    return PadAgentConfig(
        child_name=child_name,
        tailscale_ip=tailscale_ip,
        adb_port=adb_port,
        monitor_interval=monitor_interval,
        policy_path=pad_dir / "policy.json",
        state_path=pad_dir / "monitor_state.json",
        usage_dir=usage_dir,
        dashboard_url=dashboard_url,
        lock_url=lock_url,
        runtime_dir=RUNTIME_DIR,
        fk_password=fk_password,
        pid_file=pad_dir / "monitor.pid",
    )
