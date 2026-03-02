# Zulip Multi-Session Implementation Plan (v8)

> Status: **Design v8** (2026-03-02). Stream/topic instance model, per-topic Claude sessions, directory-based config, topic routing via env vars.

## Design Principles

1. **WhatsApp = admin control plane**, Zulip = project development windows
2. **Lazy creation** — instance starts only when first message arrives in a topic
3. **Minimal new components** — each with a single clear responsibility
4. **No private data dirs for projects** — project directory is everything
5. **Recovery via config** — on restart, `~/.ccmux/streams/` survives; instances recreated lazily
6. **Task/session management stays in WhatsApp AI assistant's context**
7. **Project Claude Code is unaware of Zulip** — output captured by Stop hook, not by Claude actively sending
8. **Project independence** — projects are standalone; ccmux-init bootstraps minimal integration without polluting the project
9. **Zero changes to existing code** — daemon, wa_notifier, hook.py all untouched
10. **Binary equality, organizational hierarchy** — all instances share the same underlying structure; differences are purely configurational (see Instance Architecture Model)
11. **Use Zulip's own concepts** — directory structure follows Zulip's stream/topic model; no separate "project" abstraction

## Instance Architecture Model

All ccmux instances — whether the WhatsApp personal assistant or a Zulip project developer — share the same underlying structure at the binary level:

```
Instance = Adapter → FIFO → Injector → tmux (Claude Code) → Hook → Channel
```

The differences between instance types are purely configurational:

| Dimension | Configuration |
|-----------|---------------|
| **Adapter** | Which adapter feeds the FIFO (wa_notifier, zulip_adapter) |
| **Hooks** | Which Stop hooks are installed (output broadcast, Zulip relay, or both) |
| **MCP tools** | Which MCP servers are available (WhatsApp, Futu, etc.) — configured via `.mcp.json` |
| **CLAUDE.md** | Behavioral protocol (assistant vs developer vs hybrid) |
| **Environment** | Channel-specific env vars (Zulip stream/topic, proxy, etc.) |

**Organizational hierarchy**: Despite binary equality, instances have a control plane / data plane relationship:

- **Control plane** = WhatsApp assistant instance. Creates/deletes streams and projects, manages lifecycle, routes admin intent, holds personal data and family context.
- **Data plane** = Project topic instances. Execute specific tasks, report output via hooks. Capabilities (including WhatsApp access) determined by per-instance configuration.

**Capabilities are configuration, not architecture**: Whether an instance has WhatsApp MCP, access to specific data, or other tools is a configuration choice in `stream.toml` and `.mcp.json` — not a hard architectural constraint.

## Stream/Topic Instance Model

Mapping Zulip's concepts directly to ccmux's instance management:

```
Zulip stream  = project (ccmux-dev, ipo-analysis)
Zulip topic   = Claude Code session (fix-auth-bug, add-tests)

1 stream + 1 topic = 1 Claude Code instance
```

Same project, multiple topics = multiple Claude Code sessions sharing the same project directory. This mirrors how developers naturally work — multiple terminal sessions in the same repo, each on a different task.

**User-driven creation**: Admin creates a new topic in Zulip UI and sends a message → adapter sees new stream+topic → lazy creates instance → zero config needed.

**Routing is native**: Every Zulip message event carries both `display_recipient` (stream name) and `subject` (topic name). The adapter routes with zero parsing — just directory lookup.

## Two Roles, Two Output Models

The default configuration for the two instance types:

