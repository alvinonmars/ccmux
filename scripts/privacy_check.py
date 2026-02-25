#!/usr/bin/env python3
"""Pre-commit privacy scanner — blocks commits containing PII.

Two-layer architecture:
  Layer 1 (regex, automated): Generic patterns (in-repo, safe for public) +
      personal blocklist from ~/.ccmux/secrets/privacy_blocklist.txt (gitignored).
      Runs automatically in the pre-commit hook.
  Layer 2 (token-based AI review gate): Before every git commit, 3 independent
      Claude Code Task agents review the staged diff. When all 3 pass, a token
      file (.privacy_review_token) is written containing the SHA256 hash of the
      staged diff. The pre-commit hook verifies this token — no token means the
      AI review was skipped, and the commit is blocked. Token is one-time use
      and deleted immediately after verification. NO API calls — uses Claude Code
      Max subscription only.

Usage:
    As pre-commit hook:      hooks/pre-commit calls this script
    Manual full scan:        python scripts/privacy_check.py --all
    Scan staged only:        python scripts/privacy_check.py
    Generate review token:   python scripts/privacy_check.py --generate-token
    Print staged diff:       python scripts/privacy_check.py --review

Exit code 0 = clean / token generated, 1 = PII found / commit blocked.

Blocklist setup (~/.ccmux/secrets/privacy_blocklist.txt):
    One pattern per line. Lines starting with # are comments.
    Patterns are matched case-insensitively as word boundaries.
    Example:
        # Family names
        Alice
        Bob
        # Paths
        /home/myuser
        # Domains
        myemail@gmail.com
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BLOCKLIST_PATH = Path(
    os.environ.get(
        "CCMUX_PRIVACY_BLOCKLIST",
        str(Path.home() / ".ccmux" / "secrets" / "privacy_blocklist.txt"),
    )
)

ALLOWLIST_PATH = Path(
    os.environ.get(
        "CCMUX_PRIVACY_ALLOWLIST",
        str(Path.home() / ".ccmux" / "secrets" / "privacy_allowlist.txt"),
    )
)

# Token file used by Layer 2 gate. Written after AI review passes; deleted after
# pre-commit hook verifies it. Path is relative to the git repo root.
_REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    ).stdout.strip() or "."
)
TOKEN_FILE = _REPO_ROOT / ".privacy_review_token"

# ---------------------------------------------------------------------------
# Layer 1: Generic patterns (safe to publish — no personal data here)
# ---------------------------------------------------------------------------

GENERIC_PATTERNS: list[tuple[str, str, int]] = [
    # Phone numbers (international formats likely to be personal)
    (r"852[0-9]{8}", "PHONE_NUMBER", re.IGNORECASE),
    (r"86[1-9][0-9]{9,10}", "PHONE_NUMBER", re.IGNORECASE),

    # WhatsApp JIDs
    (r"\d{15,21}@g\.us", "WHATSAPP_JID", 0),
    (r"852\d{8}@s\.whatsapp\.net", "WHATSAPP_JID", 0),
    (r"86[1-9]\d{9,10}@s\.whatsapp\.net", "WHATSAPP_JID", 0),
    (r"\d{12,18}@lid", "WHATSAPP_JID", 0),

    # Credentials (actual values, not code references)
    (r"APP_PASSWORD\s*=\s*[a-z]{4}\s+[a-z]{4}", "CREDENTIAL", re.IGNORECASE),
    (r"\bPASS(?:WORD)?\s*=\s*[A-Za-z0-9_/.@~-]{6,}", "CREDENTIAL", re.IGNORECASE),
    (r"\bSECRET\s*=\s*[A-Za-z0-9_/.@~-]{6,}", "CREDENTIAL", re.IGNORECASE),
    (r"\bTOKEN\s*=\s*[A-Za-z0-9_/.@~-]{10,}", "CREDENTIAL", re.IGNORECASE),
    (r"sk-[a-zA-Z0-9]{20,}", "API_KEY", 0),
    (r"ghp_[a-zA-Z0-9]{36}", "API_KEY", 0),
]

# ---------------------------------------------------------------------------
# Allowlist — loaded from external file or defaults
# ---------------------------------------------------------------------------

DEFAULT_ALLOWLISTED_STRINGS = {
    "10000000000",
    "100000000000000000",
    "<phone>@s.whatsapp.net",
    "<your-jid>@s.whatsapp.net",
}

ALLOWLISTED_FILES = {
    ".claude/CLAUDE.md",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".zip", ".tar", ".gz", ".bz2", ".7z",
    ".pyc", ".pyo", ".so", ".dll", ".dylib",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".db", ".sqlite", ".sqlite3",
}


# ---------------------------------------------------------------------------
# Load external config files
# ---------------------------------------------------------------------------

def load_blocklist(path: Path = BLOCKLIST_PATH) -> list[tuple[str, str, int]]:
    """Load personal PII patterns from external blocklist file.

    Each non-empty, non-comment line becomes a word-boundary regex pattern.
    Returns list of (pattern, category, flags) tuples.
    """
    patterns = []
    if not path.exists():
        return patterns

    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Escape regex special chars, wrap in word boundaries
            escaped = re.escape(line)
            patterns.append((rf"\b{escaped}\b", "BLOCKLIST", re.IGNORECASE))

    return patterns


def load_allowlist(path: Path = ALLOWLIST_PATH) -> set[str]:
    """Load allowlisted strings from external file.

    Each non-empty, non-comment line is an allowlisted substring.
    If a line appears in the scanned content, that match is skipped.
    """
    strings = set(DEFAULT_ALLOWLISTED_STRINGS)
    if not path.exists():
        return strings

    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            strings.add(line)

    return strings


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def is_binary(path: str) -> bool:
    return Path(path).suffix.lower() in BINARY_EXTENSIONS


def get_staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True,
    )
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def get_all_tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True,
    )
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def get_staged_content(filepath: str) -> str:
    result = subprocess.run(
        ["git", "show", f":{filepath}"], capture_output=True, text=True,
    )
    return result.stdout


def get_staged_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "--cached"], capture_output=True, text=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Layer 1: Regex scanning
# ---------------------------------------------------------------------------

def scan_content(
    filepath: str,
    content: str,
    patterns: list[tuple[str, str, int]],
    allowlist: set[str],
) -> list[dict]:
    findings = []

    for line_num, line in enumerate(content.splitlines(), 1):
        if any(allowed in line for allowed in allowlist):
            continue

        for pattern, category, flags in patterns:
            for match in re.finditer(pattern, line, flags):
                matched_text = match.group()
                if matched_text in allowlist:
                    continue
                findings.append({
                    "file": filepath,
                    "line": line_num,
                    "category": category,
                    "match": matched_text,
                    "context": line.strip()[:120],
                })

    return findings


def scan_files(
    files: list[str],
    patterns: list[tuple[str, str, int]],
    allowlist: set[str],
    staged_only: bool = True,
) -> list[dict]:
    all_findings = []

    for filepath in files:
        if filepath in ALLOWLISTED_FILES:
            continue
        if is_binary(filepath):
            continue

        if staged_only:
            content = get_staged_content(filepath)
        else:
            try:
                content = Path(filepath).read_text(errors="replace")
            except (OSError, UnicodeDecodeError):
                continue

        if not content:
            continue

        findings = scan_content(filepath, content, patterns, allowlist)
        all_findings.extend(findings)

    return all_findings


# ---------------------------------------------------------------------------
# Layer 2: AI reviewer personas and prompts
# ---------------------------------------------------------------------------

REVIEW_PROMPT_VERSION = "1.0"


@dataclass(frozen=True)
class ReviewerPersona:
    name: str
    focus: str
    instructions: str


REVIEW_PREAMBLE = """You are a privacy reviewer for a git commit. Your job is to inspect
the staged diff below and determine whether it contains Personally Identifiable Information
(PII) that should NOT be committed to a (potentially public) repository.

