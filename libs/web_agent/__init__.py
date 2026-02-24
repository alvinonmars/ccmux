"""Screenshot-driven web automation framework for Claude Code agents.

Provides browser session management with persistent state, enabling agents
to navigate web portals step-by-step using screenshot recognition.
"""

from libs.web_agent.browser import BrowserSession
from libs.web_agent.prompts import web_task_prompt

__all__ = ["BrowserSession", "web_task_prompt"]
