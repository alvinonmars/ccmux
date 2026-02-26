#!/bin/bash
# Switch voice bridge between normal and interview mode
# Usage: bash scripts/voice_mode.sh interview   — switch to interview prompt
#        bash scripts/voice_mode.sh normal      — switch back to default prompt

UNIT="ccmux-voice-bridge.service"
OVERRIDE_DIR="$HOME/.config/systemd/user/${UNIT}.d"
OVERRIDE_FILE="$OVERRIDE_DIR/prompt.conf"
INTERVIEW_PROMPT="$HOME/.ccmux/data/voice/prompts/interview.txt"

case "${1:-status}" in
    interview)
        mkdir -p "$OVERRIDE_DIR"
        cat > "$OVERRIDE_FILE" <<EOF
[Service]
Environment=VOICE_PROMPT_FILE=$INTERVIEW_PROMPT
EOF
        systemctl --user daemon-reload
        systemctl --user restart "$UNIT"
        echo "Switched to INTERVIEW mode (prompt: $INTERVIEW_PROMPT)"
        ;;
    normal)
        rm -f "$OVERRIDE_FILE"
        rmdir "$OVERRIDE_DIR" 2>/dev/null || true
        systemctl --user daemon-reload
        systemctl --user restart "$UNIT"
        echo "Switched to NORMAL mode (default prompt)"
        ;;
    status)
        if [ -f "$OVERRIDE_FILE" ]; then
            echo "Mode: INTERVIEW"
            cat "$OVERRIDE_FILE"
        else
            echo "Mode: NORMAL (default prompt)"
        fi
        systemctl --user status "$UNIT" --no-pager -l
        ;;
    *)
        echo "Usage: $0 {interview|normal|status}"
        exit 1
        ;;
esac
