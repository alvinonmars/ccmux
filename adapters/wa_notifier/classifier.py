"""Local intent classifier for household group messages.

Purely local heuristics — no external API calls.  Handles obvious cases
(S3 commands, videos, emoji-only messages, stickers) and passes everything
else to Claude Code as ``unknown`` for judgment.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)

# Regex matching strings composed entirely of emoji, whitespace, and
# common variation selectors / ZWJ sequences.
_EMOJI_ONLY_RE = re.compile(
    r"^[\s"
    r"\U0001F600-\U0001F64F"  # emoticons
    r"\U0001F300-\U0001F5FF"  # symbols & pictographs
    r"\U0001F680-\U0001F6FF"  # transport & map
    r"\U0001F900-\U0001F9FF"  # supplemental symbols
    r"\U0001FA00-\U0001FA6F"  # chess symbols
    r"\U0001FA70-\U0001FAFF"  # symbols extended-A
    r"\U00002702-\U000027B0"  # dingbats
    r"\U0000FE00-\U0000FE0F"  # variation selectors
    r"\U0000200D"             # ZWJ
    r"\U000020E3"             # combining enclosing keycap
    r"\U00002600-\U000026FF"  # misc symbols
    r"\U0000231A-\U0000231B"  # watch/hourglass
    r"\U00002934-\U00002935"  # arrows
    r"\U000025AA-\U000025FE"  # geometric shapes
    r"\U00002B05-\U00002B55"  # arrows/shapes
    r"\U0001F1E0-\U0001F1FF"  # flags
    r"]+$"
)


class Intent(str, Enum):
    """Classified intent for a household group message."""

    RECEIPT = "receipt"
    SCHEDULE_CHANGE = "schedule_change"
    HEALTH_RESPONSE = "health_response"
    FOOD_PHOTO = "food_photo"
    SCHOOL_PHOTO = "school_photo"
    QUESTION = "question"
    INFORMATION = "information"
    CASUAL = "casual"
    VIDEO = "video"
    S3_COMMAND = "s3_command"
    UNKNOWN = "unknown"


ACTIONABLE_INTENTS: frozenset[Intent] = frozenset({
    Intent.RECEIPT, Intent.SCHEDULE_CHANGE, Intent.HEALTH_RESPONSE,
    Intent.QUESTION, Intent.S3_COMMAND,
})

PASSIVE_INTENTS: frozenset[Intent] = frozenset({
    Intent.FOOD_PHOTO, Intent.SCHOOL_PHOTO, Intent.INFORMATION,
})

SILENT_INTENTS: frozenset[Intent] = frozenset({
    Intent.CASUAL, Intent.VIDEO,
})


@dataclass
class ClassificationResult:
    intent: Intent
    confidence: float
    reasoning: str
    action: str  # "respond" | "log" | "skip"

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "action": self.action,
        }


class IntentClassifier:
    """Classify household group messages using local heuristics only.

    Handles obvious cases locally (S3, video, emoji, sticker) and passes
    everything else to Claude Code as ``unknown``.  Zero external API calls.
    """

    def __init__(self) -> None:
        self._context_buffer: dict[str, list[str]] = {}

    def classify(
        self,
        text: str,
        sender: str,
        has_media: bool,
        media_type: str | None,
        chat_jid: str,
    ) -> ClassificationResult:
        """Classify a single message using local heuristics."""
        stripped = text.strip() if text else ""

        # 1. S3 prefix — actionable command
        if len(stripped) >= 2 and stripped[:2].upper() == "S3":
            self._update_context(chat_jid, text, sender)
            return ClassificationResult(
                Intent.S3_COMMAND, 1.0, "S3 prefix detected", "respond",
            )

        # 2. Video — always silent
        if media_type == "video":
            return ClassificationResult(
                Intent.VIDEO, 1.0, "Video media type", "skip",
            )

        # 3. Sticker — casual, silent
        if media_type == "sticker":
            return ClassificationResult(
                Intent.CASUAL, 1.0, "Sticker media type", "skip",
            )

        # 4. Emoji-only text (no media) — casual, silent
        if stripped and not has_media and _EMOJI_ONLY_RE.match(stripped):
            return ClassificationResult(
                Intent.CASUAL, 0.9, "Emoji-only text", "skip",
            )

        # 5. Empty text with no media — nothing to process
        if not stripped and not has_media:
            return ClassificationResult(
                Intent.CASUAL, 0.8, "Empty message", "skip",
            )

        # Everything else → pass to Claude Code for judgment
        self._update_context(chat_jid, text, sender)
        return ClassificationResult(
            Intent.UNKNOWN, 0.0, "Requires Claude judgment", "respond",
        )

    def _update_context(self, chat_jid: str, text: str, sender: str) -> None:
        """Maintain a rolling buffer of last 5 messages per chat."""
        if not text:
            return
        buf = self._context_buffer.setdefault(chat_jid, [])
        buf.append(f"{sender}: {text[:100]}")
        if len(buf) > 5:
            self._context_buffer[chat_jid] = buf[-5:]
