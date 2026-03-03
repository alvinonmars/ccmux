# ccmux instance environment template
# Loaded by process_mgr.py on every instance creation.
# STREAM_NAME and TOPIC_NAME are substituted at runtime.

# Network proxy (required — machine routes through surfshark-gluetun VPN)
export HTTP_PROXY=http://127.0.0.1:8118
export HTTPS_PROXY=http://127.0.0.1:8118
export NO_PROXY=localhost,127.0.0.1

# Zulip output routing (derived from directory names by process_mgr.py)
export ZULIP_STREAM=${STREAM_NAME}
export ZULIP_TOPIC=${TOPIC_NAME}
export ZULIP_SITE=http://127.0.0.1:9900
export ZULIP_BOT_EMAIL=ccmux-bot-bot@zulip.alvindesign.org
export ZULIP_BOT_API_KEY_FILE=~/.ccmux/secrets/zulip_bot.env
