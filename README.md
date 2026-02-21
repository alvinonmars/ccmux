# ccmux

A multiplexer for the Claude Code CLI. Wraps `claude` in a tmux session with standard Unix I/O interfaces so it can run 24/7 and accept input from multiple asynchronous sources (WhatsApp, Telegram, timers, etc.) while keeping the native terminal experience fully intact.

## How It Works

```
Adapters (Layer 3)                   ccmux daemon (Layer 2)
  wa-notifier  ──▶ /tmp/ccmux/in.whatsapp ─┐
  telegram-bot ──▶ /tmp/ccmux/in.telegram ──┤
  cron job     ──▶ /tmp/ccmux/in           ─┤
                                             ▼
                                     ┌─────────────┐
                                     │ ccmux daemon │──▶ tmux send-keys ──▶ Claude Code
                                     │              │◀── stop hook ◀────── Claude Code
                                     │              │──▶ output.sock ──▶ all subscribers
                                     └─────────────┘
                                             ▲
  Terminal ──── tmux attach ─────────────────┘  (native, unmodified)
```

- **Input**: adapters write JSON to named FIFOs (`in.*`); daemon merges and injects into Claude via `tmux send-keys`
- **Output**: stop hook reads transcript, sends to `output.sock`; all subscribers receive the complete turn
- **Routing**: Claude calls `send_to_channel(channel, message)` MCP tool to write to specific output FIFOs (`out.*`)
- **Discovery**: filesystem-based — adapters create/remove FIFOs; daemon auto-discovers via inotify

## Prerequisites

- Python 3.11+
- tmux
- Node.js (for `claude` CLI — must be in PATH)
- Claude Code CLI installed and authenticated (`claude` command works)

## Install

```bash
git clone <repo-url> && cd ccmux
python -m venv .venv
.venv/bin/pip install -e .
```

This creates two entry points: `ccmux` (daemon) and `ccmux-wa-notifier` (WhatsApp adapter).

## Configuration

Create `ccmux.toml` in the project root:

```toml
[project]
name = "my-project"          # tmux session: ccmux-{name}

[runtime]
dir = "/tmp/ccmux"           # FIFOs and sockets

[timing]
idle_threshold = 5           # seconds of terminal inactivity before auto-injection
silence_timeout = 3          # seconds of stdout silence = Claude is ready

[mcp]
port = 9876                  # MCP SSE server (loopback only)

[claude]
proxy = ""                   # HTTP proxy for Claude API (empty = none)

[recovery]
backoff_initial = 1          # crash restart initial delay (seconds)
backoff_cap = 60             # max restart delay

[whatsapp]                   # only needed if using wa-notifier
db_path = "/path/to/whatsapp-bridge/store/messages.db"
poll_interval = 30
ignore_groups = true
```

All fields have sensible defaults. The file is optional for basic usage.

## Running

### Manual (foreground)

```bash
cd /path/to/project-with-ccmux-toml
.venv/bin/ccmux
```

### systemd (recommended for persistent deployment)

ccmux uses systemd user services — no root required, survives logout, auto-restarts on crash.

#### Service architecture

```
Layer 1: whatsapp-bridge.service     External dependency (optional)
Layer 2: ccmux.service               Core daemon (After/Wants bridge)
Layer 3: ccmux-wa-notifier.service   Adapter (After/Requires ccmux)
Group:   ccmux.target                Controls entire stack
```

#### Setup

1. **Enable linger** (one-time, requires sudo):

```bash
sudo loginctl enable-linger $USER
```

This lets your user services run after logout and start at boot.

2. **Create unit files** in `~/.config/systemd/user/`:

`whatsapp-bridge.service` (skip if not using WhatsApp):
```ini
[Unit]
Description=WhatsApp Bridge (whatsapp-mcp Go binary)

[Service]
Type=simple
WorkingDirectory=/path/to/whatsapp-bridge
ExecStart=/path/to/whatsapp-bridge/whatsapp-bridge
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=whatsapp-bridge

[Install]
WantedBy=ccmux.target
```

`ccmux.service`:
```ini
[Unit]
Description=ccmux daemon (Claude Code multiplexer)
After=whatsapp-bridge.service
Wants=whatsapp-bridge.service

[Service]
Type=simple
WorkingDirectory=/path/to/project-with-ccmux-toml
ExecStart=/path/to/project/.venv/bin/ccmux
Environment=PATH=/path/to/node/bin:/path/to/project/.venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/youruser
Environment=TERM=xterm-256color
Restart=on-failure
RestartSec=5
TimeoutStartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ccmux

[Install]
WantedBy=ccmux.target
```

Key notes:
- **PATH must include node**: daemon runs `claude` via tmux send-keys; the tmux pane inherits the daemon's PATH
- **TERM=xterm-256color**: tmux needs a terminal type when started without a TTY
- **WorkingDirectory**: must point to the directory containing `ccmux.toml`

`ccmux-wa-notifier.service` (skip if not using WhatsApp):
```ini
[Unit]
Description=ccmux WhatsApp notifier adapter
After=ccmux.service whatsapp-bridge.service
Requires=ccmux.service
Wants=whatsapp-bridge.service

[Service]
Type=simple
WorkingDirectory=/path/to/project-with-ccmux-toml
ExecStart=/path/to/project/.venv/bin/ccmux-wa-notifier
Environment=HOME=/home/youruser
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ccmux-wa-notifier

[Install]
WantedBy=ccmux.target
```

`ccmux.target`:
```ini
[Unit]
Description=ccmux stack (daemon + adapters)

[Install]
WantedBy=default.target
```

3. **Enable and start**:

```bash
systemctl --user daemon-reload
systemctl --user enable ccmux.target whatsapp-bridge ccmux ccmux-wa-notifier
systemctl --user start ccmux.target
```

#### Operations

```bash
# Start/stop the entire stack
systemctl --user start ccmux.target
systemctl --user stop ccmux.target

# Restart just the daemon (wa-notifier auto-restarts via Requires=)
systemctl --user restart ccmux

# View logs
journalctl --user -u ccmux -f                    # follow daemon logs
journalctl --user -u ccmux-wa-notifier -n 50      # last 50 notifier lines
journalctl --user -u whatsapp-bridge --boot       # bridge logs since boot

# Check status
systemctl --user status ccmux.target whatsapp-bridge ccmux ccmux-wa-notifier
```

#### Adding a new adapter

1. Write `~/.config/systemd/user/ccmux-<name>.service` with:
   - `After=ccmux.service` / `Requires=ccmux.service`
   - `WantedBy=ccmux.target`
2. `systemctl --user daemon-reload && systemctl --user enable --now ccmux-<name>`

No changes to ccmux core required.

## Sending Messages to Claude

Write JSON to any `in.*` FIFO:

```bash
echo '{"channel":"test","content":"hello","ts":1234567890}' > /tmp/ccmux/in
```

Messages are queued and injected when Claude is idle, formatted as `[HH:MM channel] content`.

## Attaching to the Terminal

```bash
tmux attach -t ccmux-my-project
```

This gives you the native Claude Code experience. Your keyboard activity suppresses auto-injection (configurable via `idle_threshold`).

## Development

```bash
.venv/bin/pip install -e ".[dev]"

# Run tests (no proxy needed)
.venv/bin/python -m pytest tests/ -m "not real_claude" -v

# Run with real Claude (requires proxy + Claude Max auth)
.venv/bin/python -m pytest tests/ -m real_claude -v
```

See `docs/spec.md` for architecture details and `docs/acceptance-criteria.md` for test coverage.
