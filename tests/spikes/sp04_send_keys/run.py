#!/usr/bin/env python3
"""
SP-04: tmux send-keys 注入行为验证

验证目标:
  1. Unicode / 中文字符能否正确注入
  2. Shell 特殊字符是否被解释（$, `, !, ", \）
  3. 是否需要 -l (literal) 标志
  4. 注入后 Claude Code 收到的内容是否与原始内容完全一致

原理: 在 tmux pane 中运行 `cat`，send-keys 注入，cat 原样输出，与预期比对。

运行: python3 run.py
依赖: tmux 已安装
结果写入: sp04_results.txt
"""

import os
import sys
import time
import subprocess
import tempfile

RESULTS_FILE = os.path.join(os.path.dirname(__file__), "sp04_results.txt")
SESSION = "ccmux_sp04"
OUTPUT_FILE = tempfile.mktemp(suffix=".sp04.txt")

results = []

def log(msg):
    print(msg)
    results.append(msg)


def run(cmd, **kwargs):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)


def tmux(cmd):
    return run(f"tmux {cmd}")


def setup_session():
    """创建 tmux session，在 pane 里运行 cat，输出重定向到文件"""
    run(f"tmux kill-session -t {SESSION} 2>/dev/null")
    time.sleep(0.3)
    # 启动 session，cat 输出到临时文件
    run(f"tmux new-session -d -s {SESSION} 'cat > {OUTPUT_FILE}'")
    time.sleep(0.3)


def teardown_session():
    run(f"tmux kill-session -t {SESSION} 2>/dev/null")
    if os.path.exists(OUTPUT_FILE):
        os.unlink(OUTPUT_FILE)


def send_and_capture(content: str, use_literal: bool = True) -> str:
    """通过 send-keys 注入 content（加回车），返回 cat 实际输出的内容"""
    # 记录当前文件大小，只读取新增内容（避免截断文件导致 cat fd 偏移错误）
    before_size = os.path.getsize(OUTPUT_FILE) if os.path.exists(OUTPUT_FILE) else 0

    escaped = content.replace("'", "'\\''")
    if use_literal:
        # -l 标志下，内容和 Enter 必须分两次发送
        # 合并写成 send-keys -l 'content' Enter 时，Enter 会被当作字面量 "Enter" 发送
        tmux(f"send-keys -t {SESSION} -l '{escaped}'")
        tmux(f"send-keys -t {SESSION} Enter")
    else:
        tmux(f"send-keys -t {SESSION} '{escaped}' Enter")
    time.sleep(0.4)  # 等待 cat 处理

    with open(OUTPUT_FILE) as f:
        f.seek(before_size)
        return f.read()


def test_case(label: str, content: str, use_literal: bool = True):
    """运行一个测试用例，比对输入和输出"""
    expected = content + "\n"
    actual = send_and_capture(content, use_literal)

    ok = actual == expected
    flag_str = "-l" if use_literal else "no flag"
    status = "✅" if ok else "❌"
    log(f"  {status} [{flag_str}] {label}")
    if not ok:
        log(f"      期望: {repr(expected)}")
        log(f"      实际: {repr(actual)}")
    return ok


# ─────────────────────────────────────────────
# 测试用例组
# ─────────────────────────────────────────────

TEST_CASES = [
    # (label, content)
    ("ASCII 普通文本",        "hello world"),
    ("中文字符",              "你好世界"),
    ("中英混合",              "check作业 today"),
    ("美元符号",              "price is $100"),
    ("反引号",                "run `date`"),
    ("感叹号",                "hello!"),
    ("双引号",                'say "hello"'),
    ("单引号",                "it's fine"),
    ("反斜杠",                "path\\to\\file"),
    ("换行符转义",            "line1\\nline2"),
    ("方括号",                "[14:30 telegram] msg"),
    ("花括号 JSON 格式",      '{"channel":"tg","content":"hi"}'),
    ("星号通配符",            "file*.txt"),
    ("管道符",                "echo foo | cat"),
    ("分号",                  "cmd1; cmd2"),
]


def run_all_tests():
    log("\n=== 使用 -l (literal) 标志 ===")
    literal_results = [test_case(label, content, use_literal=True)
                       for label, content in TEST_CASES]

    log("\n=== 不使用 -l 标志（观察是否有 shell 解释） ===")
    noliteral_results = [test_case(label, content, use_literal=False)
                         for label, content in TEST_CASES]

    passed_literal   = sum(literal_results)
    passed_noliteral = sum(noliteral_results)

    log(f"\n  -l 标志:    {passed_literal}/{len(TEST_CASES)} 通过")
    log(f"  无 -l 标志: {passed_noliteral}/{len(TEST_CASES)} 通过")

    if passed_literal > passed_noliteral:
        log("\n  结论: ✅ 应使用 -l 标志注入，避免 shell 解释特殊字符")
    elif passed_literal == passed_noliteral == len(TEST_CASES):
        log("\n  结论: ✅ 两种方式均可，建议保守使用 -l")
    else:
        log("\n  结论: ⚠️  需人工核查差异")


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # 检查 tmux 是否可用
    if run("which tmux").returncode != 0:
        print("❌ tmux 未安装，无法运行 SP-04")
        sys.exit(1)

    log("SP-04: tmux send-keys 注入行为验证")
    log(f"Python {sys.version}")

    try:
        setup_session()
        run_all_tests()
    finally:
        teardown_session()

    log("\n=== 需要固化到 spec.md 的结论 ===")
    log("  [ ] 是否需要 -l 标志")
    log("  [ ] 需要特殊处理的字符集（如有）")

    with open(RESULTS_FILE, "w") as f:
        f.write("\n".join(results) + "\n")
    log(f"\n结果已写入: {RESULTS_FILE}")
