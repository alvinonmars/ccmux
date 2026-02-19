#!/usr/bin/env python3
"""
SP-01 stop hook 脚本 —— 捕获 Claude Code 传给 hook 的所有数据

配置方法见 setup.py。
本脚本由 Claude Code 在每个 assistant turn 结束后调用。
捕获内容写入 /tmp/ccmux_sp01_capture/ 目录，每次调用生成一个文件。
"""

import os
import sys
import json
import time

CAPTURE_DIR = "/tmp/ccmux_sp01_capture"
os.makedirs(CAPTURE_DIR, exist_ok=True)

ts = int(time.time() * 1000)
out = {}

# ── 1. 环境变量 ──────────────────────────────
out["env"] = dict(os.environ)

# ── 2. stdin 内容 ────────────────────────────
try:
    stdin_raw = sys.stdin.buffer.read()
    out["stdin_raw_len"] = len(stdin_raw)
    out["stdin_text"] = stdin_raw.decode("utf-8", errors="replace")
    # 尝试解析为 JSON
    try:
        out["stdin_json"] = json.loads(stdin_raw)
    except Exception as e:
        out["stdin_json_error"] = str(e)
except Exception as e:
    out["stdin_error"] = str(e)

# ── 3. 当前工作目录 ──────────────────────────
out["cwd"] = os.getcwd()

# ── 4. 命令行参数 ────────────────────────────
out["argv"] = sys.argv

# ── 5. 尝试读取 transcript（如果 env 里有路径）─
transcript_keys = [k for k in os.environ if "transcript" in k.lower() or "session" in k.lower()]
out["transcript_related_env_keys"] = transcript_keys

for key in transcript_keys:
    path = os.environ[key]
    if os.path.isfile(path):
        try:
            with open(path) as f:
                content = f.read()
            out[f"transcript_content_{key}"] = content[:8000]  # 限制大小
        except Exception as e:
            out[f"transcript_read_error_{key}"] = str(e)

# ── 写出 ──────────────────────────────────────
outfile = os.path.join(CAPTURE_DIR, f"capture_{ts}.json")
with open(outfile, "w") as f:
    json.dump(out, f, indent=2, default=str)

# 同时追加到 summary 文件，方便查看关键字段
summary_file = os.path.join(CAPTURE_DIR, "summary.txt")
with open(summary_file, "a") as f:
    f.write(f"\n{'='*60}\n")
    f.write(f"CALL at {ts}\n")
    f.write(f"argv: {sys.argv}\n")
    f.write(f"stdin_raw_len: {out.get('stdin_raw_len', 'N/A')}\n")
    f.write(f"stdin_json keys: {list(out['stdin_json'].keys()) if 'stdin_json' in out else 'parse failed'}\n")
    f.write(f"transcript_related_env_keys: {transcript_keys}\n")
    if "stdin_json" in out:
        s = out["stdin_json"]
        f.write(f"stdin_json preview:\n{json.dumps(s, indent=2, default=str)[:2000]}\n")
