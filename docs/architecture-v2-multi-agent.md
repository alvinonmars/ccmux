# ccmux v2 Architecture — Multi-Agent via Instance Multiplication

> Status: **Design memo** (2026-03-01). Updated after Zulip deployment decision.
> Origin: Admin + assistant discussion on project isolation and multi-session development.

## Decision Log

### Slack → Zulip pivot (2026-03-01)

Slack was the original plan for project chat. Rejected because:
- Free tier: 90-day message history limit, max 10 integrations
- Many useful features (workflow builder, advanced permissions) require paid plans
- Self-hosting not available

Alternatives evaluated (with machine context: 93GB RAM available):

| Option | RAM | Verdict |
|--------|-----|---------|
| Matrix + Conduit | 32MB | Lightweight but mobile clients immature |
| Mattermost v9 | 100-300MB | Good but heavier than needed |
| Rocket.Chat | 500MB-1.5GB | MongoDB dependency, complex |
| XMPP + Prosody | 25-50MB | No good unified client experience |
| **Zulip** | **2.7-3GB** | **Selected** — topic-based threading, excellent API, self-hosted, mobile app on App Store |
| Custom Web UI | 10-50MB | Too much work, reinventing the wheel |

**Zulip selected** because:
- Topic-based threading (stream > topic) maps naturally to project > task
- Full REST API + bot system for programmatic access
- Self-hosted with Docker (deployed to `zulip.alvindesign.org` via cloudflared)
- Mobile app available on iOS/Android App Store (no push notification registration needed — WhatsApp handles alerts)
- Invite-only registration, stream-level permissions
- 3GB RAM is trivial on a 93GB machine

### Architecture direction (2026-03-01)

**Current state**: Zulip deployed as development workbench alongside WhatsApp personal assistant.

**Future vision** (not yet implemented): Zulip becomes the root control plane.
- Today: WhatsApp = primary, Zulip = new channel for dev
- Future: Zulip = primary control plane, WhatsApp = one of many channel adapters
- This refactoring is deferred. Current priority is multi-session via Zulip.

## Problem

The current single ccmux session handles everything: family WhatsApp, contacts, email, scheduling, AND development work. When deep development is needed, personal messages interrupt context and dilute focus. The assistant cannot do heavy engineering while simultaneously being a responsive personal butler.

## Core Insight

**Don't modify ccmux. Multiply it.**

Each role/project gets its own ccmux instance — same codebase, different config, isolated context. ccmux remains a single-session system. Multi-agent is achieved by running multiple instances, not by making one instance multi-session.

## Architecture

```
Admin (human)
│
├── Personal Assistant ccmux (WhatsApp + email + scheduling)
│   ├── in.whatsapp (wa_notifier adapter)
│   ├── in.project-report    ← receives status from all projects
│   ├── tmux: ccmux-main
│   ├── data: ~/.ccmux/data/
│   └── CLAUDE.md: personal assistant behavior
│
├── Project A ccmux (Zulip #project-a stream)
│   ├── in.zulip (zulip_notifier adapter)
│   ├── in.boss              ← receives directives from assistant
│   ├── tmux: ccmux-project-a
│   ├── data: ~/.ccmux-project-a/data/
│   └── CLAUDE.md: project-a specific context
│
├── Project B ccmux (Zulip #project-b stream)
│   ├── in.zulip
│   ├── in.boss
│   ├── tmux: ccmux-project-b
│   ├── data: ~/.ccmux-project-b/data/
│   └── CLAUDE.md: project-b specific context
│
└── ... (up to ~10 projects)
```

### Zulip Mapping

