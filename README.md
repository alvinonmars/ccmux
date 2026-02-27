# ccmux

> **WARNING: ACTIVE DEVELOPMENT — HIGHLY UNSTABLE**
>
> This project is under heavy active development. APIs, file structures, and behavior change frequently without notice. **Do not use in production.** Expect breaking changes on every commit.

A multiplexer for the Claude Code CLI. Wraps `claude` in a tmux session with standard Unix I/O interfaces so it can run 24/7 and accept input from multiple asynchronous sources (WhatsApp, Telegram, timers, etc.) while keeping the native terminal experience fully intact.

## Privacy Gate — MANDATORY

> **This project processes personal and family data paths. Every commit MUST pass a two-layer privacy gate. No exceptions.**

### How it works

**Layer 1 — Regex scan (automatic):** The pre-commit hook runs `scripts/privacy_check.py` which scans all staged changes against:
- Generic PII patterns: phone numbers, WhatsApp JIDs, email addresses, credentials, home paths
- Personal blocklist: names, domains, and custom patterns from `~/.ccmux/secrets/privacy_blocklist.txt` (gitignored)

**Layer 2 — AI review token (manual):** After the regex scan passes, 3 independent Claude Code agents review the staged diff from different perspectives (Identity, Secrets, Context). All 3 must pass before a one-time review token is generated. The pre-commit hook verifies this token — **no token = commit blocked**.

### Setup (required for all contributors)

```bash
# 1. Activate the pre-commit hook
git config core.hooksPath hooks

# 2. Create your personal blocklist
mkdir -p ~/.ccmux/secrets
cat > ~/.ccmux/secrets/privacy_blocklist.txt << 'EOF'
# Add names, paths, domains, or any string that must never appear in a commit
# One pattern per line, matched case-insensitively as word boundaries
EOF
```

### Commit workflow

```bash
git add <files>

# Run the privacy scanner + AI review to generate a token
python scripts/privacy_check.py --generate-token

# Commit (hook verifies the token automatically)
git commit -m "your message"
```

If the hook blocks your commit, it will print the exact matches found. Fix them and retry. **Never bypass the hook** (`--no-verify` is not allowed).

### History

The entire git history has been cleaned with `git filter-repo` to remove all PII. Maintaining a clean history is a hard requirement — the privacy gate exists to ensure no PII is ever re-introduced.

### Quick reference

| Command | Purpose |
|---------|---------|
| `python scripts/privacy_check.py` | Scan staged changes only |
| `python scripts/privacy_check.py --all` | Full repo scan |
| `python scripts/privacy_check.py --generate-token` | AI review + generate commit token |
| `python scripts/privacy_check.py --review` | Print staged diff for manual review |

## Why ccmux

I'm a heavy Claude Code user. My philosophy: use the best model on the best engineering foundation. The model's capability sets the ceiling; the engineering infrastructure sets the floor. That's why I build my AI agents on top of Claude Code rather than raw API calls or thin wrappers.

There's also a practical reason: the Anthropic API is expensive. Claude Code with a Claude Max subscription gives you the same Opus/Sonnet models at a flat rate — but only through the CLI, which assumes a human at the keyboard. ccmux removes that assumption.

The goal: let Claude Code run natively (no API shim, no prompt wrapper, no capability loss) while gaining 24/7 heartbeat capability — always on, accepting input from any channel, surviving reboots, and auto-recovering from crashes. A persistent AI agent that uses the full Claude Code experience as its runtime.

## How It Works

```
Adapters (Layer 3)                   ccmux daemon (Layer 2)
  wa-notifier    ──▶ /tmp/ccmux/in.whatsapp ─┐
  telegram-bot   ──▶ /tmp/ccmux/in.telegram ──┤
  systemd timer  ──▶ /tmp/ccmux/in           ─┤
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

> **IMPORTANT: ccmux requires a running Claude Code session to function. It is NOT a standalone application — it is infrastructure that wraps the Claude Code CLI.**

| Requirement | Why |
|-------------|-----|
| **Claude Code CLI** | Core runtime — must be installed, authenticated, and working (`claude` command in PATH) |
| **Claude Max subscription** | Provides flat-rate access to Opus/Sonnet models. ccmux runs 24/7 and spawns many agent tasks — API billing would be extremely expensive |
| **Python 3.11+** | Daemon, adapters, and all scripts |
| **tmux** | Session management — ccmux wraps Claude Code in a persistent tmux session |
| **Node.js** | Required by the Claude Code CLI |
| **Linux (systemd)** | Deployment uses systemd user services. macOS/WSL may work but are untested |

## Install

```bash
git clone <repo-url> && cd ccmux
python -m venv .venv
.venv/bin/pip install -e .
git config core.hooksPath hooks    # activate pre-commit privacy scanner
```

This creates three entry points: `ccmux` (daemon), `ccmux-wa-notifier` (WhatsApp adapter), and `ccmux-deploy` (timer/service reconciler).
The `core.hooksPath` line activates the two-layer pre-commit privacy gate — see [Privacy Gate](#privacy-gate--mandatory) above.

## Configuration

Copy the example config and customize:

```bash
cp ccmux.toml.example ccmux.toml
```

`ccmux.toml` is gitignored (contains machine-specific paths). Edit it:

```toml
[project]
name = "my-project"          # tmux session: ccmux-{name}

