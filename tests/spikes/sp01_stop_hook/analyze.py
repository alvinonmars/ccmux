#!/usr/bin/env python3
"""
SP-01 分析脚本 —— 解析捕获到的 hook 调用数据，输出关键结论

运行: python3 analyze.py
"""

import json
import os
import glob

CAPTURE_DIR = "/tmp/ccmux_sp01_capture"
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "sp01_results.txt")

results = []

def log(msg=""):
    print(msg)
    results.append(msg)


def analyze_capture(path: str, index: int):
    with open(path) as f:
        data = json.load(f)

    log(f"\n{'─'*50}")
    log(f"捕获 #{index}: {os.path.basename(path)}")

    # ── stdin 分析 ──
    stdin_json = data.get("stdin_json")
    if stdin_json:
        log(f"  stdin 是合法 JSON: ✅")
        log(f"  stdin 顶层 keys: {list(stdin_json.keys())}")

        # 寻找 transcript 相关字段
        for key in ["transcript_path", "session_id", "session", "conversation_id"]:
            if key in stdin_json:
                log(f"  stdin['{key}']: {stdin_json[key]}")

        # 寻找 messages / content
        for key in ["messages", "content", "response", "assistant_response"]:
            if key in stdin_json:
                val = stdin_json[key]
                preview = json.dumps(val)[:300]
                log(f"  stdin['{key}'] (前300字): {preview}")

        # thinking block
        def find_thinking(obj, path=""):
            if isinstance(obj, dict):
                if obj.get("type") == "thinking":
                    log(f"  ✅ 发现 thinking block at {path}: keys={list(obj.keys())}")
                for k, v in obj.items():
                    find_thinking(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    find_thinking(v, f"{path}[{i}]")

        log("  ── 搜索 thinking block ──")
        find_thinking(stdin_json)

    else:
        log(f"  stdin 不是 JSON: {data.get('stdin_json_error', 'unknown')}")
        log(f"  stdin 原始内容 (前500字): {data.get('stdin_text', '')[:500]}")

    # ── 环境变量中的 transcript 相关 ──
    env_keys = data.get("transcript_related_env_keys", [])
    log(f"  transcript 相关环境变量: {env_keys}")
    for key in env_keys:
        val = data["env"].get(key, "N/A")
        log(f"    {key} = {val}")
        # 如果有 transcript content
        content_key = f"transcript_content_{key}"
        if content_key in data:
            log(f"    transcript 内容 (前500字): {data[content_key][:500]}")

    # ── hook 触发频率猜测 ──
    log(f"  argv: {data.get('argv', [])}")
    log(f"  cwd: {data.get('cwd', '')}")


def main():
    captures = sorted(glob.glob(os.path.join(CAPTURE_DIR, "capture_*.json")))

    if not captures:
        log("❌ 未找到捕获文件。请先运行 setup.py install，完成一次 Claude Code 对话，再运行本脚本。")
        return

    log(f"SP-01 分析结果")
    log(f"共找到 {len(captures)} 次 hook 调用")

    for i, path in enumerate(captures, 1):
        analyze_capture(path, i)

    log("\n" + "="*50)
    log("需要填写到 spec.md 的结论:")
    log("  [ ] transcript 路径的环境变量名")
    log("  [ ] thinking block 的 JSON 字段名")
    log("  [ ] stdin 数据格式（JSON / 纯文本 / 无）")
    log("  [ ] hook 触发时机（每 turn / 仅会话结束）")
    log(f"  → 查看原始数据: ls {CAPTURE_DIR}/")

    with open(RESULTS_FILE, "w") as f:
        f.write("\n".join(results) + "\n")
    log(f"\n结果已写入: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
