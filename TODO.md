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
| 9 | Persistent pending task tracker | Medium | Done | Implemented ccmux/pending_tasks.py. JSONL-backed tracker with add/update/close/overdue. 5 unit tests. File: ~/.ccmux/data/pending_tasks.jsonl. |
| 10 | Remove out.* FIFO mechanism | Medium | Done | Dead code removed. Commit c04af83. Deleted mcp_server.py, removed mcp_port/mcp_url from config, removed output FIFO watcher callbacks, simplified daemon entrypoint. 247 tests pass. |
| 11 | Multi-agent architecture (v2) | High | In Progress | Multiple ccmux instances (one per project). Zulip stream = project chat window → FIFO → Claude Code session. See docs/architecture-v2-multi-agent.md. |
| 12 | Consolidate assistant identity under ~/.ccmux/ | High | TODO | All non-git config (private CLAUDE.md, ccmux.toml, .mcp.json) should live under ~/.ccmux/config/ with symlinks from project dir. Goal: `rsync ~/.ccmux/` = full assistant migration. Also: hook.py context compaction detection + auto state re-injection. |
| 13 | Hook-based context compaction recovery | High | Done | PreCompact hook implemented. hook.py triggers startup_selfcheck.py on context compaction. Committed 028056e. |
| 14 | Zulip adapter + multi-session MVP | High | In Progress | Zulip deployed (zulip.alvindesign.org, org: System3, bot: ccmux-bot). Next: Zulip adapter (adapters/zulip_notifier/), instance provisioning script, first project instance (ccmux itself). Pivoted from Slack due to paid limitations. See docs/architecture-v2-multi-agent.md. |
| 15 | Procurement closed-loop for S3 | Medium | TODO | End-to-end household procurement workflow: helper reports needs → S3 records list → forward to admin/wife for approval → order (online/offline) → delivery confirmation → expense tracking. Bilingual (EN/ZH) support. |
| 16 | Gmail scanner: FIFO delivery resilience | High | TODO | When FIFO write fails (e.g. after restart), scanner silently drops notification. Fix: (a) retry with backoff, (b) fallback to writing a flag file that ccmux checks on next cycle, (c) preserve scan_results per-run (timestamped files or append mode) instead of overwriting single file. Incident: 2026-03-02 18:00 scan found 2 emails but FIFO write failed, results lost. |
| 17 | Gmail scanner: scan_results history | Medium | TODO | Change scan_results.json from single-file overwrite to timestamped files (e.g. `scan_results_20260302_1800.json`) or append-mode JSONL. Allows recovery of missed scans and audit trail. |
| 18 | whatsapp-bridge: Business message support | High | TODO | Bridge does not capture WhatsApp Business messages (template messages, interactive messages, list messages). Chat entry is created in DB but zero messages stored. Discovered via Cainiao Network HK (8618413279466). Need to investigate whatsmeow library support for business message types and add parsing. |
| 19 | Post-recovery timer audit | Medium | TODO | After any system restart, automatically verify all scheduled timers executed successfully by checking systemd journal. Flag any timer that ran but had delivery failures (FIFO write errors, script errors). Add to startup_selfcheck.py or as a separate recovery step. |
| 20 | Periodic scan: cover all active chats | Medium | TODO | Message scan currently only checks whitelisted groups/contacts. Add a broad scan of all recently active chats (via list_chats sorted by last_active) to detect messages from unknown contacts (couriers, new school groups, etc.). Run at lower frequency (e.g. every 6h) to avoid noise. |
| 21 | Zulip adapter: session resilience on restart | High | TODO | **Problem**: Adapter restart → PID files deleted → next message triggers `_lazy_create()` → kills existing tmux sessions (destroys active Claude conversations). **Root cause**: 2026-03-03 09:30 dev-topic Claude ran `systemctl restart ccmux-zulip-adapter`, adapter lost all instance state. **Fix plan** (in order): (1) Notification: post Zulip messages when sessions are killed/recreated so users know what happened. (2) Adopt logic: `_lazy_create()` should detect live tmux sessions and adopt them (rebuild injector + PID) instead of killing. (3) Guard: prevent topic-Claude from restarting the adapter (PreToolUse hook or CLAUDE.md rule). |
| 22 | Zulip intermediate output visibility | High | TODO | **Problem**: During Claude processing, Zulip shows nothing — output only appears after turn completes (Stop hook). **Fix plan**: Add PostToolUse hook (`zulip_posttool_hook.py`) that posts brief tool activity updates to Zulip after each tool call (e.g. "📂 Reading file.py", "⚙️ Running tests"). Gives step-by-step visibility. Also: post injection ACK ("⏳ Working...") when message is injected. |
| 23 | whatsapp-bridge: reply/quote context inconsistent | Medium | TODO | **Problem**: When a user directly replies to (quotes) a message, the `list_messages` API sometimes returns the `↳ replying to` context and sometimes doesn't. Discovered 2026-03-03: admin replied to evening report at 20:26 — no quote context returned; follow-up reply at 20:27 to the same message — quote context correctly returned. **Fix**: Investigate whatsmeow quoted message metadata extraction, ensure `ContextInfo.QuotedMessage` is always parsed and included in `list_messages` output. |

## Completed

| # | Task | Date | Notes |
|---|------|------|-------|
| — | Gmail scanner | 2026-02-25 | IMAP scanner deployed, timer active every 2h 06:00-20:00. |
| — | Git history privacy rewrite | 2026-02-25 | filter-repo across 22 commits, 16 patterns. 5 audit rounds passed. |
| — | GitHub repo migration (ccmx→ccmux) | 2026-02-25 | Remote URL switched, all history pushed. |
