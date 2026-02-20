"""Unit tests for ccmux.injector — format_messages."""
import time

import pytest

from ccmux.injector import Message, format_messages


def _msg(channel: str, content: str, ts: int) -> Message:
    return Message(channel=channel, content=content, ts=ts)


def test_format_single_message():
    ts = int(time.mktime(time.strptime("14:30", "%H:%M")))
    msg = Message(channel="telegram", content="Hello", ts=ts)
    text = format_messages([msg])
    assert "[14:30 telegram] Hello" in text


def test_format_multiple_messages():
    # Use fixed timestamps
    ts1 = int(time.mktime(time.strptime("09:00", "%H:%M")))
    ts2 = int(time.mktime(time.strptime("09:05", "%H:%M")))
    msgs = [
        Message(channel="telegram", content="First", ts=ts1),
        Message(channel="phone", content="Second", ts=ts2),
    ]
    text = format_messages(msgs)
    lines = text.splitlines()
    assert len(lines) == 2
    assert "telegram" in lines[0]
    assert "First" in lines[0]
    assert "phone" in lines[1]
    assert "Second" in lines[1]


def test_format_empty_list():
    assert format_messages([]) == ""


def test_format_preserves_unicode():
    ts = int(time.time())
    msg = Message(channel="wechat", content="你好世界", ts=ts)
    text = format_messages([msg])
    assert "你好世界" in text


def test_format_includes_channel_and_content():
    ts = int(time.time())
    msg = Message(channel="timer", content="reminder", ts=ts)
    text = format_messages([msg])
    assert "timer" in text
    assert "reminder" in text
    assert text.startswith("[")
