"""WeChat Work notifier configuration loaded from ccmux.toml [wework] + secrets."""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
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
class WeWorkConfig:
    port: int
    runtime_dir: Path
    corp_id: str
    agent_id: str
    secret: str
    callback_token: str
    callback_aes_key: str  # raw Base64-encoded 43-char key
    verify_file: str  # domain verification filename (e.g. "WW_verify_xxx.txt")
    verify_content: str  # domain verification response content


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (no quoting, # comments)."""
    env: dict[str, str] = {}
    if not path.exists():
        raise FileNotFoundError(f"Secrets file not found: {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load(project_root: Path | None = None) -> WeWorkConfig:
    """Load WeWork config from ccmux.toml [wework] + secret files."""
    from ccmux.paths import WEWORK_ENV, WEWORK_CALLBACK_ENV

    if project_root is None:
        project_root = Path.cwd()

    toml_path = project_root / "ccmux.toml"
    data: dict = {}
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

    ww = data.get("wework", {})
    runtime = data.get("runtime", {})

    port = ww.get("port", 9801)
    runtime_dir = Path(runtime.get("dir", "/tmp/ccmux"))

    # Load secrets from env files
    main_env = _load_env_file(WEWORK_ENV)
    callback_env = _load_env_file(WEWORK_CALLBACK_ENV)

    corp_id = main_env.get("WEWORK_CORP_ID", "")
    agent_id = main_env.get("WEWORK_AGENT_ID", "")

    # Secret stored in a separate key file
    secret_file = main_env.get("WEWORK_SECRET_FILE", "")
    if secret_file:
        secret_path = Path(secret_file).expanduser()
        if not secret_path.exists():
            raise FileNotFoundError(f"WeWork secret key file not found: {secret_path}")
        secret = secret_path.read_text().strip()
    else:
        raise ValueError("WEWORK_SECRET_FILE not set in wework.env")

    callback_token = callback_env.get("WEWORK_CALLBACK_TOKEN", "")
    callback_aes_key = callback_env.get("WEWORK_CALLBACK_AES_KEY", "")
    verify_file = callback_env.get("WEWORK_VERIFY_FILE", "")
    verify_content = callback_env.get("WEWORK_VERIFY_CONTENT", "")

    if not all([corp_id, agent_id, secret, callback_token, callback_aes_key,
                verify_file, verify_content]):
        raise ValueError(
            "Incomplete WeWork config. Required in wework.env: WEWORK_CORP_ID, "
            "WEWORK_AGENT_ID, WEWORK_SECRET_FILE. Required in wework_callback.env: "
            "WEWORK_CALLBACK_TOKEN, WEWORK_CALLBACK_AES_KEY, WEWORK_VERIFY_FILE, "
            "WEWORK_VERIFY_CONTENT"
        )

    log.info("WeWork config loaded: corp_id=%s agent_id=%s port=%d", corp_id, agent_id, port)

    return WeWorkConfig(
        port=port,
        runtime_dir=runtime_dir,
        corp_id=corp_id,
        agent_id=agent_id,
        secret=secret,
        callback_token=callback_token,
        callback_aes_key=callback_aes_key,
        verify_file=verify_file,
        verify_content=verify_content,
    )