[runtime]
dir = "/tmp/ccmux"           # FIFOs and sockets

[timing]
idle_threshold = 30          # seconds of terminal inactivity before auto-injection
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
poll_interval = 5
ignore_groups = true
classify_enabled = false     # enable AI classifier for household group
smart_classify_chats = []    # chat JIDs that use AI classification

[services]                   # managed services verified by ccmux-deploy
managed = ["whatsapp-bridge", "ccmux", "ccmux-wa-notifier"]
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
Layer 1: whatsapp-bridge.service       External dependency (optional)
Layer 2: ccmux.service                 Core daemon (After/Wants bridge)
Layer 3: ccmux-wa-notifier.service     Adapter (After/Requires ccmux)
Oneshot: ccmux-reconcile.service       Syncs ccmux.toml timers → systemd (runs at boot)
Timers:  ccmux-*.timer                 Generated by ccmux-deploy from [timers] in ccmux.toml
Group:   ccmux.target                  Controls entire stack
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

## Operations

### Quick Reference

```bash
# Start/stop the entire stack
systemctl --user start ccmux.target
systemctl --user stop ccmux.target

# Restart just the daemon (wa-notifier auto-restarts via Requires=)
systemctl --user restart ccmux

# Restart the full stack
systemctl --user restart ccmux.target

# Health check (services, timers, tmux, proxy)
ccmux-deploy verify

# View logs
journalctl --user -u ccmux -f                    # follow daemon logs
journalctl --user -u ccmux-wa-notifier -n 50      # last 50 notifier lines
journalctl --user -u whatsapp-bridge --boot       # bridge logs since boot
journalctl --user -u 'ccmux*' -f                  # unified log stream (all units)

# Check status
systemctl --user status ccmux.target whatsapp-bridge ccmux ccmux-wa-notifier
```

### Boot Sequence

On system boot (or `systemctl --user start ccmux.target`), services start in this order:

```
1. ccmux.target activates
   ├── 2. ccmux-reconcile.service (oneshot)
   │       → Syncs ccmux.toml [timers] → systemd .timer/.service files
   │       → Enables and starts all declared timers
   │       → Removes orphan timer units no longer in ccmux.toml
   │
   ├── 3. whatsapp-bridge.service
   │       → Go binary, manages WhatsApp Web connection
   │
   ├── 4. ccmux.service (After=whatsapp-bridge)
   │       → Daemon startup sequence:
   │         a. Configure logging
   │         b. Install Claude Code hooks (stop hook for output capture)
   │         c. Start MCP SSE server on 127.0.0.1:{mcp.port}
   │         d. Write .mcp.json with MCP server URL
   │         e. Start output broadcaster (output.sock) and control server
   │         f. Start FIFO manager + directory watcher (inotify)
   │         g. Create/attach tmux session, launch `claude --dangerously-skip-permissions`
   │         h. Mount pipe-pane for stdout monitoring
   │         i. Start LifecycleManager (crash detection + auto-restart)
   │
   ├── 5. ccmux-wa-notifier.service (After=ccmux, Requires=ccmux)
   │       → Polls whatsapp-bridge SQLite DB for new messages
   │       → Loads S3 whitelist from contacts.json (permission gate)
   │       → Classifies messages (admin self-chat, monitored groups, contacts)
   │       → Filters bot echo loops via BOT_PREFIXES (admin self-chat only)
   │       → Writes JSON to /tmp/ccmux/in.whatsapp FIFO
   │
   └── 6. ccmux-selfcheck.timer (OnActiveSec=30s)
           → Runs startup_selfcheck.py 30s after boot
           → Sends system health report to Claude via [butler] FIFO
           → Claude forwards the report to admin via WhatsApp
```

