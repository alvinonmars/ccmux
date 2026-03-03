"""Integration test conftest — imports Zulip fixtures for test discovery."""

# Import all fixtures from conftest_zulip so pytest can discover them.
# This is necessary because pytest only auto-loads conftest.py files.
from tests.integration.conftest_zulip import (  # noqa: F401
    mock_zulip,
    zulip_credentials,
    zulip_env_template,
    zulip_streams_dir,
    zulip_project_dir,
    zulip_config,
    zulip_adapter,
    zulip_test_env,
    mock_claude_env,
    patch_claude_cmd,
    fast_timing,
    cleanup_tmux,
    pytest_configure,
)
