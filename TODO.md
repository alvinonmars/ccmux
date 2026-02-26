# ccmux — Development TODO

Single source of truth for all engineering tasks. Referenced by CLAUDE.md.

## Active

| # | Task | Priority | Status | Notes |
|---|------|----------|--------|-------|
| 1 | Project restructuring | High | Pending | Separate ccmux core from extended capabilities, address privacy data in repo, clean module boundaries. No git until done. |
| 2 | Build `libs/web_agent/` | High | Pending | Screenshot-driven web automation framework. Depends on #1. |
| 3 | PowerSchool sign-up flow | Medium | Blocked | Event sign-up. Blocked on #2 + admin approval. |
| 4 | Admin JID file (`admin_jid.txt`) | Medium | TODO | wa_notifier writes auto-detected admin JID to read-only `~/.ccmux/data/admin_jid.txt`. First-write/change triggers WhatsApp verification. Part of deployment manual config. Main session reads file instead of hardcode. |
| 5 | Google Calendar replacement | Low | TODO | Removed Google Calendar MCP (auth issues). Find alternative approach for calendar management. |

## Completed

| # | Task | Date | Notes |
|---|------|------|-------|
| — | Gmail scanner | 2026-02-25 | IMAP scanner deployed, timer active every 2h 06:00-20:00. |
| — | Git history privacy rewrite | 2026-02-25 | filter-repo across 22 commits, 16 patterns. 5 audit rounds passed. |
| — | GitHub repo migration (ccmx→ccmux) | 2026-02-25 | Remote URL switched, all history pushed. |