| Zulip Concept | ccmux Mapping |
|---------------|---------------|
| Stream (e.g. #ccmux-dev) | One project's chat window → one ccmux instance's FIFO |
| Topic (within stream) | Conversation thread (optional organization) |
| Bot (ccmux-bot) | Message I/O interface — single bot routes to all instances by stream |
| DM to bot | Possible future: quick commands without stream context |

One bot serves all project instances. The Zulip adapter routes messages by stream name to the correct FIFO.

## Key Design Decisions

### 1. Stream = Project

Each Zulip stream corresponds to one project. The project has a persistent Claude Code session (not an ephemeral agent). When the project ends, the stream is archived.

Rejected alternatives:
- Channel = Employee (capability-based): cross-project context bleed, lifecycle mismatch
- Hybrid (dispatchable workers): over-engineered for <10 projects

### 2. ccmux Stays Single-Session

No multi-session refactoring. Each ccmux instance is identical in architecture:
- One tmux session
- One daemon
- One set of FIFOs (in/out)
- One CLAUDE.md
- One private data directory
- One systemd service unit
- Human can attach to any instance via tmux

### 3. Zulip = Development Workbench, WhatsApp = Personal Life

| Channel | Purpose | ccmux Instance |
|---------|---------|----------------|
| WhatsApp | Family, contacts, email, scheduling | Personal assistant (main) |
| Zulip #project-x | Development, engineering, analysis | Project-x instance |

These are complementary, not competing. Admin interacts with project sessions directly via Zulip. Personal assistant does not participate in development details.

### 4. Project Creation (Three Modes)

Admin creates a new project via command or Zulip:
1. **Empty project**: Name only → creates empty directory + session
2. **GitHub repo**: URL → clone + session
3. **Existing path**: Local directory → session pointing to that path

Each creation triggers: Zulip stream + tmux session + FIFO + systemd unit + CLAUDE.md.

### 5. Context Isolation (like real employees)

- Project sessions do NOT see personal/family data
- Project sessions do NOT see other projects' data
- Personal assistant sees project meta-info (status, time spent, costs) but NOT technical details
- This mirrors real-world org structure: CEO's PA knows the boss's schedule; engineers know their codebase; neither sees the other's domain

## Inter-Instance Communication Protocol

### Transport: FIFO (reuse existing mechanism)

Each ccmux already supports multiple FIFOs. Cross-instance communication is just FIFO wiring:

```
Project-A out.report  →  symlink/pipe  →  Assistant in.project-report
Assistant out.project-a  →  symlink/pipe  →  Project-A in.boss
```

### Project → Assistant (upward reporting)

JSON messages on `out.report` FIFO:

```json
{
  "project": "ipo-analysis",
  "ts": "2026-03-01T14:30:00+08:00",
  "type": "status_update",
  "status": "active",
  "current_task": "Analyzing March IPO pipeline",
  "progress": "3/7 companies analyzed",
  "session_tokens_today": 12500000,
  "session_cost_today": 8.50,
  "active_minutes_today": 45,
  "last_interaction": "2026-03-01T14:25:00+08:00",
  "alerts": [],
  "deliverables": ["output/march_ipo_report.md"]
}
```

Trigger conditions:
- Scheduled heartbeat (hourly or daily)
- Task completion
- Blocker requiring admin decision
- Token consumption threshold
- Session start/stop

### Assistant → Project (downward directives)

Plain text or JSON on `out.project-{name}` FIFO:

```json
{
  "type": "task",
  "from": "personal-assistant",
  "content": "Admin wants a summary of this week's IPO activity",
  "priority": "normal",
  "deadline": null
}
```

### Assistant-Side Aggregation

Personal assistant maintains:
- `~/.ccmux/data/projects/dashboard.jsonl` — latest status of all projects
- Included in morning/evening briefings
- Responds to admin queries ("how are my projects doing?")

## Relationship to Existing Patterns

| Current | v2 Equivalent |
|---------|---------------|
| Cross-Project Delegation (background agent) | Replaced by persistent project session |
| Single CLAUDE.md with all rules | Split: each instance has role-specific CLAUDE.md |
| One systemd service | Multiple: ccmux-main, ccmux-project-a, ccmux-project-b... |
| All under ccmux.target | Still under ccmux.target (PartOf cascading works) |

## What This Is (and Isn't)

**This IS:**
- Multi-agent system built on Unix primitives (processes, FIFOs, tmux)
- Each agent is a real independent process with full Claude Code capability
- Human can attach to any agent anytime (tmux)
- No AI agent framework needed (no AutoGen, no CrewAI)

**This is NOT:**
- A rewrite of ccmux (zero changes to core)
- Session-internal sub-agents (Claude Code Team/Task)
- Simulated agents sharing one context window

## Deployment Status (Zulip)

Deployed 2026-03-01:
- **Server**: Docker Compose (see private config for path)
- **URL**: `https://zulip.alvindesign.org` (via cloudflared tunnel)
- **Organization**: System3
- **Admin**: (see `.claude/CLAUDE.md`)
- **Bot**: ccmux-bot (API key in `~/.ccmux/secrets/zulip_bot.env`)
- **Streams**: #ccmux-dev, #alerts, #general
- **Security**: Invite-only registration, localhost binding, HTTPS via cloudflared
- **Port**: 127.0.0.1:9900 → container port 80

## Implementation Order

1. **Zulip adapter** (`adapters/zulip_notifier/`) — polls Zulip for new messages, routes by stream to correct FIFO, sends Claude output back via bot API. Same pattern as wa_notifier.
2. **Instance provisioning script** — create new ccmux instance (tmux + FIFO + CLAUDE.md + systemd + Zulip stream). Supports three modes: empty project, GitHub clone, existing path.
3. **Inter-instance FIFO wiring** (report + directive channels)
4. **Personal assistant CLAUDE.md update** (project dashboard rules)
5. **Project CLAUDE.md template** (reporting behavior, Zulip-only communication)
6. **First project instance**: ccmux itself as the first Zulip-managed project

## Constraints & Assumptions

- Max subscription supports multiple concurrent Claude Code sessions (confirmed by admin)
- Fewer than 10 concurrent projects (no need for session pooling)
- Each project session runs 24/7 (persistent, not on-demand)
- Admin interacts with project sessions directly via Zulip (not through personal assistant)
- Personal assistant only receives meta-info, does not relay development conversations
- Single bot (ccmux-bot) serves all instances, routing by stream name
- WhatsApp personal assistant remains unchanged during Phase 1
