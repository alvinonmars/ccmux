"""Gmail SMTP send + IMAP read.

Credentials loaded from ~/.ccmux/secrets/gmail.env:

    GMAIL_ADDRESS=you@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

The app password must be generated from Google Account > Security >
2-Step Verification > App Passwords. Regular password will NOT work
if 2FA is enabled (which it should be).

Usage:

    from libs.email.gmail import send_email, list_inbox

    # Send
    send_email(
        to="recipient@example.com",
        subject="Re: Meeting",
        body="Looking forward to the discussion.",
        reply_to_message_id="<original-message-id@mail.gmail.com>",
    )

    # Read inbox
    emails = list_inbox(limit=10, unseen_only=True)
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import smtplib
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ccmux.paths import GMAIL_ENV

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


def load_credentials(env_path: Path = GMAIL_ENV) -> dict[str, str]:
    """Read Gmail credentials from the .env file."""
    creds: dict[str, str] = {}
    if not env_path.exists():
        raise FileNotFoundError(f"Gmail credential file not found: {env_path}")
    with open(env_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            creds[key.strip()] = value.strip()
    required = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
    for key in required:
        if key not in creds:
            raise ValueError(f"Missing {key} in {env_path}")
    return creds


def send_email(
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    reply_to_message_id: str | None = None,
    references: str | None = None,
    in_reply_to: str | None = None,
    html: bool = False,
    attachments: list[str | Path] | None = None,
    creds: dict[str, str] | None = None,
) -> bool:
    """Send an email via Gmail SMTP.

    Args:
        to: Recipient address(es).
        subject: Email subject.
        body: Plain text (or HTML if html=True) body.
        cc: CC address(es).
        reply_to_message_id: Message-ID of the email being replied to.
            Sets both In-Reply-To and References headers for proper
            threading in Gmail/Outlook.
        references: Explicit References header (overrides auto-generation).
        in_reply_to: Explicit In-Reply-To header (overrides reply_to_message_id).
        html: If True, body is treated as HTML.
        attachments: List of file paths to attach.
        creds: Credentials dict. If None, loaded from GMAIL_ENV.

    Returns:
        True if sent successfully.
    """
    if creds is None:
        creds = load_credentials()

    addr = creds["GMAIL_ADDRESS"]
    app_pw = creds["GMAIL_APP_PASSWORD"]

    if isinstance(to, str):
        to = [to]
    if isinstance(cc, str):
        cc = [cc]

    msg = MIMEMultipart("alternative") if html else MIMEMultipart()
    msg["From"] = addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=addr.split("@")[1])

    # Threading headers for replies
    if reply_to_message_id:
        msg["In-Reply-To"] = in_reply_to or reply_to_message_id
        msg["References"] = references or reply_to_message_id
    elif in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references

    content_type = "html" if html else "plain"
    msg.attach(MIMEText(body, content_type, "utf-8"))

    # Attach files
    for filepath in (attachments or []):
        filepath = Path(filepath)
        if not filepath.exists():
            log.warning("Attachment not found, skipping: %s", filepath)
            continue
        with open(filepath, "rb") as fh:
            part = MIMEApplication(fh.read(), Name=filepath.name)
        part["Content-Disposition"] = f'attachment; filename="{filepath.name}"'
        msg.attach(part)

    all_recipients = list(to) + (cc or [])

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(addr, app_pw)
            server.sendmail(addr, all_recipients, msg.as_string())
        log.info("Email sent to %s, subject: %s", ", ".join(to), subject)
        return True
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
        return False


def list_inbox(
    limit: int = 10,
    unseen_only: bool = False,
    folder: str = "INBOX",
    search_subject: str | None = None,
    search_from: str | None = None,
    creds: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read emails from Gmail via IMAP.

    Args:
        limit: Maximum number of emails to return (most recent first).
        unseen_only: If True, only return unread emails.
        folder: IMAP folder to read from.
        search_subject: Filter by subject (partial match).
        search_from: Filter by sender address (partial match).
        creds: Credentials dict. If None, loaded from GMAIL_ENV.

    Returns:
        List of dicts with: message_id, from, to, subject, date, body_text,
        body_html, is_unread.
    """
    if creds is None:
        creds = load_credentials()

    addr = creds["GMAIL_ADDRESS"]
    app_pw = creds["GMAIL_APP_PASSWORD"]

    results: list[dict[str, Any]] = []

    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(addr, app_pw)
            imap.select(folder, readonly=True)

            # Build search criteria
            criteria: list[str] = []
            if unseen_only:
                criteria.append("UNSEEN")
            if search_subject:
                criteria.append(f'SUBJECT "{search_subject}"')
            if search_from:
                criteria.append(f'FROM "{search_from}"')
            if not criteria:
                criteria.append("ALL")

            search_str = " ".join(criteria)
            _, msg_ids = imap.search(None, search_str)

            if not msg_ids or not msg_ids[0]:
                return results

            id_list = msg_ids[0].split()
            # Most recent first
            id_list = list(reversed(id_list[-limit:]))

            for mid in id_list:
                _, msg_data = imap.fetch(mid, "(RFC822 FLAGS)")
                if not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                parsed = email.message_from_bytes(raw_email)

                # Extract flags for read status
                flags_data = msg_data[0][0] if msg_data[0] else b""
                is_unread = b"\\Seen" not in flags_data

                # Extract body
                body_text = ""
                body_html = ""
                if parsed.is_multipart():
                    for part in parsed.walk():
                        ct = part.get_content_type()
                        if ct == "text/plain" and not body_text:
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or "utf-8"
                                body_text = payload.decode(charset, errors="replace")
                        elif ct == "text/html" and not body_html:
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or "utf-8"
                                body_html = payload.decode(charset, errors="replace")
                else:
                    payload = parsed.get_payload(decode=True)
                    if payload:
                        charset = parsed.get_content_charset() or "utf-8"
                        body_text = payload.decode(charset, errors="replace")

                results.append({
                    "message_id": parsed.get("Message-ID", ""),
                    "from": parsed.get("From", ""),
                    "to": parsed.get("To", ""),
                    "subject": parsed.get("Subject", ""),
                    "date": parsed.get("Date", ""),
                    "body_text": body_text[:5000],
                    "body_html": body_html[:5000] if body_html else "",
                    "is_unread": is_unread,
                    "references": parsed.get("References", ""),
                    "in_reply_to": parsed.get("In-Reply-To", ""),
                })

    except Exception as exc:
        log.error("IMAP read failed: %s", exc)

    return results


def find_email(
    subject_contains: str | None = None,
    from_contains: str | None = None,
    limit: int = 5,
    creds: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Search for a specific email by subject or sender.

    Convenience wrapper around list_inbox for finding a specific email
    thread (e.g., to get the Message-ID for replying).
    """
    return list_inbox(
        limit=limit,
        search_subject=subject_contains,
        search_from=from_contains,
        creds=creds,
    )
