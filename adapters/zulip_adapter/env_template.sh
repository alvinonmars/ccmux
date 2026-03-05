# ccmux instance environment template
# Loaded by process_mgr.py on every instance creation.
# STREAM_NAME and TOPIC_NAME are substituted at runtime.
#
# NOTE: Proxy env vars (HTTP_PROXY etc.) are NOT set here.
# Only the Claude Code process needs a proxy (for Anthropic API).
# The proxy is injected as a command prefix on the claude send-keys
# invocation in process_mgr.py, so relay hooks and other child
# processes in the tmux session are never affected.

# Zulip output routing (derived from directory names by process_mgr.py)
export ZULIP_STREAM=${STREAM_NAME}
export ZULIP_TOPIC=${TOPIC_NAME}
export ZULIP_SITE=http://127.0.0.1:9900
export ZULIP_BOT_EMAIL=ccmux-bot-bot@zulip.alvindesign.org
export ZULIP_BOT_API_KEY_FILE=~/.ccmux/secrets/zulip_bot.env
