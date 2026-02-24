# ccmux Deployment Architecture

> This document describes the deployment architecture. The source of truth for
> timer schedules is `ccmux.toml`. For operational commands, see CLAUDE.md.

## Architecture

```
ccmux.toml (SSOT for timers + service registry)
    │
    ├── [timers.*]  →  ccmux-reconcile  →  generates .timer + .service files
    │
    └── [services].managed  →  ccmux-reconcile  →  verifies .service files
```

```
systemctl --user start ccmux.target
  → ccmux-reconcile.service (oneshot, syncs toml → systemd)
    → whatsapp-bridge.service
      → ccmux.service (daemon, creates tmux + launches Claude)
        → ccmux-wa-notifier.service
      → all ccmux-*.timer (generated, Persistent=true)
```

## Components

| Unit | Type | PartOf target | Description |
|------|------|---------------|-------------|
| ccmux-reconcile | oneshot | WantedBy | Syncs toml → systemd before other services start |
| whatsapp-bridge | simple | yes | WhatsApp WebSocket connection |
| ccmux | simple | yes | Message multiplexer daemon (owns tmux session + Claude lifecycle) |
| ccmux-wa-notifier | simple | yes | Polls SQLite, writes to FIFO |
| ccmux-*.timer | timer | yes | Auto-generated from ccmux.toml |

> **Note**: tmux session and Claude process are managed exclusively by the ccmux daemon
> (`daemon._setup_tmux()` + `LifecycleManager`). There is no separate `claude-code.service` —
> having two components create the same tmux session caused a race condition where Claude
> launched without proxy env vars.

## Reconciliation

`ccmux-reconcile.service` runs automatically before `ccmux.target` on every boot:

1. Reads `ccmux.toml [timers]` — desired state
2. Scans existing `ccmux-*.timer` files — actual state
3. Creates missing timers, updates changed ones, removes orphans
4. Verifies `[services].managed` entries exist with `PartOf`
5. Runs `daemon-reload` if anything changed

Manual trigger: `ccmux-deploy` (same logic, immediate effect).

## Known Limitations

- Claude Code restarts as a new session (no context carryover — relies on CLAUDE.md)
- WhatsApp bridge auth may expire (requires manual QR re-scan)
- MCP connections are established by Claude Code config on startup
