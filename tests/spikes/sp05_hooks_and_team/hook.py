#!/usr/bin/env python3
"""SP-05 multi-event hook: 记录所有 hook 事件，测试触发时机"""
import os, sys, json, time

LOG = "/tmp/ccmux_sp05.log"

stdin_data = {}
try:
    raw = sys.stdin.buffer.read()
    stdin_data = json.loads(raw) if raw else {}
except:
    pass

event = stdin_data.get("hook_event_name", os.environ.get("HOOK_EVENT", "unknown"))
ts = time.strftime("%H:%M:%S")
session = stdin_data.get("session_id", "?")[:8]
tool = stdin_data.get("tool_name", stdin_data.get("tool_input", {}).get("command", ""))

with open(LOG, "a") as f:
    f.write(f"[{ts}] {event:20s} session={session} tool={str(tool)[:40]}\n")
    if event == "Stop":
        f.write(f"           transcript={stdin_data.get('transcript_path','?')}\n")
        f.write(f"           last_msg={stdin_data.get('last_assistant_message','?')[:60]}\n")
