"""AC-07: Output socket broadcast format.

Tests that broadcast payloads contain correct fields and structure.
Layer: Integration/mock — fire_hook + control_server + broadcaster.

T-07-1: plain text reply (ts, session, turn fields)
T-07-2: reply with thinking block (passthrough verification)
T-07-3: reply with tool_use and tool_result blocks (passthrough verification)
T-07-4: ts accuracy (≤ 2s drift)
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from tests.helpers import connect_subscriber


@pytest.mark.asyncio
async def test_T07_1_plain_text_reply(control_server, broadcaster, test_config, fire_hook, tmp_path):
    """T-07-1: plain text turn → broadcast has ts, session, turn fields."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "plain reply"}],
            },
            "ts": 1700000000,
        }) + "\n"
    )

    reader, writer = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)

    # fire_hook calls hook.py → hook.py sends to control.sock → control_server receives
    result = fire_hook("Stop", {
        "session_id": "sess-plain",
        "transcript_path": str(transcript),
    })
    assert result.returncode == 0

    await asyncio.sleep(0.3)
    cs, broadcasts, _ = control_server

    assert broadcasts, "No broadcast received by control server"
    msg = broadcasts[-1]
    assert "ts" in msg, "hook.py must include ts in broadcast"
    payload = {"ts": msg["ts"], "session": msg["session"], "turn": msg["turn"]}
    await broadcaster.broadcast(payload)

    data = await asyncio.wait_for(reader.readline(), timeout=2.0)
    broadcast = json.loads(data)
    assert "ts" in broadcast
    assert isinstance(broadcast["ts"], int)
    assert "session" in broadcast
    assert isinstance(broadcast["session"], str)
    assert "turn" in broadcast
    assert isinstance(broadcast["turn"], list)

    writer.close()


@pytest.mark.asyncio
async def test_T07_2_thinking_block_passthrough(control_server, broadcaster, test_config, fire_hook, tmp_path):
    """T-07-2: transcript with thinking block → turn contains thinking + text blocks."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me reason about this..."},
                    {"type": "text", "text": "Here is my answer."},
                ],
            },
            "ts": 1700000000,
        }) + "\n"
    )

    reader, writer = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)

    result = fire_hook("Stop", {
        "session_id": "sess-thinking",
        "transcript_path": str(transcript),
    })
    assert result.returncode == 0

    await asyncio.sleep(0.3)
    cs, broadcasts, _ = control_server

    assert broadcasts, "No broadcast received by control server"
    msg = broadcasts[-1]
    assert "ts" in msg, "hook.py must include ts in broadcast"
    payload = {"ts": msg["ts"], "session": msg["session"], "turn": msg["turn"]}
    await broadcaster.broadcast(payload)

    data = await asyncio.wait_for(reader.readline(), timeout=2.0)
    broadcast = json.loads(data)
    turn = broadcast["turn"]
    assert len(turn) == 2
    assert turn[0]["type"] == "thinking"
    assert turn[0]["thinking"] == "Let me reason about this..."
    assert turn[1]["type"] == "text"
    assert turn[1]["text"] == "Here is my answer."

    writer.close()


@pytest.mark.asyncio
async def test_T07_3_tool_use_passthrough(control_server, broadcaster, test_config, fire_hook, tmp_path):
    """T-07-3: transcript with tool_use and tool_result → blocks passed through."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will send a message."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "send_to_channel",
                        "input": {"channel": "telegram", "message": "hello"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": "ok",
                    },
                ],
            },
            "ts": 1700000000,
        }) + "\n"
    )

    reader, writer = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)

    result = fire_hook("Stop", {
        "session_id": "sess-tool",
        "transcript_path": str(transcript),
    })
    assert result.returncode == 0

    await asyncio.sleep(0.3)
    cs, broadcasts, _ = control_server

    assert broadcasts, "No broadcast received by control server"
    msg = broadcasts[-1]
    assert "ts" in msg, "hook.py must include ts in broadcast"
    payload = {"ts": msg["ts"], "session": msg["session"], "turn": msg["turn"]}
    await broadcaster.broadcast(payload)

    data = await asyncio.wait_for(reader.readline(), timeout=2.0)
    broadcast = json.loads(data)
    turn = broadcast["turn"]
    assert len(turn) == 3
    assert turn[0]["type"] == "text"
    assert turn[1]["type"] == "tool_use"
    assert turn[1]["name"] == "send_to_channel"
    assert turn[1]["input"] == {"channel": "telegram", "message": "hello"}
    assert turn[2]["type"] == "tool_result"
    assert turn[2]["tool_use_id"] == "toolu_01"

    writer.close()


@pytest.mark.asyncio
async def test_T07_4_ts_accuracy(control_server, broadcaster, test_config, fire_hook, tmp_path):
    """T-07-4: ts in broadcast differs from actual trigger time by ≤2s."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            "ts": 1700000000,
        }) + "\n"
    )

    reader, writer = await connect_subscriber(test_config.output_sock)
    await asyncio.sleep(0.1)
    t_before = int(time.time())

    fire_hook("Stop", {
        "session_id": "s4",
        "transcript_path": str(transcript),
    })
    await asyncio.sleep(0.3)

    cs, broadcasts, _ = control_server
    assert broadcasts
    msg = broadcasts[-1]
    assert "ts" in msg, "hook.py must include ts in broadcast"
    payload = {"ts": msg["ts"], "session": msg["session"], "turn": msg["turn"]}
    await broadcaster.broadcast(payload)

    data = await asyncio.wait_for(reader.readline(), timeout=2.0)
    broadcast = json.loads(data)
    t_after = int(time.time())

    ts_diff = abs(broadcast["ts"] - t_before)
    assert ts_diff <= 2, f"ts differs by {ts_diff}s (expected ≤2s)"

    writer.close()