### MCP Auto-Reconnection

The daemon writes `.mcp.json` on startup (step 4d above). Claude Code detects `.mcp.json` changes and auto-reconnects to MCP servers. **No manual MCP reconnection is needed after restart.**

If Claude was already running when the daemon restarts (e.g., `systemctl --user restart ccmux`), the daemon attaches to the existing tmux session rather than creating a new one. The `.mcp.json` is re-written, and Claude picks up the new MCP URL automatically.

### Crash Recovery (LifecycleManager)

The LifecycleManager monitors the Claude process inside the tmux pane every 2 seconds. If Claude crashes or exits:

1. Detects crash via PID check + pane capture fallback
2. Waits with exponential backoff: `min(initial * 2^N, cap)` seconds (default: 1s initial, 60s cap)
3. Restarts Claude with `claude --dangerously-skip-permissions --continue` (preserves conversation history)
4. Re-mounts pipe-pane for stdout monitoring

The restart count never resets — even after days of stable operation, the next crash immediately uses the capped backoff. This is intentional for a 24/7 daemon to prevent rapid restart storms.

### Timer Management

All scheduled tasks are defined in `ccmux.toml` under `[timers.<name>]`. **Never use cron or create .timer files manually.**

#### Adding a timer

Add to `ccmux.toml`:

```toml
[timers.my-task]
description = "What this does"
schedule = "*-*-* 08:00:00"          # systemd OnCalendar syntax
exec = ".venv/bin/python3 scripts/my_script.py"
syslog = "ccmux-my-task"
env = { MY_VAR = "value" }           # optional
```

Then apply:

```bash
ccmux-deploy
```

This runs `ccmux/reconcile.py` which:
- Generates `~/.config/systemd/user/ccmux-my-task.{timer,service}`
- Runs `systemctl --user daemon-reload`
- Enables and starts the timer
- Removes orphan timers no longer in ccmux.toml

#### Timer schedule syntax

Uses systemd `OnCalendar` format (not cron). Use a list for multiple triggers:

```toml
schedule = ["*-*-* 07:00:00", "*-*-* 20:00:00"]   # 7am and 8pm daily
```

For one-shot delays after boot:

```toml
startup_delay = "30s"     # fires 30s after ccmux.target starts
```

#### Listing timers

```bash
systemctl --user list-timers 'ccmux-*'     # show all ccmux timers with next trigger
ccmux-deploy verify                         # full health check
```

### Post-Restart Checklist

After restarting the stack (`systemctl --user restart ccmux.target`):

1. **Automatic** — No manual steps needed:
   - `ccmux-reconcile` syncs timers from ccmux.toml
   - Daemon writes `.mcp.json`, Claude auto-reconnects MCP
   - LifecycleManager monitors Claude process health
   - wa-notifier reconnects to bridge DB and resumes polling
   - Self-check timer fires 30s after boot, sends health report

2. **Verify** (optional, for peace of mind):
   ```bash
   ccmux-deploy verify                        # services + timers + tmux + proxy
   systemctl --user status ccmux.target       # quick stack status
   tmux attach -t ccmux-<project-name>        # visual check of Claude session
   ```

3. **Check for missed messages** (if downtime was significant):
   - The startup self-check reports the gap between last scan and current time
   - Claude automatically scans for missed messages during the downtime window

### Adding a New Adapter

1. Write `~/.config/systemd/user/ccmux-<name>.service` with:
   - `After=ccmux.service` / `Requires=ccmux.service`
   - `WantedBy=ccmux.target`
   - `PartOf=ccmux.target` (so `stop ccmux.target` cascades)
2. `systemctl --user daemon-reload && systemctl --user enable --now ccmux-<name>`

No changes to ccmux core required. The adapter writes JSON to a FIFO under `/tmp/ccmux/in.<name>`, which the daemon auto-discovers via inotify.

### Data Directory Layout

All privacy-sensitive data lives outside the repo under `~/.ccmux/`:

