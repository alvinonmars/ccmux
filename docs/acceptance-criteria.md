# ccmux — Acceptance Criteria and Test Cases

## Test Strategy

| Layer | Scope | Dependencies | How to run |
|-------|-------|-------------|------------|
| **Unit** | Individual functions/classes, protocol parsing, state machines | Pure mocks | `pytest tests/unit/` |
| **Integration** | Full core-layer flows | tmux + Mock Claude | `pytest tests/integration/` |
| **Spike** | One-time validation of real Claude Code behavior | Real Claude Code | Run manually; findings written into spec.md |

The automated iteration loop depends on Unit + Integration tests. Spike findings are frozen as config parameters and never re-run.

### Mock Claude Process Specification

Located at `tests/helpers/mock_claude.py`. Replaces real Claude Code in integration tests.

**Environment variables**:

| Variable | Default | Description |
|----------|---------|-------------|
| `MOCK_PROMPT` | `❯ ` | Prompt string output when waiting for input |
| `MOCK_REPLY` | `mock reply` | Reply content for each turn (supports `\n` escapes) |
| `MOCK_DELAY` | `0.1` | Seconds between receiving input and outputting reply |
| `MOCK_TRANSCRIPT` | `""` | Transcript file path — if set, appends a valid JSONL line after each reply |
| `MOCK_HOOK_SCRIPT` | `""` | Hook script path — if set, calls it after each reply with Stop hook JSON on stdin |
| `MOCK_SPINNER` | `0` | If > 0, emits N spinner sequences (`\x1b[?2026l\x1b[?2026h✻`, 0.1s apart) before replying |
| `MOCK_CONTINUOUS_SPINNER` | `0` | If > 0, emits spinner sequences indefinitely (one per 0.1s) until stdin receives input; overrides `MOCK_SPINNER` |
| `MOCK_PERMISSION_INTERVAL` | `0` | If > 0, every N turns: output permission prompt text (`Allow this action? Yes/No`), call hook with PermissionRequest JSON, then pause waiting for stdin |

**Behavior loop**:
1. On start: output `$MOCK_PROMPT`
2. On stdin input: wait `$MOCK_DELAY` seconds
3. If `MOCK_CONTINUOUS_SPINNER > 0`: emit spinner sequences every 0.1s until next stdin arrives (loops back to step 2)
4. Else if `MOCK_SPINNER > 0`: emit N spinner sequences (0.1s apart)
5. If this is turn N and `MOCK_PERMISSION_INTERVAL > 0` and `N % MOCK_PERMISSION_INTERVAL == 0`:
   - Output `Allow this action? Yes/No`
   - If `MOCK_HOOK_SCRIPT` is set: call hook script with PermissionRequest JSON on stdin
   - Wait for next stdin (the "resolution"); then continue
6. Output `$MOCK_REPLY`
7. If `MOCK_TRANSCRIPT` is set: append one JSONL line to the file
8. If `MOCK_HOOK_SCRIPT` is set: call the hook script with Stop hook JSON on stdin (including `transcript_path`, `session_id`)
9. Output `$MOCK_PROMPT` again; go to step 2

**Transcript JSONL line format**:
```json
{"message": {"role": "assistant", "content": [{"type": "text", "text": "$MOCK_REPLY"}]}, "ts": 1740000000}
```

---

## Spikes (one-time pre-implementation validation)

Located in `tests/spikes/`. Each spike is an independently runnable program. All spikes are complete; findings are frozen in spec.md.

### SP-01: Claude Code stop hook data format ✅

**Validation goals**:
- JSON field structure of the hook script's stdin
- How to obtain the transcript file path
- Hook trigger timing (per-turn vs per-session)

**Findings (frozen)**:
- [x] Transcript path: `stdin["transcript_path"]`, format `~/.claude/projects/<hash>/<session_id>.jsonl` (no `conversations/` subdirectory)
- [x] Trigger timing: once after each assistant turn (confirmed by SP-05 interactive mode test)
- [x] Thinking block field name: did not appear in `-p` mode; inferred as `"thinking"` from API docs; treated as optional in implementation

---

### SP-02: Claude Code stdout prompt and permission prompt detection ✅

**Validation goals**:
- stdout byte sequence when Claude Code is waiting for input
- stdout byte sequence during generation
- Reliable way to distinguish the two states

