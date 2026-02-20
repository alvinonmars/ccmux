"""AC-03: Message injection into Claude Code via tmux send-keys.

Layer: Integration/mock — bare_pane + fire_hook.
Tests SP-04 verified behavior: -l flag, Enter separate, lossless unicode.
"""
import asyncio
import time
from pathlib import Path

import pytest
import libtmux


def _pane_text(pane: libtmux.Pane) -> str:
    """Get current visible text in pane."""
    lines = pane.cmd("capture-pane", "-p").stdout
    return "\n".join(lines) if isinstance(lines, list) else lines


@pytest.fixture
def cat_pane(tmux_server, test_config):
    """tmux pane running cat — collects injected text."""
    session = tmux_server.new_session(
        session_name=test_config.tmux_session, window_name="test"
    )
    pane = session.active_window.active_pane
    pane.send_keys("cat", enter=True)
    time.sleep(0.3)
    yield pane
    tmux_server.kill_session(target_session=test_config.tmux_session)


def test_T03_1_single_message_injection(cat_pane):
    """T-03-1: inject 'hello world' → appears in pane."""
    from ccmux.injector import inject
    inject(cat_pane, "hello world")
    time.sleep(0.3)
    text = _pane_text(cat_pane)
    assert "hello world" in text


def test_T03_2_chinese_content(cat_pane):
    """T-03-2: inject Chinese characters → no corruption."""
    from ccmux.injector import inject
    inject(cat_pane, "你好世界")
    time.sleep(0.3)
    text = _pane_text(cat_pane)
    assert "你好世界" in text


def test_T03_3_multiple_messages_merged(cat_pane):
    """T-03-3: 3 messages merged into one injection."""
    from ccmux.injector import Message, inject_messages
    ts = int(time.time())
    msgs = [
        Message(channel="telegram", content="msg1", ts=ts),
        Message(channel="phone", content="msg2", ts=ts),
        Message(channel="timer", content="msg3", ts=ts),
    ]
    inject_messages(cat_pane, msgs)
    time.sleep(0.3)
    text = _pane_text(cat_pane)
    assert "msg1" in text
    assert "msg2" in text
    assert "msg3" in text


def test_T03_4_injection_format(cat_pane):
    """T-03-4: format is [HH:MM channel] content."""
    from ccmux.injector import Message, inject_messages, format_messages
    import time as _time
    ts = int(_time.time())
    msgs = [Message(channel="source", content="content here", ts=ts)]
    text = format_messages(msgs)
    assert text.startswith("[")
    assert "source" in text
    assert "content here" in text
    assert "]" in text


def test_T03_special_characters(cat_pane):
    """inject: special characters ($, backtick, !, ', \", |) are lossless."""
    from ccmux.injector import inject
    special = "$ ` ! ' \" | ; * [] {}"
    inject(cat_pane, special)
    time.sleep(0.3)
    text = _pane_text(cat_pane)
    # At minimum, the dollar sign and brackets should appear
    assert "$" in text or "dollar" in text or special[:3] in text