CONTEXT
-------
Layer 1 (regex scan) has already passed. Your role is Layer 2: catch PII that regex misses —
semantic identifiers, obfuscated data, indirect identifiers, and contextual combinations.

PII CATEGORIES TO LOOK FOR
---------------------------
- Names: real human names in any language or script, nicknames, partial names
- Contacts: phone numbers, email addresses, messaging IDs (WhatsApp JIDs, Telegram handles)
- Locations: home addresses, specific building names, neighbourhood-level detail tied to a person
- Credentials: API keys, tokens, passwords, session cookies, app-specific passwords
- Paths: absolute filesystem paths containing real usernames (e.g. /home/realuser/)
- Contextual PII: combinations of data that together identify a specific individual
  (e.g. school name + grade + activity schedule for a named child)
- Health/financial info: medical details, bank account numbers, transaction records tied to a person

WHAT IS NOT PII
---------------
- Generic placeholder text: <name>, <email>, <phone>, YOUR_NAME, example@example.com
- Code references: variable names like `user_name`, function names, class names
- Test fixtures with clearly fake/random data (e.g. "Alice" in an isolated unit test with no
  real context, test phone numbers like 555-0100)
- Pattern definitions in the scanner itself (regex strings are not PII)
- Generic category labels: "admin", "user", "helper", "child" without real identifying detail
- Publicly known institutional names without accompanying personal detail