**Findings (frozen)**:
- [x] During generation: stdout emits `\x1b[?2026l\x1b[?2026h` + spinner chars multiple times per second
- [x] Ready state: stdout completely silent; 3 consecutive seconds with no new bytes = ready
- [x] Auxiliary confirmation: last line of `tmux capture-pane -p` contains `❯` when ready
- [x] Permission prompt detection: `capture-pane` text search for `Yes`/`No`/`allow`/`y/n` keywords
- [x] `pipe-pane -O` reliably captures stdout; production-ready

---

### SP-03: FIFO concurrent write and non-blocking read ✅

**Validation goals**:
- Whether lines interleave when multiple processes write to the same FIFO concurrently
- Behavior of `open`/`read` in O_NONBLOCK mode when no writer exists
- Whether the reader correctly detects EOF when all writers close

**Findings (frozen)**:
- [x] Short messages (< 4096B): writes are atomic, no data interleaving
- [x] O_NONBLOCK open with no writer: `open()` returns immediately; `read()` returns `b''`; no ENXIO
- [x] Correct pattern for daemon FIFO reads: `O_NONBLOCK` + `select` + `os.read()`; `readline()` is forbidden

---

### SP-04: tmux send-keys injection behavior ✅

**Validation goals**:
- Whether Unicode/Chinese characters inject correctly
- Whether shell special characters are interpreted
- `-l` flag behavior

