#!/usr/bin/env python3
"""
SP-02 分析脚本 —— 从捕获的 stdout 中提取提示符模式

运行: python3 analyze.py [capture_file]
默认 capture_file: /tmp/ccmux_sp02_stdout.raw
"""

import sys
import os
import re
import json

CAPTURE_FILE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ccmux_sp02_stdout.raw"
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "sp02_results.txt")

results = []

def log(msg=""):
    print(msg)
    results.append(msg)


def strip_ansi(text: str) -> str:
    """移除 ANSI 控制序列"""
    return re.sub(r'\x1b\[[0-9;]*[mGKHFABCDJsu]|\x1b\][^\x07]*\x07|\x1b[=>]|\r', '', text)


def xxd_preview(data: bytes, n: int = 64) -> str:
    """显示前 n 字节的 hex + ascii"""
    chunk = data[:n]
    hex_part = ' '.join(f'{b:02x}' for b in chunk)
    ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
    return f"{hex_part}  |{ascii_part}|"


def main():
    if not os.path.exists(CAPTURE_FILE):
        log(f"❌ 捕获文件不存在: {CAPTURE_FILE}")
        log("请先运行 monitor.sh 完成捕获")
        return

    with open(CAPTURE_FILE, "rb") as f:
        raw = f.read()

    log(f"SP-02 分析结果")
    log(f"捕获文件: {CAPTURE_FILE}")
    log(f"文件大小: {len(raw)} bytes")

    # ── 原始字节预览 ──────────────────────────
    log("\n── 前 128 字节 (hex) ──")
    log(xxd_preview(raw, 128))

    # ── 按行分割（忽略 ANSI）────────────────────
    text = raw.decode("utf-8", errors="replace")
    clean = strip_ansi(text)

    lines = clean.split("\n")
    log(f"\n── 共 {len(lines)} 行（去除 ANSI 后）──")

    # ── 寻找短行（候选提示符行）────────────────
    # 提示符通常很短（< 30 字符），且出现在输出末尾或对话分界处
    log("\n── 候选提示符行（长度 < 30 的非空行）──")
    prompt_candidates = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if 0 < len(stripped) < 30:
            prompt_candidates.append((i, stripped))
            log(f"  line {i:4d}: {repr(stripped)}")

    # ── 寻找 permission prompt ───────────────
    log("\n── 包含 permission / allow / y/n / yes/no 关键词的行 ──")
    perm_keywords = ["permission", "allow", "y/n", "yes", "no", "approve",
                     "run", "execute", "confirm", "proceed"]
    perm_candidates = []
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(kw in lower for kw in perm_keywords) and len(line.strip()) > 0:
            perm_candidates.append((i, line.strip()))
            log(f"  line {i:4d}: {repr(line.strip()[:120])}")

    # ── 重复出现的行（提示符通常重复）──────────
    from collections import Counter
    freq = Counter(line.strip() for line in lines if line.strip())
    log("\n── 出现频率最高的行（提示符通常重复出现）──")
    for text_val, count in freq.most_common(10):
        if count > 1:
            log(f"  x{count:3d}: {repr(text_val[:80])}")

    # ── 保存原始 ANSI 字节序列中的特殊模式 ─────
    log("\n── 原始字节中出现的 ANSI ESC 序列类型 ──")
    ansi_seqs = re.findall(rb'\x1b\[[0-9;]*[mGKHFABCDJsu]', raw)
    ansi_freq = Counter(ansi_seqs)
    for seq, count in ansi_freq.most_common(15):
        log(f"  x{count:3d}: {seq!r}")

    # ── 结论模板 ──────────────────────────────
    log("\n" + "="*50)
    log("需要人工填写到 spec.md 的结论:")
    log("  [ ] 正常等待输入提示符的精确内容: _________")
    log("  [ ] 提示符的正则表达式模式: _________")
    log("  [ ] permission prompt 的识别特征: _________")
    log("  [ ] 两者是否可靠区分: yes / no")
    log("  [ ] pipe-pane -O 捕获是否稳定: yes / no")
    log(f"\n  原始文件供人工检查: {CAPTURE_FILE}")

    with open(RESULTS_FILE, "w") as f:
        f.write("\n".join(results) + "\n")
    log(f"\n结果已写入: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
