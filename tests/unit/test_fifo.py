"""Unit tests for ccmux.fifo — message parsing logic."""
import time

import pytest

from ccmux.fifo import parse_message


def test_parse_plain_text():
    msg = parse_message("hello world", "in.telegram")
    assert msg.channel == "telegram"
    assert msg.content == "hello world"
    assert abs(msg.ts - int(time.time())) <= 2


def test_parse_plain_text_default_channel():
    msg = parse_message("test", "in")
    assert msg.channel == "in"  # no dot → use full name


def test_parse_json_with_all_fields():
    line = '{"channel":"phone","content":"call me","ts":1700000000,"meta":{}}'
    msg = parse_message(line, "in.phone")
    assert msg.channel == "phone"
    assert msg.content == "call me"
    assert msg.ts == 1700000000


def test_parse_json_missing_channel_falls_back_to_fifo_name():
    line = '{"content":"hi","ts":1700000000}'
    msg = parse_message(line, "in.agent")
    assert msg.channel == "agent"
    assert msg.content == "hi"


def test_parse_invalid_json_treated_as_plain_text():
    line = '{bad json'
    msg = parse_message(line, "in.test")
    assert msg.content == "{bad json"
    assert msg.channel == "test"


def test_parse_json_missing_ts_uses_current_time():
    line = '{"channel":"x","content":"y"}'
    msg = parse_message(line, "in.x")
    assert abs(msg.ts - int(time.time())) <= 2


def test_channel_from_in_dot_name():
    msg = parse_message("text", "in.telegram-bot")
    assert msg.channel == "telegram-bot"


# ---------------------------------------------------------------------------
# Intent metadata in JSON payloads
# ---------------------------------------------------------------------------


def test_parse_json_with_intent_metadata():
    """JSON payload with intent fields produces Message.meta."""
    line = (
        '{"channel":"whatsapp","content":"test","ts":1700000000,'
        '"intent":"receipt","intent_meta":{"confidence":0.9,"action":"respond"}}'
    )
    msg = parse_message(line, "in.whatsapp")
    assert msg.channel == "whatsapp"
    assert msg.content == "test"
    assert msg.meta is not None
    assert msg.meta["intent"] == "receipt"
    assert msg.meta["intent_meta"]["confidence"] == 0.9


def test_parse_json_without_intent_has_no_meta():
    """JSON payload without intent fields has meta=None."""
    line = '{"channel":"whatsapp","content":"hi","ts":1700000000}'
    msg = parse_message(line, "in.whatsapp")
    assert msg.meta is None


def test_parse_plain_text_has_no_meta():
    """Plain text messages always have meta=None."""
    msg = parse_message("hello world", "in.telegram")
    assert msg.meta is None


def test_message_meta_default_is_none():
    """Message.meta defaults to None (backward-compatible)."""
    from ccmux.injector import Message
    m = Message(channel="test", content="hi", ts=0)
    assert m.meta is None