**Findings (frozen)**:
- [x] Use `-l` flag: `tmux send-keys -l 'content'` + `tmux send-keys Enter` (two commands; Enter sent separately)
- [x] Special characters (Chinese, `$`, backtick, `!`, `"`, `'`, `\`, `[]`, `{}`, `|`, `;`, `*`) require no extra escaping; injection is lossless

---

### SP-05: Complete hook event types + agent team behavior ✅

**Validation goals**:
- All valid hook event types
- Which session_id sub-agent hooks report
- Side effects of PreToolUse/PostToolUse

**Findings (frozen)**:
- [x] Stop hook fires per turn in interactive mode (corrects SP-01's -p mode misreading)
- [x] SubagentStart/Stop report the leader's session_id; leader's Stop hook includes complete team output
- [x] PreToolUse/PostToolUse fire for all claude instances on the machine — **ccmux does not install these**
- [x] Correct hook format: `[{"hooks": [{"type": "command", "command": "..."}]}]` (nested, not flat)
- [x] SessionStart carries session_id; usable for transcript path discovery

---

## Acceptance Criteria and Test Cases

### AC-00 Startup Sequence

**Criterion**: After `ccmux start`, hooks are installed, the MCP server is up, claude is running in tmux, and pipe-pane is mounted.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-00-1 | Fresh start | Run `ccmux start` (no existing tmux session) | hooks written to settings.json; MCP server socket exists; claude process running in tmux pane; `/tmp/ccmux/` directory exists |
| T-00-2 | Hook installation is idempotent | Run `ccmux start` twice | Other fields in settings.json are preserved; hook entry appears exactly once |
| T-00-3 | Restart with existing session | tmux session and claude both running; restart daemon | Daemon attaches to existing session; does not kill claude; detects state via capture-pane and resumes |
| T-00-4 | Proxy not configured | Start without HTTP_PROXY set | Daemon logs a warning but does not abort startup |

---

### AC-01 Input FIFO Message Acceptance

**Criterion**: While the daemon is running, writing to any `in.*` FIFO delivers the content; daemon does not crash.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-01-1 | Plain text | `echo "hello" > /tmp/ccmux/in` | Daemon receives it; log entry recorded |
| T-01-2 | JSON | `echo '{"channel":"tg","content":"hi","ts":1000,"meta":{}}' > /tmp/ccmux/in` | Daemon correctly parses channel and content |
| T-01-3 | Invalid JSON | `echo '{bad' > /tmp/ccmux/in` | Treated as plain text; daemon does not crash |
| T-01-4 | Concurrent writers | 5 processes write to the same FIFO simultaneously | All messages received; no content interleaving |

---

### AC-02 Filesystem Dynamic Registration (inotify)

**Criterion**: When an adapter creates a FIFO, the daemon auto-discovers and starts reading it. When the FIFO disappears, the daemon stops reading it.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-02-1 | Dynamic input FIFO registration | Daemon running; `mkfifo /tmp/ccmux/in.test`; write a message | Daemon detects new FIFO; message received |
| T-02-2 | Input FIFO removal | Delete `in.test`; attempt to write | Daemon does not crash; logs FIFO deregistration |
| T-02-3 | Dynamic output FIFO registration | Create `out.test`; Claude calls `send_to_channel("test", "msg")` | Message written to `out.test`; adapter can read it |
| T-02-4 | Output FIFO does not exist | Call `send_to_channel("nonexist", "msg")` | Tool returns error to Claude; daemon does not crash; logs the event |

---

### AC-03 Message Injection into Claude Code

**Criterion**: After receiving a message, the daemon injects it via `tmux send-keys`; Mock Claude receives the input.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-03-1 | Single message injection | Send `{"content":"hello world"}` | `tmux capture-pane` shows `hello world` |
| T-03-2 | Chinese content | Send `{"content":"你好"}` | Pane shows `你好`, no corruption |
| T-03-3 | Multiple queued messages merged | Write 3 messages before stop hook fires | All 3 messages merged into one injection, format includes source and timestamp |
| T-03-4 | Injection format | Inject multi-source messages; inspect capture-pane | Format is `[HH:MM channel] content` one line per message |

---

### AC-04 Terminal Activity Detection (pipe-pane)

**Criterion**: After `pipe-pane -I` detects keyboard input, the daemon updates `last_terminal_input_time` and suppresses injection.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-04-1 | Suppress injection when terminal active | Simulate keyboard input via send-keys; immediately trigger stop hook | Queued FIFO messages are not injected |
| T-04-2 | Resume injection after terminal idle | Wait longer than N seconds; trigger stop hook | Queued messages are injected normally |
| T-04-3 | Idle threshold configurable | Set threshold to 5s | Injection resumes after 5s; timing error ≤ 1s |

---

### AC-05 Claude Ready Detection

**Criterion**: The detector correctly distinguishes "waiting for input", "generating", and "permission prompt" states.

| ID | Scenario | Mock Claude behavior | Expected result |
|----|----------|---------------------|----------------|
| T-05-1 | Normal prompt | `MOCK_PROMPT=❯ `, no spinner | Ready event fires within 3.2s of silence; capture-pane last line contains `❯` |
| T-05-2 | Silence timeout fallback | Reply without outputting prompt; remain silent | Daemon fires ready event after 3s silence (±200ms) |
| T-05-3 | Permission prompt | `MOCK_PERMISSION_INTERVAL=2`; outputs `Allow this action? Yes/No` | Ready event does not fire; daemon logs `permission_prompt` state; no injection |
| T-05-4 | Generating | `MOCK_CONTINUOUS_SPINNER=1`; emits spinner sequences indefinitely until input received | Ready event does not fire while spinner is active; ready fires only after spinner stops and 3s silence elapses |
| T-05-5 | Silence timeout configurable | Set timeout to 1s | Daemon fires ready event after 1s (±200ms) |

---

### AC-06 Output Socket Full Broadcast

**Criterion**: After the stop hook fires, all subscribers receive the complete turn content within ≤ 1s.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-06-1 | Single subscriber | 1 client connected to output.sock | Receives broadcast |
| T-06-2 | Multiple subscribers | 3 clients connected; stop hook fires | All 3 receive identical broadcast; time difference ≤ 100ms |
| T-06-3 | No subscribers | Stop hook fires with 0 clients | Daemon does not crash; logs the event |
| T-06-4 | Subscriber disconnect | 1 client disconnects; broadcast fires | Remaining subscribers receive broadcast normally |

---

### AC-07 Output Socket Broadcast Format

**Criterion**: Broadcast payload is valid JSON containing the complete turn (not just text).

| ID | Scenario | Verification |
|----|----------|-------------|
| T-07-1 | Plain text reply | Contains `ts` (int), `session` (str), `turn` (array) |
| T-07-2 | Reply with thinking | `turn` contains `{"type":"thinking","thinking":"..."}` block if present |
| T-07-3 | Reply with tool call | `turn` contains `tool_use` and `tool_result` blocks |
| T-07-4 | `ts` accuracy | Differs from actual trigger time by ≤ 2s |

---

### AC-08 MCP Tool: send_to_channel

**Criterion**: After Claude calls `send_to_channel`, the message is written to the output FIFO and the adapter can read it.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-08-1 | Normal call | `out.telegram` exists; Claude calls the tool | Message written to FIFO; adapter reads it |
| T-08-2 | Target does not exist | `out.nonexist` absent; Claude calls the tool | Tool returns error; daemon logs it; does not crash |
| T-08-3 | Concurrent calls | Two `send_to_channel` calls in the same turn | Both messages written to their respective FIFOs |

---

### AC-09 Claude Code Crash Recovery

**Criterion**: After Mock Claude terminates, the daemon detects it and restarts with exponential backoff.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-09-1 | Single crash | `kill -9 <mock claude pid>` | Daemon detects crash within ≤ 3s and restarts; log recorded |
| T-09-2 | Normal operation after restart | Send a new message after restart | Message injected normally; pipe-pane re-mounted |
| T-09-3 | Exponential backoff | Kill 4 times consecutively | Restart intervals approximately 1s, 2s, 4s, 8s (error ≤ 50%, accounting for process poll interval) |
| T-09-4 | Backoff cap | Kill 10 times consecutively | Interval stabilizes at 60s (±10s) |

---

### AC-10 Structured Logging

**Criterion**: Key events produce structured JSON log entries with required fields.

| Event | Required fields |
|-------|----------------|
| FIFO register/deregister | `event`, `ts`, `path` |
| Message received | `event`, `ts`, `channel`, `content_len` |
| Message injected | `event`, `ts`, `message_count` |
| Ready detected | `event`, `ts`, `method` (`prompt`/`timeout`/`skipped`) |
| Broadcast sent | `event`, `ts`, `subscriber_count` |
| Tool call | `event`, `ts`, `channel`, `message_len` |
| Process crash | `event`, `ts`, `pid` |
| Process restart | `event`, `ts`, `restart_count`, `backoff_seconds` |

| ID | Scenario | Verification |
|----|----------|-------------|
| T-10-1 | Full injection flow | Log contains: receive → inject → ready → broadcast, in correct order |
| T-10-2 | Crash recovery | Log contains crash + restart + backoff_seconds event sequence |
| T-10-3 | Invalid input | Log contains an `error`-level entry |

---

### AC-11 Graceful Shutdown

**Criterion**: After SIGTERM, all sockets are closed, no orphan processes remain, exit code is 0.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-11-1 | Normal shutdown | Send SIGTERM | Exits within 5s; exit code 0; socket files cleaned up |
| T-11-2 | Active subscriber during shutdown | Subscriber connected; send SIGTERM | Subscriber connection closed; daemon exits normally |
| T-11-3 | Injection in progress during shutdown | Send SIGTERM during injection | Current injection completes or times out before exit; no hung processes |

---

### AC-12 ccmux Daemon Restart and Reconnect

**Criterion**: After the ccmux daemon restarts, it attaches to the existing tmux session and resumes normal operation without interrupting the Claude session.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-12-1 | Daemon restarts while claude is running | Kill daemon; let claude complete a turn; start daemon again | Daemon attaches to existing session; does not restart claude; re-mounts pipe-pane |
| T-12-2 | Injection resumes after restart | Write to FIFO after daemon restarts | Message received and injected; Mock Claude receives it |
| T-12-3 | Broadcast resumes after restart | Stop hook fires after daemon restarts | Subscribers receive broadcast |
| T-12-4 | Claude in permission prompt during restart | Daemon restarts; claude is at permission prompt | Daemon detects permission prompt state; does not inject; logs the state |

---

### AC-13 PermissionRequest Routing

**Criterion**: When a permission prompt is detected, automatic injection is suppressed and an alert is routed to the human channel.

| ID | Scenario | Action | Expected result |
|----|----------|--------|----------------|
| T-13-1 | PermissionRequest hook fires | Mock Claude emits permission prompt (`MOCK_PERMISSION_INTERVAL=1`) | Daemon receives PermissionRequest hook; stops automatic injection |
| T-13-2 | capture-pane fallback detection | Hook does not fire but capture-pane finds permission keywords | Daemon still stops injection; logs detection source as `capture-pane` |
| T-13-3 | Injection resumes after permission resolved | Human resolves the permission prompt; claude returns to ready | Daemon detects ready state; resumes automatic injection |

---

## Pass Criteria

- Unit tests: 100% pass
- Integration tests: 100% pass (AC-00 through AC-13 fully covered)
- All 5 spikes (SP-01 through SP-05) have written findings, updated in spec.md
- No skipped test items

## Test Commands

```bash
pytest tests/unit/        -v                    # no external dependencies
pytest tests/integration/ -v                    # requires tmux
pytest tests/             --cov=ccmux --cov-report=term-missing
```