OUTPUT FORMAT
-------------
Reply with EXACTLY one of:
  PASS
  FAIL
  <one-line explanation of what was found>

If in doubt, prefer FAIL over PASS. A false positive is recoverable; a false negative leaks
real personal data. Do not add any other text before or after your verdict.

STAGED DIFF
-----------"""


REVIEWER_PERSONAS: list[ReviewerPersona] = [
    ReviewerPersona(
        name="Reviewer-Identity",
        focus="Names, contacts, and direct identifiers",
        instructions="""Focus exclusively on DIRECT IDENTIFIERS:

- Real human names: any language, any script (Latin, CJK, Arabic, etc.), including
  partial names, nicknames, and diminutives. "John", "Johnny", "Mei-Lin", "太郎" are all names.
- Phone numbers: any format, any country, including partial numbers embedded in longer strings
- Email addresses: any format, including ones disguised with [at] or spaces
- Messaging identifiers: WhatsApp JIDs (@s.whatsapp.net, @g.us, @lid), Telegram usernames,
  Signal numbers, Line IDs
- Social media handles or usernames that identify a real person
- Names appearing in: comments, docstrings, string literals, commit author fields,
  log format strings, configuration values

Be especially alert to non-ASCII names and names split across variables or concatenated strings.
""",
    ),
    ReviewerPersona(
        name="Reviewer-Secrets",
        focus="Credentials, paths, and infrastructure details",
        instructions="""Focus exclusively on CREDENTIALS AND INFRASTRUCTURE PII:

- API keys, access tokens, bearer tokens, OAuth tokens (any service)
- Passwords or passphrases, including app-specific passwords
- Session cookies, authentication headers with real values
- Private keys or certificates (PEM blocks, key material)
- Absolute filesystem paths that reveal a real username:
  e.g. /home/realuser/, /Users/realuser/, C:\\Users\\realuser\\
- Internal hostnames, internal IP addresses, internal domain names
- Database connection strings with real credentials or hostnames
- SMTP/IMAP server credentials, mail server hostnames with account info
- Commented-out credentials (often left behind after a refactor)
- Test fixtures that look TOO real: a test "API key" that matches a known key format
  with what looks like real entropy (not obviously fake)
- Environment variable assignments where the VALUE appears to be real (not a placeholder)

Be especially alert to secrets in comments and to credentials that were temporarily
hardcoded and "commented out" rather than removed.
""",
    ),
    ReviewerPersona(
        name="Reviewer-Context",
        focus="Indirect and contextual PII",
        instructions="""Focus exclusively on CONTEXTUAL AND INDIRECT PII — data that may
not look sensitive in isolation but becomes identifying when combined with other
information already present in the repository or the diff itself:

- Schedule details tied to a named or identifiable person:
  e.g. a class schedule with a child's name, day, time, and activity centre name
- Location details that narrow down a family's identity:
  e.g. specific school name + grade level + residential district + child's first name
- Health information: diagnoses, medications, symptoms, bowel/health logs for named individuals
- Financial details: specific transaction amounts, merchant names, recurring expense patterns
  linked to an identifiable household
- Routine patterns: a named person's daily schedule, commute details, work hours
- Combinations that cross-identify: even if each field alone is generic, flag combinations
  where school + grade + activity + name together identify a specific child or family
- Relationship graphs: named contacts with their roles (e.g. "helper", "tutor") alongside
  schedule and location details

