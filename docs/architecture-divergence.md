# Architecture Divergence: Current vs Admin's Vision (v4)

Captured 2026-03-03 from discussion between admin and assistant.
Reference: admin's v4 architecture diagram (WhatsApp image 2026-03-03 13:46).

## Admin's v4 Vision

Each project instance is **fully independent** — its own process, own injector, own FIFO, own tmux session. The adapter is a thin router, not a manager.

```
                    ┌──────────────┐
                    │  Zulip Server │
                    └──────┬───────┘
                           │ event queue
                    ┌──────▼───────┐
                    │ zulip_adapter │  (thin router only)
                    │  route by     │
                    │  stream+topic │
                    └──┬────┬────┬─┘
                       │    │    │   write to per-instance FIFO
              ┌────────▼┐ ┌▼────┐ ┌▼────────┐
              │Instance A│ │ B   │ │ C       │  (fully independent)
              │ injector │ │ ... │ │ ...     │
              │ tmux     │ │     │ │         │
              │ Claude   │ │     │ │         │
              └──────────┘ └─────┘ └─────────┘
```

Key characteristics:
- **projects.json** registry defines all instances
- **ccmux-init** bootstraps a new project instance
- Each instance is a standalone process (own PID, own crash recovery)
- Adapter only routes messages to FIFOs — no lifecycle management
- Instances can exist without the adapter (e.g. timer-triggered, terminal-only)

## Current Implementation

The adapter is a **manager** — it owns instance lifecycle via `ProcessManager`.

```
                    ┌──────────────┐
                    │  Zulip Server │
                    └──────┬───────┘
                           │ event queue
                    ┌──────▼───────────────────┐
                    │       ZulipAdapter         │
                    │  ┌─────────────────────┐  │
                    │  │   ProcessManager     │  │
                    │  │  _lazy_create()      │  │
                    │  │  ensure_instance()   │  │
                    │  │  injector tasks      │  │
                    │  └─────────┬───────────┘  │
                    └────────────┼──────────────┘
                       ┌────────▼────────┐
                       │  Per-topic state │
                       │  (asyncio tasks) │
                       │  tmux + FIFO +   │
                       │  injector        │
                       └─────────────────┘
```

Key characteristics:
- **ProcessManager** inside the adapter creates/manages all instances
- Injectors run as asyncio tasks within the adapter process
- Instance lifecycle (create, resume, fallback, crash detect) is adapter-managed
- Adapter restart = loss of all instance state (TODO #21)
- No external registry — stream.toml files define routing, not instances

## Key Divergences

| Aspect | v4 Vision | Current |
|--------|-----------|---------|
| Instance independence | Fully standalone process | Asyncio task inside adapter |
| Injector ownership | Per-instance (own process) | Adapter-managed (asyncio task) |
| Lifecycle management | Self-managed or external supervisor | ProcessManager in adapter |
| Registry | projects.json (explicit) | stream.toml (routing only) |
| Adapter role | Thin router (FIFO write only) | Manager (lifecycle + routing) |
| Crash isolation | Instance crash doesn't affect others | Adapter crash kills all |
| Bootstrap | ccmux-init tool | _lazy_create() on first message |

## What Can Be Reused

Despite the architectural difference, significant code is reusable:

1. **Adapter routing logic** (`_handle_message`, stream config, hot-reload) — same role in both
2. **FIFO protocol** (NUL-delimited, `_write_to_fifo`) — identical
3. **Message formatting** (`[yy/mm/dd HH:MM From zulip] content`) — identical
4. **File handler** (attachment download, sanitize, strip links) — identical
5. **Relay hook** (`zulip_relay_hook.py`) — identical, already per-instance
6. **Event queue + reconnect** (register, long-poll, BAD_EVENT_QUEUE_ID, staleness watchdog) — identical
7. **Config loading** (`scan_streams`, `ZulipAdapterConfig`) — reusable with extension
8. **Session resume logic** (instance.toml, session-id, JSONL detection) — reusable

## What Needs Refactoring

1. **Extract injector to standalone process** — currently an asyncio task, needs to be a separate script/process that reads FIFO and injects into tmux
2. **Extract lifecycle to external supervisor** — replace ProcessManager's _lazy_create with an init tool + systemd/supervisor
3. **Add projects.json registry** — explicit instance registry replacing implicit stream.toml
4. **Decouple adapter from ProcessManager** — adapter should only write to FIFO, not manage tmux sessions

## Agreed Next Steps

- Admin will use Zulip (ccmux-dev stream) for the refactoring work
- This document serves as the shared reference for the discussion
- Fix deployed: event queue auto-reconnect (staleness watchdog, 2026-03-03)
