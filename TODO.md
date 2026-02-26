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
| 6 | Gmail scanner HTML parsing | Medium | TODO | HTML-only emails (e.g. Tapestry school notifications) lose content after tag stripping. Improve `extract_text_body()` in `scripts/gmail_scanner.py` — use proper HTML-to-text conversion (e.g. html2text) instead of naive regex strip. |
| 7 | wa_notifier forward "From: Me" | Medium | Done | Forward admin's "From: Me" messages from all chats (not just self-chat). Anti-loop: skip messages starting with bot prefixes. Completed 2026-02-26. |
| 8 | Outlook Web email send tool | Medium | TODO | Consolidate Outlook Web email compose/reply/send into `libs/web_agent/email.py`. Continue using Playwright approach. Key safety: screenshot before send → AI analysis confirms TO/body/subject are correct → then send. Need: `reply_email(search_query, body)`, `compose_email(to, subject, body)`. Rule: reply from same inbox that received the email. |
| 9 | Persistent pending task tracker | Medium | TODO | Cross-session task tracking for tasks waiting on external confirmation. File: `~/.ccmux/data/pending_tasks.jsonl`. Lifecycle: received -> notified -> follow-up -> confirmed -> closed. Prevents tasks being dropped across sessions (see error_log.md 2026-02-26). |

## Completed

| # | Task | Date | Notes |
|---|------|------|-------|
| — | Gmail scanner | 2026-02-25 | IMAP scanner deployed, timer active every 2h 06:00-20:00. |
| — | Git history privacy rewrite | 2026-02-25 | filter-repo across 22 commits, 16 patterns. 5 audit rounds passed. |
| — | GitHub repo migration (ccmx→ccmux) | 2026-02-25 | Remote URL switched, all history pushed. |
