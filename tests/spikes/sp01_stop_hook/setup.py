#!/usr/bin/env python3
"""
SP-01 配置脚本 —— 把 hook.py 注册到 Claude Code

运行: python3 setup.py [install|uninstall|status]

install   : 在 ~/.claude/settings.json 中注册 Stop hook
uninstall : 移除 Stop hook
status    : 显示当前 hook 配置和捕获目录内容
"""

import json
import os
import sys
import glob

HOOK_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "hook.py"))
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
CAPTURE_DIR = "/tmp/ccmux_sp01_capture"


def load_settings():
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    return {}


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    print(f"✅ 已写入 {SETTINGS_PATH}")


def install():
    settings = load_settings()
    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])

    hook_entry = {
        "matcher": "",
        "hooks": [{"type": "command", "command": f"python3 {HOOK_SCRIPT}"}]
    }

    # 检查是否已存在
    existing = [h for h in stop_hooks if HOOK_SCRIPT in str(h)]
    if existing:
        print("⚠️  hook 已存在，跳过")
        return

    stop_hooks.append(hook_entry)
    save_settings(settings)
    print(f"✅ hook 已注册: {HOOK_SCRIPT}")
    print(f"   捕获目录: {CAPTURE_DIR}")
    print()
    print("下一步:")
    print("  1. 启动 Claude Code 进行 1-2 轮对话（包括触发一次工具调用）")
    print("  2. 运行: python3 analyze.py")


def uninstall():
    settings = load_settings()
    hooks = settings.get("hooks", {})
    stop_hooks = hooks.get("Stop", [])
    before = len(stop_hooks)
    hooks["Stop"] = [h for h in stop_hooks if HOOK_SCRIPT not in str(h)]
    after = len(hooks["Stop"])
    save_settings(settings)
    print(f"✅ 已移除 {before - after} 个 hook 条目")


def status():
    settings = load_settings()
    stop_hooks = settings.get("hooks", {}).get("Stop", [])
    registered = [h for h in stop_hooks if HOOK_SCRIPT in str(h)]
    print(f"hook 注册状态: {'✅ 已注册' if registered else '❌ 未注册'}")
    print(f"捕获目录: {CAPTURE_DIR}")

    captures = sorted(glob.glob(os.path.join(CAPTURE_DIR, "capture_*.json")))
    print(f"已捕获次数: {len(captures)}")
    if captures:
        print(f"最新捕获: {captures[-1]}")
        summary = os.path.join(CAPTURE_DIR, "summary.txt")
        if os.path.exists(summary):
            print("\n── summary.txt ──")
            with open(summary) as f:
                print(f.read()[-3000:])  # 最后 3000 字符


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"install": install, "uninstall": uninstall, "status": status}.get(cmd, status)()
