# Incident Report: Missed Notification & Delayed Email Response

**Date**: 2026-03-04
**Severity**: Low (no data loss, no missed deadlines)
**Duration**: ~2 hours (16:05 restart → 18:20 resolution)

## Summary

After an unplanned system restart caused by accidental power disconnection, the AI assistant failed to: (1) proactively report a service scan failure, and (2) promptly locate a relevant email when asked. Both failures stem from incomplete context recovery after restart, not from system design flaws.

## Timeline

| Time | Event |
|------|-------|
| 08:30 | ISF email scanner runs. Login fails due to stale browser cookies (redirected to a logout page instead of ADFS login). Scanner saves failure screenshot and sends FIFO notification. |
| 08:30–15:50 | System running normally. Gmail scanner runs at 10:00 (fetched 1 email), 12:00 (fetched 2 emails including a recruiter confirmation), 14:00 (0 new). All notifications processed by the active session. |
| ~15:50 | Desktop computer accidentally unplugged by household staff. All services go down. |
| 16:05 | System reboots. ccmux services restart. New AI session starts. Startup selfcheck runs. |
| 16:05 | Selfcheck report generated. Lists `email-scanner: failed`. AI reads report, sends admin a summary, but **does not explicitly flag the email scan failure or attempt diagnosis**. |
| 16:05 | Gmail scanner runs post-boot. Fetches 0 new emails (all already processed by pre-restart session, UID watermark is current). |
| 17:21 | Admin asks "did you check the email? should we reply?" |
| 17:21 | AI checks ISF Outlook scan results (wrong mailbox). Finds the login failure from 08:30. Reports it to admin but **does not check Gmail simultaneously**. |
| 17:46 | After further prompting, AI searches Gmail via IMAP, finds the recruiter email (received 10:20, already scanned at 12:00). |
| 17:46 | Admin points out: (a) scan failures must be reported immediately, (b) Gmail scanner was already working fine, (c) the AI's judgment failed. |
| 18:20 | Full resolution: email reply sent, root cause identified. |

## Root Cause Analysis

### Primary Cause: Incomplete Context Recovery

The unplanned restart at 15:50 destroyed all in-session context. The recovery protocol (selfcheck report) provided a mechanical summary but the AI treated it as a checklist rather than a diagnostic tool.

Specific gaps:
1. **Failed service not escalated**: Selfcheck report listed `email-scanner: failed` but the AI did not query the service logs, attempt a re-run, or notify admin of the specific failure.
2. **Scanner results not re-reviewed**: The Gmail scanner had processed the recruiter email at 12:00 in the pre-restart session. Post-restart, this context was lost. The AI did not re-read the day's scan results to rebuild awareness.
3. **Single-mailbox assumption**: When asked about "email", the AI defaulted to ISF Outlook instead of checking all mailboxes (ISF + Gmail).

### Contributing Factor: Mid-Day Restart

The system is designed for continuous operation. A full-day session accumulates context organically — every scanner notification, every message, every event builds awareness. A mid-day restart resets this to zero, and the recovery protocol was not thorough enough to compensate.

### What Was NOT the Cause

- **Gmail scanner design**: Working correctly. Runs every 2 hours (06–20:00, 8 times/day). All 8 runs on 2026-03-04 completed successfully. The recruiter email was fetched at 12:00 and notification was delivered to the FIFO.
- **ISF email scanner design**: The ADFS SSO login flow is fully automated using stored credentials. The 08:30 failure was caused by stale browser cookies in `/tmp/` (cleared on reboot). A fresh run at 18:25 succeeded without any manual intervention — confirming the system is self-healing after restart.
- **Service architecture**: All timers, services, and scanners functioned as designed.

## Impact

- Recruiter email confirmation (received 10:20) was not surfaced to admin until 17:46 — ~7.5 hour delay. No actual consequence since the email was informational (confirming an already-known interview schedule).
- ISF email scan failure went unreported for ~10 hours. One school email (class team-building event on Mar 13) was delayed but non-urgent.

## Corrective Actions (Behavioral — No Code Changes)

| # | Action | Type |
|---|--------|------|
| 1 | **Recovery protocol enhancement**: After any restart, actively query the day's scanner logs (journalctl) and re-read scan results — not just the selfcheck summary. Rebuild full-day awareness before resuming normal operations. | Process |
| 2 | **Failed service = immediate escalation**: Any service showing `failed` status in selfcheck must be: (a) reported to admin immediately, (b) diagnosed via logs, (c) re-triggered if possible. | Behavioral |
| 3 | **"Check email" = check ALL mailboxes**: When admin asks about email, always check both ISF and Gmail scan results before responding. | Behavioral |
| 4 | **Proactive anomaly scanning during recovery**: During selfcheck recovery, treat every non-green item as requiring active investigation, not passive acknowledgment. | Behavioral |

## Lessons Learned

1. **Continuous context ≠ persistent context**: A full-day session builds implicit awareness that cannot be captured in a summary file. Recovery must actively reconstruct this awareness, not just read a checklist.
2. **The scanner infrastructure is robust**: Gmail runs 8x/day, ISF runs daily, both auto-login with stored credentials, both self-heal after restart. The weak link was the AI layer's handling of the context gap.
3. **Service owner mindset**: The AI must treat itself as the owner of every monitored service. An owner's reaction to "service failed" is "fix it now and tell the stakeholder", not "noted, moving on."
