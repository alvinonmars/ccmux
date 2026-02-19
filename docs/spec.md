# ccmux — Requirements and Design

## Purpose

Wrap the Claude Code CLI with standard Unix I/O interfaces so it can accept input from multiple asynchronous sources while keeping the native terminal experience fully intact.

**This project is infrastructure, not an end-user application. Design principle: more AI decisions, fewer hardcoded rules.**

---

## Background

### Problem

Claude Code CLI is an interactive TTY program that only accepts input from a terminal. It cannot run 24/7 and respond to multi-source asynchronous events.

### Constraints

- Uses Claude Max subscription — no API key
- Must run the real `claude` CLI; cannot bypass it

### Prior Art

Community projects (e.g. [hanxiao/claudecode-telegram](https://github.com/hanxiao/claudecode-telegram)) have validated: running Claude Code persistently in tmux + injecting input via `tmux send-keys` + reading transcripts via stop hook is a viable approach. ccmux generalizes this pattern.

---

## Goals and Non-Goals

### Goals

- Claude Code runs 24/7, auto-recovers from crashes
- Accept input from any source via named FIFOs and inject into Claude Code
- Broadcast Claude's replies to all subscribers via a Unix socket
- Enable precise output routing via the `send_to_channel` MCP tool
- Filesystem as channel registry — adapters self-register, ccmux discovers dynamically
- Native terminal experience (direct tmux attach) is completely unaffected
- Structured logging for debugging

### Non-Goals (current version)

- Per-channel reliable delivery (adapter's responsibility)
- Injection rate limiting (adapter's responsibility)
- Concrete adapter implementations (Telegram bot, push notifications, etc.)
- Session content persistence (depends on prompt design; future iteration)

---

## Concepts

### Transcript

The conversation history JSONL file maintained automatically by Claude Code:

```
~/.claude/projects/<project-hash>/<session-id>.jsonl
```

(Note: no `conversations/` subdirectory — verified by SP-01)

Each line is one turn, with `role` being `user` or `assistant`, and `content` being a block array:

```jsonc
{"role": "user",      "content": [{"type": "text", "text": "Check my homework"}]}
{"role": "assistant", "content": [
  {"type": "thinking",  "thinking": "Let me think..."},
  {"type": "text",      "text": "Sure, I'll help you check it."},
  {"type": "tool_use",  "name": "send_to_channel", "input": {"channel": "telegram", "message": "Homework: ..."}}
]}
```

**SP-01/SP-05 verified**: The Stop hook fires once after each assistant turn (fires on every reply in interactive mode). The transcript path is obtained from `stdin["transcript_path"]` — no environment variable needed.

**Broadcast design confirmed**: stop hook → read last transcript line → control.sock → output.sock broadcast. The original design works as-is.

### Pub/Sub

The output socket distribution model:

- Subscribers connect to the output socket, maintain a long connection, and wait passively
- When Claude completes a turn, the daemon pushes the **complete turn content** to all connected subscribers
- A subscriber disconnecting does not affect other subscribers
- Routing logic is Claude's responsibility via tool calls; the output socket does full broadcast only (for monitoring, logging, debugging)

CLI test: `nc -U /tmp/ccmux/output.sock`

---

## Architecture Overview

```
Adapter layer (self-registering, start/stop on demand)
  Telegram adapter ──creates──▶ /tmp/ccmux/in.telegram  (FIFO)
  Phone adapter    ──creates──▶ /tmp/ccmux/in.phone     (FIFO)
  Timer adapter    ──creates──▶ /tmp/ccmux/in.agent     (FIFO)

  Telegram adapter ──creates──▶ /tmp/ccmux/out.telegram (FIFO, waits for Claude to write)
  Phone adapter    ──creates──▶ /tmp/ccmux/out.phone    (FIFO)

                   inotify detects new/removed FIFOs dynamically
                         │
              ┌───────────▼──────────────────────┐
              │          ccmux daemon             │
              │                                   │
              │  ┌─ message collector ───────────┐ │
              │  │ reads all in.* FIFOs          │ │
              │  │ on stop hook + terminal idle  │ │
              │  │ injects into Claude at once   │ │
              │  └───────────────────────────────┘ │
              │                                   │
              │  ┌─ MCP server ──────────────────┐ │
              │  │ send_to_channel(channel, msg) │ │
              │  │ → writes /tmp/ccmux/out.<ch> │ │
              │  └───────────────────────────────┘ │
              │                                   │
              │  ┌─ lifecycle manager ───────────┐ │
              │  │ monitors Claude Code process  │ │
              │  │ exponential backoff restart   │ │
              │  └───────────────────────────────┘ │
              │                                   │
              └──────────────┬────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  tmux session   │
                    │  Claude Code    │
                    │  (MCP client)   │
                    └────────┬────────┘
                             │
              ┌──────────────▼────────────────────┐
              │  stop hook script                 │
              │  reads transcript → control.sock  │
              └──────────────┬────────────────────┘
                             │
              ┌──────────────▼────────────────────┐
              │  output.sock (Unix socket server) │
              │  broadcasts complete turn to all  │
              │  subscribers                      │
              └───────────────────────────────────┘

  Terminal ──── tmux attach ────▶ Claude Code  (native, no intermediate layer)
```

---

## Filesystem as Channel Registry

The `/tmp/ccmux/` directory (configurable) uses this naming convention:

| Path pattern | Type | Created by | Purpose |
|-------------|------|------------|---------|
| `in` | FIFO | ccmux | Default input channel |
| `in.<name>` | FIFO | adapter | Named input channel |
| `out.<name>` | FIFO | adapter | Named output channel (Claude writes to it) |
| `output.sock` | Unix socket | ccmux | Full broadcast |
| `control.sock` | Unix socket | ccmux | stop hook → daemon internal communication |

Adapters create their own FIFOs on startup; the daemon auto-discovers them via inotify. When an adapter exits, its FIFO disappears and the daemon automatically detaches. **No config files, no registration handshakes.**

---

## Core Layer Design

### Components

| Component | Responsibility |
|-----------|---------------|
| Directory watcher (inotify) | Watches `/tmp/ccmux/`, detects FIFO additions/removals |
| Input FIFO reader | `O_NONBLOCK` + `select` + `os.read()` on all `in.*` FIFOs (SP-03: avoids readline deadlock) |
| Message collector | Gathers queued messages on stop hook signal + terminal idle check, injects all at once |
| tmux injector | `tmux send-keys -l 'content'` + `tmux send-keys Enter` to inject text into the Claude pane (two steps, SP-04 verified) |
| Ready detector | Determines whether Claude Code is waiting for input |
| MCP server | Provides the `send_to_channel` tool to Claude Code |
| Transcript watcher | inotify on transcript JSONL tail; reads and broadcasts new lines (**fallback**: used when Stop hook fails) |
| Output pub/sub | Maintains subscriber list, broadcasts complete turns |
| Lifecycle manager | Monitors Claude Code process, restarts with exponential backoff on crash |
| Terminal monitor (pipe-pane) | Side-channel observation of pane stdin, updates `last_terminal_input_time` |
| Logger | Structured JSON logs |

### Message Collection and Injection Flow

```
Stop hook fires (Claude completes a turn)
    → control.sock receives complete turn content
    → broadcast to output.sock
    → check: has terminal received input in the last N seconds? (pipe-pane -I)
        yes → skip, wait for next stop hook
        no  → drain all in.* FIFOs, format and inject all queued messages into Claude
```

No priority queue, no Human/Agent classification, no batch size limit. Claude receives all queued messages and decides how to respond.

### Startup Sequence

Full flow when ccmux starts (or restarts):

```
1. Environment check: verify HTTP_PROXY/HTTPS_PROXY point to local proxy (default http://127.0.0.1:8118)
2. Hook installation: write/update the hooks field in ~/.claude/settings.json (idempotent, preserves other fields)
3. MCP server start: start on a fixed socket path (no PID or random suffix)
4. MCP config write: write MCP server address into Claude Code config
5. tmux session handling:
   a. Session does not exist → create new, run `claude --dangerously-skip-permissions` in pane
   b. Session exists → attach, use capture-pane to detect current state (ready / generating / permission prompt)
6. pipe-pane mount: `tmux pipe-pane -O` begins monitoring stdout
7. Directory watcher start: ensure /tmp/ccmux/ exists, inotify begins
```

### Hook Management

ccmux installs the following hook events (written to the `hooks` field in `~/.claude/settings.json`):

| Event | ccmux usage |
|-------|------------|
| `SessionStart` | Record session_id, discover transcript path |
| `Stop` | **Core**: read last transcript line → control.sock → broadcast |
| `SubagentStart` | Log, detect agent team activity |
| `SubagentStop` | Log |
| `SessionEnd` | Trigger lifecycle manager restart logic |
| `PermissionRequest` | Detect permission prompt, route to human channel, suppress auto-injection |

**Not installed**: `PreToolUse` / `PostToolUse` — these fire for **all running claude instances** on the machine (including when ccmux itself calls Bash), causing infinite loops or false triggers (verified by SP-05).

Hook format (correct nested format, verified by SP-05):

```json
{
  "hooks": {
    "Stop":         [{"hooks": [{"type": "command", "command": "/path/to/ccmux-hook.py"}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "/path/to/ccmux-hook.py"}]}]
  }
}
```

Hook script path: `~/.local/share/ccmux/hook.py` (fixed path, no PID or random suffix).

### Claude Ready Detection

Three states to distinguish:

| Claude Code state | stdout characteristic | Injectable? |
|------------------|-----------------------|-------------|
| Waiting for input | Input prompt present | ✅ |
| Generating reply | Continuous output | ❌ |
| Permission prompt | Confirmation prompt, different format | ❌ |

Detection mechanism (verified by SP-02):

1. **Primary**: `pipe-pane -O` monitors stdout — **3 consecutive seconds with no new bytes** = ready (during generation, spinner sequences `\x1b[?2026l/h` appear multiple times per second)
2. **Auxiliary confirmation**: `tmux capture-pane -p` snapshot — check if the last line contains `❯`
3. **Permission prompt detection**: also silent (no spinner); use `capture-pane` text search for `Yes`/`No`/`allow`/`y/n` keywords; when detected, skip injection and trigger PermissionRequest routing (capture-pane check is the fallback if the hook doesn't fire)

### Terminal Activity Detection

`tmux pipe-pane -I` side-channel monitors pane stdin, updating `last_terminal_input_time`. Before injecting, check: if < N seconds since last terminal input (default 30s), skip this injection window. This is the minimal rule that protects active human conversations from being interrupted.

### MCP Server

The ccmux daemon runs as an MCP server, ready before Claude Code starts. See the Startup Sequence section for ordering.

**Transport**: SSE over HTTP (not stdio). The MCP server runs independently at a fixed address so it survives Claude Code restarts. Claude Code reconnects by re-reading its config on each restart.

**Address**: `http://127.0.0.1:<CCMUX_MCP_PORT>` (default port: `9876`)

**Claude Code config**: written to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "ccmux": {
      "type": "sse",
      "url": "http://127.0.0.1:9876/sse"
    }
  }
}
```

Provided tool:

```
send_to_channel(channel: str, message: str) -> void
  → writes to /tmp/ccmux/out.<channel>
  → if that FIFO does not exist, returns an error to Claude (Claude decides how to handle it)
```

### Session Recovery

- Lifecycle manager monitors the claude process in the tmux session
- On crash: exponential backoff restart (1s → 2s → 4s → 8s, cap 60s)
- Restart command includes `--continue`
- pipe-pane is re-mounted after restart

**ccmux daemon restart strategy: continuity-first.** When the ccmux daemon itself restarts, it attaches to the existing tmux session and uses `capture-pane` to detect Claude's current state (ready / generating / permission prompt) before deciding whether to inject. Claude Code's conversation history is preserved.

### Logging

- structlog, JSON format, output to file + stderr
- Key events: FIFO register/deregister, message injection, ready detection method, broadcast, crash/restart, tool calls

---

## Protocol Specification

### Input FIFO Format

Two formats are supported, auto-detected:

```bash
# Plain text (starts with any character other than {)
echo "Check my homework" > /tmp/ccmux/in.telegram

# JSON Lines (with metadata)
echo '{"channel":"telegram","content":"Check my homework","ts":1740000000,"meta":{}}' \
  > /tmp/ccmux/in.telegram
```

| Case | channel | ts | content |
|------|---------|-----|---------|
| Plain text | Inferred from FIFO filename (`in.telegram` → `telegram`) | Current time | Entire line |
| JSON | `channel` field in JSON | `ts` field in JSON | `content` field in JSON |

### Injection Format (what Claude sees)

Multiple queued messages merged into one injection:

```
[14:30 telegram] Did you check your homework?
[15:02 phone] You are near the school
[15:45 timer] Daily homework check reminder
```

Format assembled by the daemon; Claude decides how to handle each message.

### Output Socket Broadcast Format

JSON Lines — one broadcast per turn (complete turn, all blocks):

```json
{
  "ts": 1740000000,
  "session": "abc123",
  "turn": [
    {"type": "thinking", "thinking": "..."},
    {"type": "text", "text": "OK, I'll handle this."},
    {"type": "tool_use", "name": "send_to_channel", "input": {"channel": "telegram", "message": "Homework: Math ch.3"}},
    {"type": "tool_result", "content": "ok"}
  ]
}
```

### Control Socket (internal)

For use by the stop hook script only. Format:

```json
{"type": "broadcast", "session": "abc123", "turn": [...]}
```

---

## Configuration Parameters

All parameters have defaults; none are required. Override via environment variable or config file.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CCMUX_RUNTIME_DIR` | `/tmp/ccmux` | Directory for FIFOs and sockets |
| `CCMUX_TMUX_SESSION` | `ccmux` | tmux session name managed by ccmux |
| `CCMUX_IDLE_THRESHOLD` | `30` | Seconds of terminal inactivity before injection is allowed |
| `CCMUX_SILENCE_TIMEOUT` | `3` | Seconds of stdout silence before Claude is considered ready |
| `CCMUX_MCP_PORT` | `9876` | HTTP port for the MCP SSE server |
| `CCMUX_BACKOFF_INITIAL` | `1` | Initial backoff seconds on crash |
| `CCMUX_BACKOFF_CAP` | `60` | Maximum backoff seconds |
| `CCMUX_HOOK_SCRIPT` | `~/.local/share/ccmux/hook.py` | Path to the installed hook script |
| `CCMUX_LOG_FILE` | `~/.local/share/ccmux/ccmux.log` | Structured log output path |

---

## Technology Choices

| Decision | Choice | Reason |
|----------|--------|--------|
| Language | Python | Rich ecosystem, natural tmux control |
| Concurrency | asyncio | Multiplexed I/O without threads |
| tmux control | libtmux | Stable Python wrapper |
| Logging | structlog | Structured JSON |
| SQLite | sqlite3 (stdlib) | Optional queue persistence |
| Directory watching | watchdog or asyncinotify | FIFO add/remove detection |
| MCP server | mcp (Anthropic official Python SDK) | Standard protocol |

---

## Spikes (one-time pre-implementation validation — see acceptance-criteria.md)

| ID | Goal | Status | Impact |
|----|------|--------|--------|
| SP-01 | Stop hook data format | ✅ Done | See below |
| SP-02 | Claude ready prompt + permission prompt detection | ✅ Done | See below |
| SP-03 | FIFO concurrent write behavior | ✅ Done | See below |
| SP-04 | tmux send-keys special characters | ✅ Done | See below |
| SP-05 | Complete hook event types + agent team behavior | ✅ Done | See below |

### SP-01 Findings (stop hook data format)

- **Trigger timing**: fires once after each assistant turn (✅ SP-05 interactive mode verified: fires on every reply, not just on session exit)
- **stdin format**: valid JSON with top-level fields:
  ```json
  {
    "session_id": "...",
    "transcript_path": "/home/user/.claude/projects/<hash>/<session_id>.jsonl",
    "cwd": "...",
    "permission_mode": "default",
    "hook_event_name": "Stop",
    "stop_hook_active": false,
    "last_assistant_message": "plain text of the last reply"
  }
  ```
- **Transcript path**: obtained from `stdin["transcript_path"]`, **no `conversations/` subdirectory**, no environment variable needed
- **Transcript JSONL format**: each line is a JSON object; assistant turn structure:
  ```json
  {"message": {"role": "assistant", "content": [{"type": "text", "text": "..."}, ...]}, ...}
  ```
  Filter by `message.role == "assistant"`, take the last line, read `message.content` array.
- **Thinking block**: did not appear in `-p` mode (no extended thinking); field name inferred from API docs as `"thinking"` — treat as optional in implementation (no error if absent)

> ✅ Design confirmed (SP-05 interactive test): Stop hook fires **per turn**. Original plan (stop hook → broadcast) works as-is. inotify transcript watcher is the fallback.

### SP-02 Findings (Claude ready detection)

- **During generation**: stdout emits `\x1b[?2026l\x1b[?2026h` + spinner chars (`✻ ✶ ✽ ✢ * · ●`) + status text (`Baking…`, `Philosophising…`, etc.) multiple times per second, continuously
- **Ready state**: stdout is **completely silent**, no output at all. Most reliable detection: `pipe-pane -O` monitoring stdout — 3 consecutive seconds with no new bytes = ready
- **`❯` prompt**: present in raw captured bytes but surrounded by dense ANSI escape sequences (synchronized update `\x1b[?2026h/l`); not suitable as a direct match target. **Silence timeout is primary**; `❯` is auxiliary confirmation
- **`tmux capture-pane`**: returns current visible content; last line is `❯` when ready, contains spinner char when generating. Useful for **fast polling** (alternative or supplement to pipe-pane)
- **Permission prompt**: was not triggered (`ls /tmp` auto-approved in default mode); format pending verification, but `capture-pane` text search for keywords (`Yes/No`, `allow`, `y/n`) is the identification strategy
- **`pipe-pane -O` stability**: ✅ capture successful, byte count normal, production-ready

### SP-03 Findings (FIFO concurrent behavior)

- **O_NONBLOCK open (no writer)**: `open()` returns immediately (no ENXIO), `read()` returns `b''` (no data), no blocking
- **EOF detection**: when all writers close, `read()` returns `b''`; reader correctly detects EOF
- **Short messages (< PIPE_BUF = 4096B)**: concurrent writes are **atomic**, zero data interleaving
- **Long messages (> PIPE_BUF)**: writes are not atomic; `readline()` can mutually block with writers causing deadlock. Must use `select` + `os.read()` non-blocking reads
- **Conclusion**: daemon must read FIFOs with `O_NONBLOCK` + `select` + `os.read()`; `readline()` is forbidden. Message length should be kept < 4096B (naturally satisfied for single messages)

### SP-04 Findings (tmux send-keys injection)

- **`-l` flag**: when using `-l`, `Enter` must be sent as a separate command (`tmux send-keys -l 'content'` + `tmux send-keys Enter`); combining into one command sends `Enter` as literal text
- **Character fidelity**: 15 categories of special characters (Chinese, `$`, backtick, `!`, `"`, `'`, `\`, `[]`, `{}`, `|`, `;`, `*`) are injected **losslessly** into Claude Code regardless of whether `-l` is used
- **Conclusion**: use `-l` flag (explicit semantics), send Enter separately. No additional escaping needed for special characters

### SP-05 Findings (complete hook event types + agent team behavior)

**Complete hook event list** (extracted from settings schema, verified by SP-05):

| Event | Trigger timing | ccmux usage |
|-------|---------------|------------|
| `SessionStart` | claude process starts | Record session_id, discover transcript path |
| `Stop` | After each assistant turn ✅ | **Core**: read last transcript line, broadcast |
| `SubagentStart` | Task tool spawns a sub-agent | Detect team activity |
| `SubagentStop` | Sub-agent completes | Log |
| `SessionEnd` | claude process exits | Trigger restart logic |
| `PreToolUse` | Before a tool call | ⚠️ Fires for ALL claude instances on the machine — do not install |
| `PostToolUse` | After a tool call | Same as above |
| `PermissionRequest` | Permission confirmation prompt | Detect permission prompt, route to human channel |
| `UserPromptSubmit` | User submits input | Detect input timing |
| `Notification` | System notification | Logging only |

**Agent team key findings** (SP-05 interactive test):

- `SubagentStart` fires when Task tool spawns a sub-agent; the `session_id` field is the **leader's** session_id (sub-agents share the leader's session_id)
- `SubagentStop` fires when the sub-agent completes
- After all sub-agent work completes, the leader's `Stop` fires normally; transcript contains sub-agent results
- **Conclusion**: ccmux only needs to monitor the leader session's Stop hook to get the complete team output — no need to monitor sub-agents separately

**Permission prompt handling**:

- User's settings.json pre-authorizes all common tools (`Read/Write/Edit/Glob/Grep/Bash(*)`)
- ccmux starts claude with `--dangerously-skip-permissions` or `--permission-mode acceptEdits`
- Remaining cases: use `PermissionRequest` hook to detect; fall back to `capture-pane` keyword search

---

## Future Iterations

1. Adapter SDK: standardized adapter development interface, built-in examples (cron, HTTP webhook)
2. System prompt (CLAUDE.md): define Claude's behavior conventions in the ccmux environment (channel awareness, tool usage timing)
3. Reliable delivery: application-layer implementation using SQLite to track delivery status
4. Session continuity: improve context continuation mechanism in conjunction with CLAUDE.md
