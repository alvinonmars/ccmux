"""Prompt templates for spawning web automation agents.

Usage from main session:

    from libs.web_agent.prompts import web_task_prompt

    prompt = web_task_prompt(
        task="Sign up for 2025/26 G1 Field Trip on PowerSchool",
        auth="powerschool",
        start_url="https://portal.school.example.com/guardian/forms.html",
        requester_jid="<your-jid>@s.whatsapp.net",
    )

    # Spawn agent
    Task(subagent_type="general-purpose", model="opus", prompt=prompt)
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

import sys
sys.path.insert(0, str(PROJECT_ROOT))
from ccmux.paths import TMP_DIR


def web_task_prompt(
    task: str,
    auth: str = "powerschool",
    start_url: str = "",
    requester_jid: str = "",
    extra_context: str = "",
    state_dir: str = "/tmp/web_agent_state",
    screenshot_dir: str = "",
) -> str:
    """Generate a prompt for a web automation agent.

    Args:
        task: What the agent should accomplish (natural language).
        auth: Auth module to use ('powerschool' or 'none').
        start_url: URL to navigate to after login.
        requester_jid: WhatsApp JID to send progress screenshots to.
        extra_context: Additional context (form fields, expected content, etc.).
        state_dir: Directory for browser state persistence.
        screenshot_dir: Directory for screenshots. Defaults to ~/.ccmux/data/household/tmp/.
    """
    if not screenshot_dir:
        screenshot_dir = str(TMP_DIR)

    auth_section = ""
    if auth == "powerschool":
        auth_section = """
## Authentication

Use PowerSchool ADFS SSO login:

```python
from libs.web_agent.auth.powerschool import login, load_credentials
creds = load_credentials()
login(browser, creds)
```
"""
    elif auth == "none":
        auth_section = "\n## Authentication\n\nNo login required.\n"

    report_section = ""
    if requester_jid:
        report_section = f"""
## Progress Reporting

Send screenshots to the requester after each major step for transparency.
Use the WhatsApp MCP tools:

```python
# After taking a screenshot, send it:
mcp__whatsapp__send_file(recipient="{requester_jid}", media_path=screenshot_path)
mcp__whatsapp__send_message(recipient="{requester_jid}", message="🤖 Step N: <description of what you see>")
```

Send a screenshot + brief description after EVERY navigation or interaction step.
"""

    extra_section = ""
    if extra_context:
        extra_section = f"\n## Additional Context\n\n{extra_context}\n"

    return f"""# Web Automation Task

## Objective

{task}

## Working Directory

cd to `{PROJECT_ROOT}` before running any Python scripts.
{auth_section}{extra_section}
## Tools

You have `libs/web_agent/BrowserSession` for browser automation.
State directory: `{state_dir}`
Screenshot directory: `{screenshot_dir}`

## Method: Phase-Based Screenshot Loop

Work in **phases**. Each phase is a short Python script that runs in one browser session.
Between phases, READ the screenshots to understand what's on the page, then decide the next action.

### Phase Pattern

```python
import json, sys
sys.path.insert(0, "{PROJECT_ROOT}")
from libs.web_agent import BrowserSession

with BrowserSession(
    state_dir="{state_dir}",
    screenshot_dir="{screenshot_dir}",
) as browser:
    # Navigate (cookies restored automatically from state_dir)
    browser.goto("<url>")
    browser.wait(2000)

    # Take screenshot — you will READ this after the script finishes
    path = browser.screenshot("<step_name>")
    print(f"Screenshot: {{path}}")

    # Get structured page info if needed
    info = browser.page_info()
    print(f"URL: {{info['url']}}")
    print(f"Title: {{info['title']}}")

    # For forms, inspect field structure
    forms = browser.get_forms()
    print(json.dumps(forms, indent=2))
```

### Loop

1. **Write** a phase script (login + navigate to target)
2. **Run** it via Bash with `xvfb-run -a .venv/bin/python`
3. **Read** the screenshot file using the Read tool (you have vision — you can see images)
4. **Analyze** what's on the page and decide the next action
5. **Write** the next phase script (click, fill, navigate)
6. **Run** → **Read** → **Analyze** → repeat
7. **Stop** before any form submission — report what you've filled and wait

### Important Rules

- ALWAYS use `xvfb-run -a` when running scripts (headless Linux, no display)
- ALWAYS read screenshots between phases — do NOT blindly chain actions
- Each phase starts a NEW browser process but RESTORES cookies from state_dir
- After login, cookies persist — you don't need to re-login in subsequent phases
- If a page redirects to the login page, the session expired — re-login
- Use `browser.get_forms()` to understand form structure before filling
- Use `browser.get_links()` to find clickable elements
- Use `browser.click(text="...")` for clicking links/buttons by visible text
- Use `browser.fill(selector, value)` for form fields
- Use `browser.select(selector, value)` for dropdowns
{report_section}
## Form Submission Protocol

**NEVER submit a form without reporting first.** Before clicking Submit/Confirm:
1. Take a screenshot of the filled form
2. Send the screenshot to the requester
3. List all filled fields and their values
4. State: "Ready to submit. Awaiting confirmation."
5. STOP and return — the main session will decide whether to proceed

## Error Handling

- If login fails: report immediately, do not retry blindly
- If page is unexpected: take screenshot, report what you see, ask for guidance
- If session expires mid-task: re-login and navigate back to where you were
- If a form field is unclear: report the field structure and ask for guidance

Start by running the login + initial navigation phase.
"""
