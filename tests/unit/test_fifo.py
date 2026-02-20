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