```
~/.ccmux/
├── data/                           # CCMUX_DATA_DIR (overridable via env)
│   ├── household/
│   │   ├── family_context.jsonl    # accumulated family knowledge
│   │   ├── chat_history.jsonl      # household group message log
│   │   ├── butler/                 # butler state (last_scan.json, etc.)
│   │   │   └── announcements/
│   │   ├── health/                 # per-child health tracking
│   │   ├── homework/               # school homework screenshots
│   │   ├── receipts/               # expense tracking
│   │   └── tmp/                    # working files
│   │       └── email_scan/         # School email scanner screenshots
│   ├── contacts/                   # per-contact chat history + diet logs
│   ├── daily_reflections/          # end-of-day AI reflection logs
│   ├── security_audit/
│   └── contacts.json               # contact registry + S3 whitelist permissions
│
└── secrets/                        # CCMUX_SECRETS_DIR (overridable via env)
    └── powerschool.env             # PowerSchool credentials
```

All scripts import paths from `ccmux/paths.py` — the single source of truth for data locations. Override via environment variables: `CCMUX_DATA_DIR`, `CCMUX_SECRETS_DIR`.

### S3 Whitelist (Permission Gate)

The wa-notifier enforces a whitelist on S3 command handling. Only messages from explicitly approved chat JIDs are classified as `S3_COMMAND`; messages from non-whitelisted chats with an S3 prefix are downgraded to `UNKNOWN` (Claude sees the raw text but does not treat it as an S3 command).

The whitelist is stored in `~/.ccmux/data/contacts.json` under `permissions.s3_whitelist`:

```json
{
  "contacts": [ ... ],
  "permissions": {
    "s3_whitelist": [
      "<phone>@s.whatsapp.net",
      "<group-id>@g.us"
    ]
  }
}
```

- JIDs are chat-level: `@s.whatsapp.net` for 1:1 chats, `@g.us` for groups
- Empty or missing whitelist = all S3 commands pass through (backward compatible)
- Changes take effect on wa-notifier restart

### Troubleshooting

**Claude not responding to injected messages:**
```bash
# Check if daemon is running and injecting
journalctl --user -u ccmux -n 30
# Look for: "injection suppressed: terminal active" → detach tmux first
# Look for: "injection suppressed: permission prompt" → approve in tmux
# Look for: "injection suppressed: Claude is generating" → wait

# Check if FIFOs exist
ls -la /tmp/ccmux/in*
```

**wa-notifier crash loop:**
```bash
journalctl --user -u ccmux-wa-notifier -n 50
# Common causes: bridge DB locked, port conflict, FIFO deleted
# Fix: restart the full stack
systemctl --user restart ccmux.target
```

**MCP tools not available in Claude:**
```bash
# Check .mcp.json exists and has correct URL
cat .mcp.json
# Check MCP server is listening
curl -s http://127.0.0.1:9876/sse
# If daemon just restarted, wait ~10s for Claude to auto-detect .mcp.json change
```

**Timers not firing:**
```bash
systemctl --user list-timers 'ccmux-*'          # check next trigger times
journalctl --user -u ccmux-<timer-name> -n 10   # check timer service logs
ccmux-deploy                                      # re-sync from ccmux.toml
```

**Claude process keeps crashing:**
```bash
# Check lifecycle manager logs
journalctl --user -u ccmux | grep -i restart
# Check tmux pane for errors
tmux capture-pane -t ccmux-<project> -p | tail -20
# Common causes: auth expired (re-login), proxy down, API issues
```

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

This gives you the native Claude Code experience. Your keyboard activity suppresses auto-injection (configurable via `idle_threshold`). Detach with `Ctrl-b d` to let auto-injection resume.

## Development

```bash
.venv/bin/pip install -e ".[dev]"

# Run tests (no proxy needed)
.venv/bin/python -m pytest tests/ -m "not real_claude" -v

# Run with real Claude (requires proxy + Claude Max auth)
.venv/bin/python -m pytest tests/ -m real_claude -v
```

See `docs/spec.md` for architecture details and `docs/acceptance-criteria.md` for test coverage.

## Disclaimer

This is a personal project built for the author's own use. It is provided as-is, with no warranty of any kind, express or implied.

- **No guarantee of correctness, reliability, or fitness for any particular purpose.** The software may contain bugs, break without notice, or behave unexpectedly.
- **You are solely responsible** for any consequences of running this software, including but not limited to data loss, privacy exposure, or system damage.
- **This project processes personal and family data.** If you fork or adapt it, you are responsible for your own data privacy and compliance with applicable laws (GDPR, PDPO, etc.).
- **No support is provided.** Issues and PRs may be ignored or closed without explanation.
- **The author is not liable** for any direct, indirect, incidental, or consequential damages arising from the use of this software.

Use at your own risk.
