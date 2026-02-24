"""Unit tests for libs.web_agent.browser.BrowserSession.

All tests mock Playwright — no real browser is launched.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from libs.web_agent.browser import BrowserSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_dirs(tmp_path: Path):
    """Create state and screenshot directories."""
    state = tmp_path / "state"
    shots = tmp_path / "screenshots"
    return state, shots


@pytest.fixture()
def mock_playwright():
    """Patch sync_playwright to return mock objects."""
    with patch("libs.web_agent.browser.sync_playwright") as mock_sp:
        mock_pw = MagicMock()
        mock_sp.return_value.start.return_value = mock_pw

        mock_browser = MagicMock()
        mock_pw.chromium.launch.return_value = mock_browser

        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_page.url = "https://example.com"
        mock_page.title.return_value = "Example"
        mock_page.inner_text.return_value = "Hello World"

        yield {
            "sync_playwright": mock_sp,
            "pw": mock_pw,
            "browser": mock_browser,
            "context": mock_context,
            "page": mock_page,
        }


# ---------------------------------------------------------------------------
# Tests: Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_creates_directories(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        session = BrowserSession(state_dir=state, screenshot_dir=shots)
        session.start()
        assert state.exists()
        assert shots.exists()
        session.stop()

    def test_context_manager(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            assert session._page is not None
        # After exit, page should be None
        assert session._page is None

    def test_stop_saves_state(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots):
            pass
        mock_playwright["context"].storage_state.assert_called_once_with(
            path=str(state / "storage_state.json"),
        )

    def test_start_restores_state(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        state.mkdir(parents=True, exist_ok=True)
        state_file = state / "storage_state.json"
        state_file.write_text(json.dumps({"cookies": [], "origins": []}))

        session = BrowserSession(state_dir=state, screenshot_dir=shots)
        session.start()
        # new_context should be called with storage_state
        call_kwargs = mock_playwright["browser"].new_context.call_args[1]
        assert call_kwargs["storage_state"] == str(state_file)
        session.stop()

    def test_page_property_raises_if_not_started(self, tmp_dirs):
        state, shots = tmp_dirs
        session = BrowserSession(state_dir=state, screenshot_dir=shots)
        with pytest.raises(RuntimeError, match="not started"):
            _ = session.page


# ---------------------------------------------------------------------------
# Tests: Navigation
# ---------------------------------------------------------------------------

class TestNavigation:
    def test_goto_calls_playwright(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            info = session.goto("https://example.com/test")
            mock_playwright["page"].goto.assert_called_once_with(
                "https://example.com/test",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            assert info["url"] == "https://example.com"

    def test_goto_handles_timeout(self, tmp_dirs, mock_playwright):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        mock_playwright["page"].goto.side_effect = PlaywrightTimeout("timeout")
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            # Should not raise
            info = session.goto("https://slow.example.com")
            assert isinstance(info, dict)


# ---------------------------------------------------------------------------
# Tests: Screenshot
# ---------------------------------------------------------------------------

class TestScreenshot:
    def test_screenshot_auto_names(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        # Make CDP session fail so it falls back to page.screenshot
        cdp_mock = MagicMock()
        cdp_mock.send.side_effect = Exception("CDP unavailable")
        mock_playwright["context"].new_cdp_session.return_value = cdp_mock

        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            path = session.screenshot()
            assert "001.png" in path
            path = session.screenshot()
            assert "002.png" in path

    def test_screenshot_custom_name(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        cdp_mock = MagicMock()
        cdp_mock.send.side_effect = Exception("CDP unavailable")
        mock_playwright["context"].new_cdp_session.return_value = cdp_mock

        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            path = session.screenshot("login_page")
            assert "login_page.png" in path


# ---------------------------------------------------------------------------
# Tests: Interaction
# ---------------------------------------------------------------------------

class TestInteraction:
    def test_click_by_selector(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            session.click(selector="#submit")
            mock_playwright["page"].click.assert_called_once_with(
                "#submit", timeout=5000,
            )

    def test_click_by_text(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        mock_locator = MagicMock()
        mock_playwright["page"].get_by_text.return_value.first = mock_locator

        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            session.click(text="Sign In")
            mock_playwright["page"].get_by_text.assert_called_once_with(
                "Sign In", exact=False,
            )

    def test_click_by_position(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            session.click(position=(100, 200))
            mock_playwright["page"].mouse.click.assert_called_once_with(100, 200)

    def test_click_no_args_raises(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            with pytest.raises(ValueError, match="Provide one of"):
                session.click()

    def test_fill(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            session.fill("#username", "admin")
            mock_playwright["page"].fill.assert_called_once_with(
                "#username", "admin",
            )

    def test_select(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            session.select("#dropdown", "option1")
            mock_playwright["page"].select_option.assert_called_once_with(
                "#dropdown", "option1",
            )

    def test_press(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            session.press("Enter")
            mock_playwright["page"].keyboard.press.assert_called_once_with(
                "Enter",
            )

    def test_scroll(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            session.scroll("down", 300)
            mock_playwright["page"].mouse.wheel.assert_called_once_with(0, 300)


# ---------------------------------------------------------------------------
# Tests: Page info
# ---------------------------------------------------------------------------

class TestPageInfo:
    def test_page_info(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            info = session.page_info()
            assert info["url"] == "https://example.com"
            assert info["title"] == "Example"
            assert "Hello World" in info["text_snippet"]

    def test_get_links(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        mock_el = MagicMock()
        mock_el.inner_text.return_value = "Home"
        mock_el.get_attribute.return_value = "/home"
        mock_playwright["page"].query_selector_all.return_value = [mock_el]

        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            links = session.get_links()
            assert len(links) == 1
            assert links[0]["text"] == "Home"
            assert links[0]["href"] == "/home"

    def test_get_forms(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        mock_input = MagicMock()
        mock_input.evaluate.return_value = "input"
        mock_input.get_attribute.side_effect = lambda attr: {
            "type": "text", "name": "username", "id": "user",
            "placeholder": "Enter username", "value": "",
        }.get(attr, "")

        mock_form = MagicMock()
        mock_form.get_attribute.side_effect = lambda attr: {
            "action": "/login", "method": "post",
        }.get(attr, "")
        mock_form.query_selector_all.return_value = [mock_input]

        mock_playwright["page"].query_selector_all.return_value = [mock_form]

        with BrowserSession(state_dir=state, screenshot_dir=shots) as session:
            forms = session.get_forms()
            assert len(forms) == 1
            assert forms[0]["action"] == "/login"
            assert len(forms[0]["fields"]) == 1
            assert forms[0]["fields"][0]["name"] == "username"


# ---------------------------------------------------------------------------
# Tests: State management
# ---------------------------------------------------------------------------

class TestState:
    def test_clear_state(self, tmp_dirs, mock_playwright):
        state, shots = tmp_dirs
        state.mkdir(parents=True, exist_ok=True)
        state_file = state / "storage_state.json"
        state_file.write_text("{}")

        session = BrowserSession(state_dir=state, screenshot_dir=shots)
        assert session.has_saved_state()
        session.clear_state()
        assert not session.has_saved_state()

    def test_has_saved_state_false(self, tmp_dirs):
        state, shots = tmp_dirs
        session = BrowserSession(state_dir=state, screenshot_dir=shots)
        assert not session.has_saved_state()


# ---------------------------------------------------------------------------
# Tests: PowerSchool auth
# ---------------------------------------------------------------------------

class TestPowerSchoolAuth:
    def test_load_credentials(self, tmp_path):
        env_file = tmp_path / "test.env"
        env_file.write_text(
            "POWERSCHOOL_URL=https://ps.example.com\n"
            "POWERSCHOOL_USER=user@example.com\n"
            "POWERSCHOOL_PASS=secret123\n"
        )
        from libs.web_agent.auth.powerschool import load_credentials
        creds = load_credentials(env_file)
        assert creds["POWERSCHOOL_URL"] == "https://ps.example.com"
        assert creds["POWERSCHOOL_USER"] == "user@example.com"
        assert creds["POWERSCHOOL_PASS"] == "secret123"

    def test_load_credentials_missing_key(self, tmp_path):
        env_file = tmp_path / "test.env"
        env_file.write_text("POWERSCHOOL_URL=https://ps.example.com\n")
        from libs.web_agent.auth.powerschool import load_credentials
        with pytest.raises(ValueError, match="Missing POWERSCHOOL_USER"):
            load_credentials(env_file)

    def test_load_credentials_file_not_found(self, tmp_path):
        from libs.web_agent.auth.powerschool import load_credentials
        with pytest.raises(FileNotFoundError):
            load_credentials(tmp_path / "nonexistent.env")


# ---------------------------------------------------------------------------
# Tests: Prompt template
# ---------------------------------------------------------------------------

class TestPromptTemplate:
    def test_basic_prompt_generation(self):
        from libs.web_agent.prompts import web_task_prompt
        prompt = web_task_prompt(task="Sign up for field trip")
        assert "Sign up for field trip" in prompt
        assert "BrowserSession" in prompt
        assert "xvfb-run" in prompt

    def test_powerschool_auth_included(self):
        from libs.web_agent.prompts import web_task_prompt
        prompt = web_task_prompt(task="Test", auth="powerschool")
        assert "load_credentials" in prompt
        assert "ADFS SSO" in prompt

    def test_no_auth(self):
        from libs.web_agent.prompts import web_task_prompt
        prompt = web_task_prompt(task="Test", auth="none")
        assert "No login required" in prompt
        assert "load_credentials" not in prompt

    def test_requester_jid_included(self):
        from libs.web_agent.prompts import web_task_prompt
        prompt = web_task_prompt(
            task="Test", requester_jid="12345@s.whatsapp.net",
        )
        assert "12345@s.whatsapp.net" in prompt
        assert "send_file" in prompt

    def test_extra_context_included(self):
        from libs.web_agent.prompts import web_task_prompt
        prompt = web_task_prompt(
            task="Test", extra_context="The form has 3 fields.",
        )
        assert "The form has 3 fields." in prompt

    def test_form_submission_protocol(self):
        from libs.web_agent.prompts import web_task_prompt
        prompt = web_task_prompt(task="Test")
        assert "NEVER submit a form" in prompt
        assert "Awaiting confirmation" in prompt

    def test_custom_dirs(self):
        from libs.web_agent.prompts import web_task_prompt
        prompt = web_task_prompt(
            task="Test",
            state_dir="/custom/state",
            screenshot_dir="/custom/shots",
        )
        assert "/custom/state" in prompt
        assert "/custom/shots" in prompt
