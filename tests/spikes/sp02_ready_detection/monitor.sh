#!/usr/bin/env bash
# SP-02: Claude Code stdout 提示符捕获
#
# 原理: 在新 tmux session 中启动 Claude Code，
#       pipe-pane -O 把所有 stdout 写到文件，
#       用户正常交互后 Ctrl-C 停止，再运行 analyze.py 分析。
#
# 运行: bash monitor.sh
# 停止: 在 Claude Code 中正常退出，或 Ctrl-C

SESSION="ccmux_sp02"
CAPTURE_FILE="/tmp/ccmux_sp02_stdout.raw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "SP-02: Claude Code stdout 提示符捕获"
echo "捕获文件: $CAPTURE_FILE"
echo ""

# 清理旧 session
tmux kill-session -t "$SESSION" 2>/dev/null
rm -f "$CAPTURE_FILE"

# 创建新 session，启动 Claude Code
tmux new-session -d -s "$SESSION" "claude"
sleep 1

# 挂载 pipe-pane：捕获 pane stdout 到文件
# -O 表示只捕获输出（pane → terminal 方向）
tmux pipe-pane -t "$SESSION" -O "cat >> $CAPTURE_FILE"

echo "✅ 监控已启动"
echo ""
echo "请在新终端中执行:"
echo "  tmux attach -t $SESSION"
echo ""
echo "然后:"
echo "  1. 进行 2-3 轮普通对话"
echo "  2. 让 Claude 执行一个需要权限的操作（如写文件、运行命令）"
echo "     触发 permission prompt，观察它的样子"
echo "  3. 退出 Claude Code (输入 /quit 或 Ctrl-D)"
echo ""
echo "完成后运行:"
echo "  python3 $SCRIPT_DIR/analyze.py"
echo ""
echo "按 Enter 在此等待，或 Ctrl-C 退出监控..."
read -r

# 停止捕获
tmux pipe-pane -t "$SESSION" 2>/dev/null
tmux kill-session -t "$SESSION" 2>/dev/null
echo "监控已停止，捕获文件: $CAPTURE_FILE"
echo "运行 python3 $SCRIPT_DIR/analyze.py 查看分析"