Ask yourself: "If I found this diff on GitHub, could I identify a real person or family?"
If yes, FAIL.
""",
    ),
]


# ---------------------------------------------------------------------------
# Layer 2: Token-based AI review gate (NO API calls)
# ---------------------------------------------------------------------------
# Before committing, 3 independent Claude Code Task agents review the staged
# diff. When all 3 return PASS, `--generate-token` is called to write the
# token. The pre-commit hook verifies the token and deletes it (one-time use).
# Missing token or hash mismatch = commit blocked (fail-closed).


def _compute_review_hash() -> str:
    """Return SHA256 hex digest of review prompt version, all prompt content, and the staged diff.

    Including prompt content in the hash ensures that changes to the review
    instructions invalidate any token generated under the old prompts.
    """
    diff = get_staged_diff()
    prompt_content = REVIEW_PROMPT_VERSION + REVIEW_PREAMBLE
    for persona in REVIEWER_PERSONAS:
        prompt_content += persona.name + persona.focus + persona.instructions
    return hashlib.sha256((prompt_content + diff).encode()).hexdigest()


def generate_token() -> int:
    """Compute SHA256 of staged diff and write it to TOKEN_FILE.

    Called after all 3 AI review agents return PASS.
    Returns 0 on success, 1 on error.
    """
    diff = get_staged_diff()
    if not diff.strip():
        print("Privacy review token: no staged diff found — nothing to review.")
        print("Stage files first with `git add` before generating a token.")
        return 1

    digest = _compute_review_hash()
    TOKEN_FILE.write_text(digest + "\n")
    print(f"Privacy review token written: {TOKEN_FILE}")
    print(f"  Hash: {digest}")
    print("  Token is one-time use — it will be deleted after the next commit.")
    return 0


def verify_token() -> tuple[bool, str]:
    """Verify that TOKEN_FILE exists and matches the current staged diff hash.

    Returns (valid: bool, message: str).
    On success the token file is deleted immediately (one-time use).
    """
    if not TOKEN_FILE.exists():
        return (
            False,
            (
                "\n" + "=" * 70 + "\n"
                "  BLOCKED: Layer 2 privacy review token not found.\n"
                "  AI review was not performed before this commit.\n"
                "  The pre-commit hook requires a valid privacy review token.\n\n"
                "  To commit:\n"
                "    1. Run AI privacy review (3 independent reviewers must all PASS).\n"
                "    2. If all reviewers pass, generate the token:\n"
                "         python scripts/privacy_check.py --generate-token\n"
                "    3. Then re-run `git commit`.\n\n"
                "  Using Claude Code: ask it to review the staged diff for PII.\n"
                "  Without Claude Code: manually review with --review flag.\n"
                + "=" * 70 + "\n"
            ),
        )

    stored_hash = TOKEN_FILE.read_text().strip()
    current_hash = _compute_review_hash()

    if stored_hash != current_hash:
        # Leave the stale token in place — the user may want to inspect it.
        return (
            False,
            (
                "\n" + "=" * 70 + "\n"
                "  BLOCKED: Privacy review token hash mismatch.\n"
                "  Staged changes differ from what was reviewed.\n"
                "  Re-run privacy review after finalising your staged changes.\n\n"
                "  Steps:\n"
                "    1. Ask Claude Code to re-review the current staged diff.\n"
                "    2. python scripts/privacy_check.py --generate-token\n"
                "    3. git commit\n"
                + "=" * 70 + "\n"
            ),
        )

    # Valid — delete token immediately (one-time use)
    TOKEN_FILE.unlink()
    return True, "Privacy review token: VALID (token consumed)"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(findings: list[dict]) -> None:
    if not findings:
        print("Privacy check: CLEAN (Layer 1 regex passed)")
        return

    print(f"\n{'='*70}")
    print(f"  PRIVACY CHECK FAILED — Layer 1 (Regex)")
    print(f"{'='*70}\n")

    print(f"  {len(findings)} pattern(s) found\n")

    by_category: dict[str, list[dict]] = {}
    for f in findings:
        by_category.setdefault(f["category"], []).append(f)

    for category, items in sorted(by_category.items()):
        print(f"  [{category}] ({len(items)} match{'es' if len(items) > 1 else ''})")
        for item in items:
            print(f"    {item['file']}:{item['line']}  ->  {item['match']}")
            print(f"      {item['context']}")
        print()

    print(f"{'='*70}")
    print("  Commit BLOCKED. Fix the issues above, or update:")
    print(f"  Blocklist: {BLOCKLIST_PATH}")
    print(f"  Allowlist: {ALLOWLIST_PATH}")
    print(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = sys.argv[1:]
    full_scan = "--all" in args

    # --- Special mode: --review (no blocklist needed) ---
    if "--review" in args:
        diff = get_staged_diff()
        if not diff.strip():
            print("No staged diff to review. Stage files with `git add` first.")
            return 0
        print(diff, end="")
        return 0

    # --- Special mode: --review-prompt (no blocklist needed) ---
    if "--review-prompt" in args:
        reviewer_idx: int | None = None
        if "--reviewer" in args:
            idx_pos = args.index("--reviewer") + 1
            if idx_pos < len(args):
                try:
                    reviewer_idx = int(args[idx_pos])
                except ValueError:
                    print(f"Error: --reviewer expects an integer (0, 1, or 2), got: {args[idx_pos]}")
                    return 1
                if reviewer_idx not in range(len(REVIEWER_PERSONAS)):
                    print(f"Error: --reviewer must be 0, 1, or 2 (got {reviewer_idx})")
                    return 1
            else:
                print("Error: --reviewer requires an argument (0, 1, or 2)")
                return 1

        if reviewer_idx is not None:
            persona = REVIEWER_PERSONAS[reviewer_idx]
            print(f"# {persona.name} — {persona.focus}\n")
            print(REVIEW_PREAMBLE)
            print(f"\n# REVIEWER INSTRUCTIONS\n")
            print(persona.instructions)
        else:
            for i, persona in enumerate(REVIEWER_PERSONAS):
                print(f"# Reviewer {i}: {persona.name} — {persona.focus}\n")
                print(REVIEW_PREAMBLE)
                print(f"\n# REVIEWER INSTRUCTIONS\n")
                print(persona.instructions)
                if i < len(REVIEWER_PERSONAS) - 1:
                    print("\n" + "=" * 70 + "\n")
        return 0

    # --- Blocklist is REQUIRED (fail-closed) ---
    if not BLOCKLIST_PATH.exists():
        print(f"\n{'='*70}")
        print("  PRIVACY CHECK FAILED — blocklist file missing")
        print(f"{'='*70}")
        print(f"\n  Required: {BLOCKLIST_PATH}")
        print("  Create it with personal names/paths/emails to block (one per line).")
        print("  This file is gitignored and must exist for commits to proceed.")
        print(f"\n{'='*70}\n")
        return 1

    # Load personal patterns from external files
    blocklist_patterns = load_blocklist()
    if not blocklist_patterns:
        print(f"\n{'='*70}")
        print("  PRIVACY CHECK FAILED — blocklist file is empty")
        print(f"{'='*70}")
        print(f"\n  File: {BLOCKLIST_PATH}")
        print("  Add at least one pattern (personal name, path, email, etc.).")
        print(f"\n{'='*70}\n")
        return 1

    all_patterns = GENERIC_PATTERNS + blocklist_patterns
    allowlist = load_allowlist()
    print(f"Privacy check: loaded {len(blocklist_patterns)} blocklist pattern(s)")

    # --- Layer 1: Regex scan ---
    if full_scan:
        print("Privacy check: scanning ALL tracked files...")
        files = get_all_tracked_files()
        findings = scan_files(files, all_patterns, allowlist, staged_only=False)
    else:
        files = get_staged_files()
        if not files:
            print("Privacy check: no staged files, skipping")
            return 0
        print(f"Privacy check: scanning {len(files)} staged file(s)...")
        findings = scan_files(files, all_patterns, allowlist, staged_only=True)

    # --- Report Layer 1 results ---
    print_report(findings)

    if findings:
        return 1

    # --- Generate token: only after Layer 1 passes ---
    if "--generate-token" in args:
        return generate_token()

    # --- Layer 2: Token verification (fail-closed) ---
    # Skip token check for --all full scans (not a commit path).
    if not full_scan:
        valid, message = verify_token()
        print(message)
        if not valid:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
