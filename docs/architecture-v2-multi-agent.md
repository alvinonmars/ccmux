# ccmux v2 Architecture — Multi-Agent via Instance Multiplication

> Status: **Design memo** (2026-03-01). Not yet implemented.
> Origin: Admin + assistant discussion on Slack integration and project isolation.

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
│   ├── in.whatsapp
│   ├── in.project-report    ← receives status from all projects
│   ├── out.project-{name}   → sends directives to projects
│   ├── tmux: ccmux-main
│   ├── data: ~/.ccmux/data/
│   └── CLAUDE.md: personal assistant behavior
│
├── Project A ccmux (Slack #project-a)
│   ├── in.slack
│   ├── in.boss              ← receives directives from assistant
│   ├── out.report            → reports to assistant
│   ├── tmux: ccmux-project-a
│   ├── data: ~/.ccmux-project-a/data/
│   └── CLAUDE.md: project-a specific context
│
├── Project B ccmux (Slack #project-b)
│   ├── in.slack
│   ├── in.boss
│   ├── out.report
│   ├── tmux: ccmux-project-b
│   ├── data: ~/.ccmux-project-b/data/
│   └── CLAUDE.md: project-b specific context
│
└── ... (up to ~10 projects)
```

## Key Design Decisions

### 1. Channel = Project (not Employee)

Each Slack channel corresponds to one project. The project has a persistent Claude Code session (not an ephemeral agent). When the project ends, the channel and session are archived.

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

### 3. Slack = Development Workbench, WhatsApp = Personal Life

| Channel | Purpose | ccmux Instance |
|---------|---------|----------------|
| WhatsApp | Family, contacts, email, scheduling | Personal assistant (main) |
| Slack #project-x | Development, engineering, analysis | Project-x instance |

These are complementary, not competing. Admin interacts with project sessions directly via Slack. Personal assistant does not participate in development details.

### 4. Context Isolation (like real employees)

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

## Implementation Order (when ready)

1. Slack adapter for ccmux (adapter only, reusable across instances)
2. Instance provisioning script (create new ccmux instance with its own config/data/systemd)
3. Inter-instance FIFO wiring (report + directive channels)
4. Personal assistant CLAUDE.md update (project dashboard rules)
5. Project CLAUDE.md template (reporting behavior, Slack-only communication)
6. First project instance (e.g., ipo-analysis as standalone ccmux)

## Constraints & Assumptions

- Max subscription supports multiple concurrent Claude Code sessions (confirmed by admin)
- Fewer than 10 concurrent projects (no need for session pooling)
- Each project session runs 24/7 (persistent, not on-demand)
- Admin interacts with project sessions directly via Slack (not through personal assistant)
- Personal assistant only receives meta-info, does not relay development conversations