| | WhatsApp (Personal Assistant) | Zulip (Project Developer) |
|---|---|---|
| Claude's role | Assistant — active communicator | Developer — just works on code |
| Knows about channel? | Yes (has WhatsApp MCP tools) | No (doesn't know Zulip exists) |
| Output method | Claude calls `send_message` MCP | Stop hook captures output → sends to Zulip |
| CLAUDE.md | Contains WhatsApp rules, contacts, butler protocol | Clean — only project-specific rules |
| WhatsApp MCP | Enabled (default) | Disabled (default) — configurable per stream |
| Why? | Assistant needs to decide what/when/how to communicate | Developer's terminal output should transparently reach admin |

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│ ADMIN                                                         │
│                                                               │
│  WhatsApp ── wa_notifier ──── main ccmux (assistant)          │
│  (personal life,              manages everything,             │
│   task orchestration)         creates streams                 │
│                                                               │
│  Zulip ───── zulip_adapter ─┬─ ccmux-dev/fix-auth (topic)   │
│  (dev work,                 ├─ ccmux-dev/add-tests (topic)   │
│   per-topic sessions)       ├─ ipo-analysis/weekly  (topic)  │
│                             └─ ... (lazy created)            │
└──────────────────────────────────────────────────────────────┘
```

## Configuration Structure

### Directory Layout

```
~/.ccmux/streams/                         ← persistent config (survives reboot)
  system3/                                ← personal assistant (future unification)
    stream.toml                           ← channel=whatsapp, project_path=...
    main/                                 NOTE: v1 does not change the assistant's
      instance.toml                       existing infrastructure (ccmux daemon).
                                          This entry documents the target unified
                                          architecture where all instances — including
                                          the personal assistant — share the same model.
  ccmux-dev/                              ← Zulip stream
    stream.toml                           ← channel=zulip, project_path=~/claude-code-hub
    fix-auth-bug/
      instance.toml                       ← auto-created by adapter on first message
    add-tests/
      instance.toml                       ← auto-created
  ipo-analysis/                           ← another Zulip stream
    stream.toml                           ← channel=zulip, project_path=~/ipo_analysis
    weekly-report/
      instance.toml                       ← auto-created
```

### stream.toml (shared config, one per stream, manually created = register stream)

```toml
# ~/.ccmux/streams/ccmux-dev/stream.toml
project_path = "<project-root>"
channel = "zulip"

[capabilities]
# whatsapp_mcp = true  # uncomment to enable WhatsApp MCP for this stream's instances
```

```toml
# ~/.ccmux/streams/system3/stream.toml
project_path = "<project-root>"
channel = "whatsapp"
```

### instance.toml (unified template, auto-created by adapter)

```toml
# ~/.ccmux/streams/ccmux-dev/fix-auth-bug/instance.toml
created_at = "2026-03-02T01:00:00+08:00"
```

All instances are equal at the config level. Instance identity = directory path. Shared config inherited from parent `stream.toml`. Topic-specific values (ZULIP_TOPIC, FIFO path, tmux session name) derived from directory names.

### Runtime Layout (ephemeral, /tmp/)

```
/tmp/ccmux/                              ← runtime state (lost on reboot, recreated lazily)
  system3/main/
    in.whatsapp                           ← FIFO
    control.sock
  ccmux-dev/fix-auth-bug/
    in.zulip                              ← FIFO
    pid                                   ← injector PID
  ccmux-dev/add-tests/
    in.zulip
    pid
  ipo-analysis/weekly-report/
    in.zulip
    pid
```

**tmux session names**: `system3--main`, `ccmux-dev--fix-auth-bug`, `ipo-analysis--weekly-report` (double-dash separator for uniqueness since stream/topic names may contain single dashes).

Note: Project topic instances have NO control.sock (no output broadcast needed — Stop hook posts directly to Zulip). Simpler than the personal assistant.

### PID and Liveness Check

**PID file** stores the PID of the shell process inside the tmux pane (obtained via `tmux display-message -t {session} -p '#{pane_pid}'` after session creation).

**Liveness check** uses two methods (both must pass):
1. `tmux has-session -t {session}` — confirms tmux session exists
2. `kill -0 {pid}` — confirms the pane process is alive

If either fails, the instance is considered dead → lazy create on next message.

**Lazy create guard**: Before `tmux new-session`, always check `tmux has-session` first. If a session with that name already exists (e.g., PID file was cleaned but tmux survived), kill the old session before creating a new one.

**Claude exit detection**: The injection gate checks the tmux pane content. If it detects a shell prompt (e.g., `$`, `#`) instead of a Claude Code prompt, Claude has exited. The injector marks the instance as dead, and the next message triggers lazy recreation.

## Instance Environment Template

**File**: `~/.ccmux/instances/env_template.sh`

All project instances MUST inherit these environment variables. Loaded by process_mgr.py on every instance creation.

```bash
# Network proxy (required — machine routes through surfshark-gluetun VPN)
export HTTP_PROXY=http://127.0.0.1:8118
export HTTPS_PROXY=http://127.0.0.1:8118
export NO_PROXY=localhost,127.0.0.1

# Zulip output routing (set per-instance by process_mgr.py)
# ZULIP_STREAM and ZULIP_TOPIC are derived from directory names
export ZULIP_STREAM=${STREAM_NAME}
export ZULIP_TOPIC=${TOPIC_NAME}
export ZULIP_SITE=https://zulip.example.org
export ZULIP_BOT_EMAIL=ccmux-bot-bot@zulip.example.org
export ZULIP_BOT_API_KEY_FILE=~/.ccmux/secrets/zulip_bot.env
```

**Why a template file (not hardcoded)**:
- Single source of truth for instance startup requirements
- New requirements (e.g., new env var) added in one place
- process_mgr.py reads and applies, code guarantees completeness
- Prevents the class of bugs where "it works for the main session but not project instances"

## Stream Creation Flow

Admin initiates via WhatsApp (natural language):

```
Admin: "Create a dev project for ccmux"
  │
  ▼
AI Assistant (main ccmux):
  1. Asks: "Please provide one of:"
     a) Git URL → clone + suggest local path
     b) Existing local path
     c) Empty project → suggest path
  │
  ▼
Admin: "Use existing path ~/Desktop/claude-code-hub"
  │
  ▼
AI Assistant:
  1. Creates Zulip stream via helper script:
     .venv/bin/python3 scripts/zulip_helpers.py create-stream ccmux-dev
  2. Creates stream config directory + stream.toml:
     mkdir -p ~/.ccmux/streams/ccmux-dev/
     write stream.toml (project_path, channel=zulip)
  3. Replies: "✅ Stream ccmux-dev created. Open Zulip #ccmux-dev, create a topic, and start chatting."
  4. (Does NOT start any instance — lazy creation on first topic message)
```

## Complete Data Flow

### Inbound: Admin types in Zulip → Claude Code processes

```
Step 1  Admin types "fix the auth bug" in Zulip #ccmux-dev, topic "fix-auth"
            │
Step 2  Zulip adapter receives event via bot event queue (long-polling)
            │  event.display_recipient = "ccmux-dev"
            │  event.subject = "fix-auth"
            │
Step 3  Adapter checks: is instance alive?
            │
            ├── ~/.ccmux/streams/ccmux-dev/stream.toml exists? → stream registered
            │
            ├── /tmp/ccmux/ccmux-dev/fix-auth/pid exists AND process alive?
            │   └── YES → Go to Step 5
            │
            └── NO → Step 4 (lazy create)
            │
Step 4  LAZY CREATE:
            ├── Read stream.toml → get project_path, capabilities
            ├── Create instance.toml in ~/.ccmux/streams/ccmux-dev/fix-auth/
            ├── Run ccmux-init on project directory (idempotent):
            │     ccmux-init <project_path> [--capabilities from stream.toml]
            │     → ensures .claude/settings.json has Stop hook
            │     → ensures git pre-commit hook (privacy gate)
            │     → ensures .gitignore covers .claude/
            │     → writes minimal CLAUDE.md if absent
            │     → applies capabilities (e.g., WhatsApp MCP if configured)
            ├── mkdir -p /tmp/ccmux/ccmux-dev/fix-auth/
            ├── mkfifo /tmp/ccmux/ccmux-dev/fix-auth/in.zulip
            ├── Load env_template.sh
            ├── tmux new-session -d -s ccmux-dev--fix-auth \
            │     -e ZULIP_STREAM=ccmux-dev \
            │     -e ZULIP_TOPIC=fix-auth \
            │     -e ZULIP_SITE=https://zulip.example.org \
            │     -e ZULIP_BOT_EMAIL=ccmux-bot-bot@zulip.example.org \
            │     -e ZULIP_BOT_API_KEY_FILE=~/.ccmux/secrets/zulip_bot.env \
            │     -e HTTP_PROXY=http://127.0.0.1:8118 \
            │     -e HTTPS_PROXY=http://127.0.0.1:8118 \
            │     -e NO_PROXY=localhost,127.0.0.1 \
            │     -c <project_path> \
            │     "claude --dangerously-skip-permissions"
            ├── Start FIFO injector for this instance
            ├── Get pane PID → write to /tmp/ccmux/ccmux-dev/fix-auth/pid
            └── Post to #ccmux-dev topic "fix-auth": "🤖 Session started."
            │
Step 5  Adapter writes message to FIFO
            │   Writes "[19:05 zulip] fix the auth bug" → in.zulip
            │
Step 6  FIFO injector reads FIFO
            ├── Injection gate: Claude ready? (prompt visible in tmux pane)
            └── tmux send-keys -t ccmux-dev--fix-auth "fix the auth bug" Enter
            │
Step 7  Claude Code processes (reads code, edits, runs tests)
```

### Outbound: Claude Code response → Zulip topic

```
Step 8   Claude Code completes a turn (produces output in terminal)
            │
Step 9   Stop hook fires automatically (Claude Code native mechanism)
            ├── Reads `last_assistant_message` from stdin JSON
            ├── Reads $ZULIP_STREAM from env → "ccmux-dev"
            ├── Reads $ZULIP_TOPIC from env → "fix-auth"
            ├── Reads $ZULIP_SITE, bot credentials from env
            └── Calls Zulip API: POST /api/v1/messages
                {
                  "type": "stream",
                  "to": "ccmux-dev",
                  "topic": "fix-auth",
                  "content": "<Claude's response>"
                }
            │
Step 10  Admin sees response in Zulip #ccmux-dev, topic "fix-auth"
```

**Key simplification from v7**: Topic routing is now trivial. Each instance has ZULIP_STREAM and ZULIP_TOPIC as fixed env vars set at tmux creation time. No shared `current_topic` file, no race conditions, no synchronization needed. The hook just reads its own env vars.

### Full Round-Trip Diagram

```
  ZULIP (browser/app)              ZULIP ADAPTER              TOPIC INSTANCE
  ═══════════════════              ═════════════              ══════════════
                                                              /tmp/ccmux/ccmux-dev/fix-auth/
  #ccmux-dev
  topic: fix-auth
  ┌──────────────┐     event      ┌──────────┐   in.zulip   ┌──────────────┐
  │ Admin types   │────queue──────▶│ Route by │────FIFO─────▶│ Injector     │
  │ message       │               │ stream + │              │   │          │
  └──────────────┘               │ topic    │              │   ▼          │
                                  └──────────┘              │ tmux session │
                                                            │ (Claude Code)│
  ┌──────────────┐     Zulip API                            │   │          │
  │ Admin sees   │◀──────────────────────────────────────────│ Stop hook    │
  │ response in  │     (hook reads ZULIP_STREAM +           │ (env vars)   │
  │ same topic   │      ZULIP_TOPIC from env)               └──────────────┘
  └──────────────┘
```

Note: Outbound path does NOT go through the adapter. The hook posts directly to Zulip API.

## System Restart / Recovery

```
System reboots
    │
    ▼
ccmux.target starts
    ├── Main assistant (WhatsApp) → online
    ├── Zulip adapter starts
    │     └── Scans ~/.ccmux/streams/*/stream.toml
    │         ├── ccmux-dev: stream.toml ✓, /tmp/ gone (reboot)
    │         └── ipo-analysis: stream.toml ✓, /tmp/ gone
    │   (Does NOT start any instances — lazy)
    │
    ▼
Admin opens Zulip #ccmux-dev, topic "fix-auth", types a message
    │
    ▼
Adapter: /tmp/ccmux/ccmux-dev/fix-auth/pid not found → LAZY CREATE
    ├── Reads stream.toml (project_path, capabilities)
    ├── Runs ccmux-init (idempotent, ensures hooks)
    ├── Loads env_template.sh
    ├── Creates /tmp/ccmux/ccmux-dev/fix-auth/, FIFO, tmux, injector
    ├── Posts "🤖 Session started" to topic
    └── Processes the message

No data lost:
    ├── ~/.ccmux/streams/ (config survives reboot)
    ├── Project code on disk (survives reboot)
    ├── Zulip history in Zulip DB (survives reboot)
    └── Only /tmp/ gone → recreated lazily on next message
```

## Components

### 1. Zulip Adapter (`adapters/zulip_adapter/`)

Single process, **inbound only** (outbound handled by per-instance hooks):

```
adapters/zulip_adapter/
├── __init__.py
├── __main__.py       # Entry point (event loop)
├── adapter.py        # Inbound loop: Zulip event → route by stream+topic → FIFO
├── process_mgr.py    # Lazy create/check instances, load env template, run ccmux-init
├── injector.py       # Simple FIFO reader → injection gate → tmux send-keys
└── config.py         # Scan ~/.ccmux/streams/, read stream.toml files, ccmux.toml [zulip]
```

**Startup sequence** (`__main__.py`):
1. Load config (scan streams directory, read ccmux.toml)
2. Clean stale PID files under `/tmp/ccmux/` (v1 adapter restart mitigation)
3. Connect to Zulip event queue
4. Enter inbound message loop

**Routing logic** (`adapter.py`):
1. Event arrives: `display_recipient` = stream name, `subject` = topic name
2. Check `~/.ccmux/streams/{stream}/stream.toml` exists → stream registered
3. If not registered → ignore silently
4. Check instance alive: `/tmp/ccmux/{stream}/{topic}/pid` exists + process alive
5. If not alive → call process_mgr to lazy create
6. Write message to FIFO: `/tmp/ccmux/{stream}/{topic}/in.zulip`

**Hot-reload**: Re-scan `~/.ccmux/streams/` on each message (mtime-based cache). New streams become visible without adapter restart.

**injector.py** — lightweight FIFO-to-tmux injector for each instance. Reuses the injection gate logic (check Claude ready state via `tmux capture-pane`, check `#{client_activity}` for terminal idle) but does NOT include output broadcast or control.sock.

### 2. Stop Hook for Zulip Relay (`scripts/zulip_relay_hook.py`)

Installed as a Claude Code **Stop hook** in each project instance's `.claude/settings.json`. The Stop hook fires after every assistant turn and receives JSON on stdin containing `last_assistant_message`.

**Why Stop hook (not "PostResponse")**: Claude Code has no "PostResponse" event. The `Stop` event fires after each assistant turn completes and provides the response content via stdin JSON.

```python
#!/usr/bin/env python3
"""Stop hook for Zulip instances.

Reads Claude's latest output from stdin JSON, posts to Zulip stream+topic.
Stream and topic are env vars set at tmux creation time — no file-based routing.
Stdlib only — no venv dependencies.

Environment: ZULIP_STREAM, ZULIP_TOPIC, ZULIP_SITE, ZULIP_BOT_EMAIL, ZULIP_BOT_API_KEY_FILE
"""
import json, os, sys, urllib.request, urllib.parse, base64

def main():
    stream = os.environ.get("ZULIP_STREAM")
    topic = os.environ.get("ZULIP_TOPIC", "chat")
    if not stream:
        return  # Not a Zulip instance — skip

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    content = data.get("last_assistant_message", "")
    if not content:
        return

    key_file = os.path.expanduser(os.environ.get("ZULIP_BOT_API_KEY_FILE", ""))
    if not key_file or not os.path.exists(key_file):
        return
    api_key = ""
    with open(key_file) as f:
        for line in f:
            if line.startswith("ZULIP_BOT_API_KEY="):
                api_key = line.split("=", 1)[1].strip()
    if not api_key:
        return

    site = os.environ.get("ZULIP_SITE", "")
    email = os.environ.get("ZULIP_BOT_EMAIL", "")

    cred = base64.b64encode(f"{email}:{api_key}".encode()).decode()
    chunks = [content[i:i+9500] for i in range(0, len(content), 9500)]

    for chunk in chunks:
        post_data = urllib.parse.urlencode({
            "type": "stream", "to": stream,
            "topic": topic, "content": chunk
        }).encode()
        req = urllib.request.Request(
            f"{site}/api/v1/messages", data=post_data, method="POST"
        )
        req.add_header("Authorization", f"Basic {cred}")
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # Best-effort, never block Claude

if __name__ == "__main__":
    main()
```

**Hook installation** (in each project's `.claude/settings.json`):
```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "<project-root>/scripts/zulip_relay_hook.py"
          }
        ]
      }
    ]
  }
}
```

### 3. Project Init Tool (`scripts/ccmux_init.py`)

Bootstraps a project directory for ccmux integration. Called by `process_mgr.py` during lazy creation, or manually by admin. **Idempotent** — safe to run repeatedly.

```
ccmux-init <project_path> [--capabilities '{"whatsapp_mcp": true}']
```

**What it does:**

| Action | Target | Details |
|--------|--------|---------|
| Stop hook | `.claude/settings.json` | Register `zulip_relay_hook.py` as Stop hook. Merges with existing hooks. |
| Git privacy gate | `.git/hooks/pre-commit` | Symlink to `scripts/privacy_check.py`. If hook already exists, skip. |
| .gitignore | `.gitignore` | Ensure `.claude/` entry exists. |
| CLAUDE.md | `CLAUDE.md` | Write minimal template ONLY if absent. Never overwrites. |
| Capabilities | `.mcp.json` etc. | Apply per-stream capabilities (e.g., WhatsApp MCP if enabled). |

**What it does NOT do:**
- Does NOT install timers, services, or scheduled tasks
- Does NOT create data directories under `~/.ccmux/`
- Does NOT modify any existing project code or config

### 4. Zulip API Helper (`scripts/zulip_helpers.py`)

CLI tool for the main assistant to manage Zulip streams. Stdlib only (urllib).

```
scripts/zulip_helpers.py create-stream <stream_name>       # Create stream + subscribe bot
scripts/zulip_helpers.py delete-stream <stream_name>       # Archive/delete stream
scripts/zulip_helpers.py list-streams                      # List bot-subscribed streams
```

## Change Impact

| Component | Change | Details |
|-----------|--------|---------|
| Main ccmux assistant | **None** | WhatsApp as-is |
| wa_notifier | **None** | Unchanged |
| hook.py (main) | **None** | Main assistant hook unchanged |
| ccmux daemon | **None** | Untouched. Project instances use a simple injector. |
| ccmux.toml | **Add** | `[zulip]` section (additive) |
| systemd | **Add** | One unit: `ccmux-zulip-adapter.service` |

**Zero changes to existing running code. All new functionality is additive.**

## Non-Goals

- ~~out.zulip FIFO~~ → hook posts directly to Zulip API
- ~~Outbound loop in adapter~~ → hook handles output, adapter is inbound-only
- ~~Instance registry server~~ → directory structure
- ~~Per-instance systemd units~~ → adapter manages processes
- ~~Per-instance daemon~~ → simple injector (no output broadcast, no control.sock)
- ~~Zulip MCP server~~ → hook + env vars
- ~~Private data dirs per project~~ → project dir is everything
- ~~Per-project timers/services~~ → projects are pure dev sessions
- ~~Topic routing via shared file~~ → each instance has fixed ZULIP_TOPIC env var
- ~~projects.json~~ → directory-based config (`streams/{stream}/stream.toml`)

## Project Instance Permission Model

Project instances run with `claude --dangerously-skip-permissions` (non-interactive mode). Safety relies on:

1. **Capability-based scope** — by default, project instances have no messaging MCP tools and no personal data access. Capabilities explicitly opted in per stream via `stream.toml`
2. **Sandboxed CLAUDE.md** — template restricts to code-only work
3. **Admin supervision** — admin actively watches output in Zulip stream
4. **Privacy gate preserved** — git pre-commit hook still enforced

## Adapter Startup Recovery (deferred — post-v1)

When the adapter restarts, existing Claude sessions survive but their injectors die. Without re-attachment, FIFOs have no reader and writes block.

**v1 mitigation**: On adapter startup, delete all `pid` files under `/tmp/ccmux/`. Next message to any topic triggers lazy creation — session is recreated with some context loss. Acceptable for developer sessions (Zulip history preserved).

**Future improvement**: On startup, iterate streams, check PID liveness, and re-attach injectors to surviving sessions.

## Configuration

### ccmux.toml additions

```toml
[zulip]
site = "https://zulip.example.org"
bot_email = "ccmux-bot-bot@zulip.example.org"
bot_credentials = "~/.ccmux/secrets/zulip_bot.env"
streams_dir = "~/.ccmux/streams"
env_template = "~/.ccmux/instances/env_template.sh"
```

### Systemd (one unit only)

```ini
# ~/.config/systemd/user/ccmux-zulip-adapter.service
[Unit]
Description=ccmux Zulip adapter (inbound message routing)
PartOf=ccmux.target
After=network.target

[Service]
Type=simple
WorkingDirectory=<project-root>
ExecStart=<project-root>/.venv/bin/python3 -m adapters.zulip_adapter
Environment=HTTP_PROXY=http://127.0.0.1:8118
Environment=HTTPS_PROXY=http://127.0.0.1:8118
SyslogIdentifier=ccmux-zulip-adapter
Restart=on-failure
RestartSec=5

[Install]
WantedBy=ccmux.target
```

## Implementation Order

| Step | What | Effort |
|------|------|--------|
| 1 | `scripts/ccmux_init.py` — project init tool (hooks, gitignore, CLAUDE.md template) | Small |
| 2 | `scripts/zulip_relay_hook.py` — Stop hook (reads stream+topic from env vars) | Small |
| 3 | `scripts/zulip_helpers.py` — Zulip API helper (create/delete stream) | Small |
| 4 | `~/.ccmux/instances/env_template.sh` — env var template | Small |
| 5 | `adapters/zulip_adapter/config.py` — scan streams dir + read ccmux.toml | Small |
| 6 | `adapters/zulip_adapter/injector.py` — FIFO reader + injection gate + tmux send-keys | Medium |
| 7 | `adapters/zulip_adapter/process_mgr.py` — lazy create, env template, ccmux-init | Medium |
| 8 | `adapters/zulip_adapter/adapter.py` — inbound loop (Zulip event → route → FIFO) | Medium |
| 9 | End-to-end test: Zulip message → Claude response in correct topic | Small |
| 10 | First real stream: ccmux-dev | Small |

## Acceptance Criteria

### AC-1: ccmux-init (`scripts/ccmux_init.py`)

| # | Test | Verify |
|---|------|--------|
| 1.1 | Init on empty project dir | Creates .claude/settings.json with Stop hook, creates CLAUDE.md, appends .claude/ to .gitignore |
| 1.2 | Init on project with existing settings.json (has hook.py) | Merges zulip_relay_hook.py into Stop hooks; hook.py preserved |
| 1.3 | Init on project with existing CLAUDE.md | CLAUDE.md NOT overwritten |
| 1.4 | Init on project with existing pre-commit hook | Pre-commit hook NOT overwritten |
| 1.5 | Init on project with .gitignore containing .claude/ | .gitignore NOT modified |
| 1.6 | Idempotent: run twice | Second run identical to first |
| 1.7 | Non-git directory | Skips pre-commit hook; other actions still run |
| 1.8 | Init with `whatsapp_mcp` capability | Installs WhatsApp MCP in `.mcp.json` |
| 1.9 | Init with empty capabilities | No `.mcp.json` modifications |

### AC-2: zulip_relay_hook.py

| # | Test | Verify |
|---|------|--------|
| 2.1 | ZULIP_STREAM not set | Exits immediately, no API call, exit code 0 |
| 2.2 | Valid stdin with last_assistant_message | Posts to correct stream + topic |
| 2.3 | Empty last_assistant_message | Exits without API call |
| 2.4 | Malformed JSON on stdin | Exits gracefully, exit code 0 |
| 2.5 | Message > 9500 chars | Splits into multiple API calls |
| 2.6 | Zulip API returns error | Silently continues |
| 2.7 | Missing credentials file | Exits gracefully |
| 2.8 | Stdlib only | Runs with system Python |
| 2.9 | ZULIP_TOPIC set to "fix-auth" | Posts to topic "fix-auth" |
| 2.10 | ZULIP_TOPIC not set | Falls back to "chat" |

### AC-3: injector.py

| # | Test | Verify |
|---|------|--------|
| 3.1 | FIFO has message, Claude ready | Message injected via tmux send-keys |
| 3.2 | Claude generating (no prompt) | Message queued, injected when ready |
| 3.3 | Terminal active | Message queued, injected after idle |
| 3.4 | Multiple messages queued | All injected when Claude ready |
| 3.5 | FIFO uses O_RDWR \| O_NONBLOCK | No EOF when no writer |
| 3.6 | tmux session dies | Injector detects and exits |
| 3.7 | Claude exits, shell remains | Injector detects shell prompt (not Claude prompt), marks instance dead |

### AC-4: config.py

| # | Test | Verify |
|---|------|--------|
| 4.1 | Valid streams directory | All streams loaded with stream.toml fields |
| 4.2 | Empty streams directory | Returns empty dict |
| 4.3 | ccmux.toml [zulip] section | Reads site, bot_email, bot_credentials correctly |
| 4.4 | Missing [zulip] section | Clear error |
| 4.5 | New stream.toml added while running | Detected on next message (hot-reload) |

### AC-5: process_mgr.py

| # | Test | Verify |
|---|------|--------|
| 5.1 | First message to new topic | Lazy create: instance.toml, /tmp/ dir, FIFO, tmux, injector, PID |
| 5.2 | Message to running instance | Writes to existing FIFO |
| 5.3 | Stale PID | Detects dead process, triggers lazy create |
| 5.4 | env_template.sh loaded | All env vars passed to tmux |
| 5.5 | ZULIP_STREAM + ZULIP_TOPIC from directory names | Env vars match directory structure |
| 5.6 | stream.toml project_path used for tmux cwd | Claude starts in correct project directory |

### AC-6: adapter.py

| # | Test | Verify |
|---|------|--------|
| 6.1 | Message in registered stream | Routed to correct instance FIFO |
| 6.2 | Message in unregistered stream | Ignored silently |
| 6.3 | Bot's own message (echo) | Ignored |
| 6.4 | New topic in registered stream | Lazy creates new instance |
| 6.5 | Zulip connection lost | Reconnects with backoff |
| 6.6 | Multiple streams + topics active | Each routed independently |
| 6.7 | New stream.toml added | Detected and usable without restart |

### AC-6b: zulip_helpers.py

| # | Test | Verify |
|---|------|--------|
| 6b.1 | create-stream valid name | Stream created, bot subscribed |
| 6b.2 | create-stream existing | Idempotent |
| 6b.3 | delete-stream valid | Stream archived |
| 6b.4 | list-streams | Returns subscribed streams |
| 6b.5 | Invalid credentials | Clear error |
| 6b.6 | Stdlib only | Runs with system Python |

### AC-7: End-to-End

| # | Test | Verify |
|---|------|--------|
| 7.1 | Cold start round-trip | Zulip message → lazy create → Claude → response in same topic |
| 7.2 | Warm round-trip | Message → inject → respond → same topic. No recreation. |
| 7.3 | Restart recovery | Delete /tmp/ → message → lazy recreate → works |
| 7.4 | WhatsApp unaffected | Zulip operation doesn't impact WhatsApp assistant |
| 7.5 | Hook isolation | Assistant turn doesn't post to Zulip; project turn doesn't broadcast |
| 7.6 | Multiple topics same stream | Two topics in #ccmux-dev → two separate Claude instances, correct routing |
| 7.7 | Multiple streams | ccmux-dev + ipo-analysis active, no cross-talk |
| 7.8 | ccmux-init on existing ccmux project | Both hook.py and zulip_relay_hook.py coexist |

### AC-8: Non-Functional

| # | Requirement | Verify |
|---|-------------|--------|
| 8.1 | Existing code zero-change | `git diff ccmux/ adapters/wa_notifier/` shows no modifications |
| 8.2 | Existing tests pass | `pytest tests/ -q` — all pre-existing tests pass |
| 8.3 | zulip_relay_hook.py stdlib only | No imports outside Python stdlib |
| 8.4 | ccmux-init idempotent | Three consecutive runs produce identical state |
| 8.5 | Graceful degradation | Zulip down → adapter retries; instances work locally |

## Resolved Questions

1. **Output method**: Stop hook + urllib (stdlib, no MCP)
2. **Max concurrent sessions**: Not enforced for v1 (deferred)
3. **Proxy**: Mandatory in env_template.sh
4. **Role separation**: WhatsApp Claude = active communicator; Zulip Claude = passive developer
5. **Hook event type**: Claude Code `Stop` event. Fires after each assistant turn.
6. **Transcript access**: Stop hook receives JSON on stdin with `last_assistant_message`.
7. **Hook installation**: Per-project `.claude/settings.json`, installed by `ccmux-init`.
8. **Message length**: Split at 9500 chars (10000 char API limit).
9. **Daemon changes**: None. Project instances use simple FIFO injector.
10. **Project independence**: Each instance is standalone. `ccmux-init` bootstraps minimal integration.
11. **Hook isolation**: ZULIP_STREAM env var guard. Per-project settings.json provides natural isolation.
12. **Topic routing** (v8): Each instance has fixed ZULIP_STREAM + ZULIP_TOPIC env vars set at tmux creation. No shared file, no race condition. Topic = directory name.
13. **Permission mode**: `claude --dangerously-skip-permissions`. Safe via capability-based scope + sandboxed CLAUDE.md + admin supervision + git privacy gate.
14. **Adapter restart recovery** (deferred): Lazy creation handles restarts with acceptable context loss.
15. **Stream/topic model** (v8): Directory structure follows Zulip's own concepts. `~/.ccmux/streams/{stream}/stream.toml` = shared config, `{stream}/{topic}/instance.toml` = per-instance. No separate "project" abstraction.
16. **Instance config equality** (v8): instance.toml is a unified template — does not distinguish between assistant and project instances. All instances are equal at the config level.
17. **Config inheritance** (v8): Instance inherits from parent stream.toml (project_path, channel, capabilities). Topic-specific values (env vars, FIFO path, tmux name) derived from directory names.

## Future Direction

**Deep Zulip integration**: Consider integrating ccmux directly into Zulip as a native component (plugin, bot framework extension, or custom service) rather than running as an external adapter. This could eliminate the adapter layer entirely and provide tighter lifecycle management, native UI integration, and simplified deployment.
