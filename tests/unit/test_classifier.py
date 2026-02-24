"""Unit tests for household group intent classifier (local heuristics only)."""
from __future__ import annotations

import pytest

from adapters.wa_notifier.classifier import (
    ACTIONABLE_INTENTS,
    PASSIVE_INTENTS,
    SILENT_INTENTS,
    ClassificationResult,
    Intent,
    IntentClassifier,
)


# ---------------------------------------------------------------------------
# Intent enum coverage
# ---------------------------------------------------------------------------


class TestIntentEnum:
    def test_all_intents_categorized(self) -> None:
        """Every Intent (except UNKNOWN) must be in exactly one category."""
        all_categorized = ACTIONABLE_INTENTS | PASSIVE_INTENTS | SILENT_INTENTS
        uncategorized = {
            i for i in Intent if i not in all_categorized and i != Intent.UNKNOWN
        }
        assert uncategorized == set(), f"Uncategorized intents: {uncategorized}"

    def test_categories_are_disjoint(self) -> None:
        assert not (ACTIONABLE_INTENTS & PASSIVE_INTENTS)
        assert not (ACTIONABLE_INTENTS & SILENT_INTENTS)
        assert not (PASSIVE_INTENTS & SILENT_INTENTS)

    def test_intent_values_are_strings(self) -> None:
        for intent in Intent:
            assert isinstance(intent.value, str)


# ---------------------------------------------------------------------------
# ClassificationResult
# ---------------------------------------------------------------------------


class TestClassificationResult:
    def test_to_dict(self) -> None:
        r = ClassificationResult(
            Intent.RECEIPT, 0.95, "looks like a receipt", "respond",
        )
        d = r.to_dict()
        assert d["intent"] == "receipt"
        assert d["confidence"] == 0.95
        assert d["reasoning"] == "looks like a receipt"
        assert d["action"] == "respond"


# ---------------------------------------------------------------------------
# S3 short-circuit
# ---------------------------------------------------------------------------


class TestS3ShortCircuit:
    def test_s3_prefix_exact(self) -> None:
        c = IntentClassifier()
        result = c.classify("S3 hello", "Alice", False, None, "group@g.us")
        assert result.intent == Intent.S3_COMMAND
        assert result.confidence == 1.0

    def test_s3_prefix_lowercase(self) -> None:
        c = IntentClassifier()
        result = c.classify("s3 what time", "Bob", False, None, "group@g.us")
        assert result.intent == Intent.S3_COMMAND

    def test_s3_prefix_with_leading_whitespace(self) -> None:
        c = IntentClassifier()
        result = c.classify("  S3 test", "Alice", False, None, "group@g.us")
        assert result.intent == Intent.S3_COMMAND

    def test_s3_takes_priority_over_video(self) -> None:
        """S3 prefix check runs before video check."""
        c = IntentClassifier()
        result = c.classify(
            "S3 check video", "Alice", True, "video", "group@g.us",
        )
        assert result.intent == Intent.S3_COMMAND


# ---------------------------------------------------------------------------
# Video / sticker short-circuit
# ---------------------------------------------------------------------------


class TestMediaShortCircuit:
    def test_video_media_type(self) -> None:
        c = IntentClassifier()
        result = c.classify("", "Alice", True, "video", "group@g.us")
        assert result.intent == Intent.VIDEO
        assert result.confidence == 1.0
        assert result.action == "skip"

    def test_sticker_media_type(self) -> None:
        c = IntentClassifier()
        result = c.classify("", "Alice", True, "sticker", "group@g.us")
        assert result.intent == Intent.CASUAL
        assert result.confidence == 1.0
        assert result.action == "skip"


# ---------------------------------------------------------------------------
# Emoji / casual detection
# ---------------------------------------------------------------------------


class TestCasualDetection:
    def test_single_emoji(self) -> None:
        c = IntentClassifier()
        result = c.classify("\U0001f44d", "Alice", False, None, "group@g.us")
        assert result.intent == Intent.CASUAL
        assert result.action == "skip"

    def test_multiple_emojis(self) -> None:
        c = IntentClassifier()
        result = c.classify(
            "\U0001f602\U0001f602\U0001f602", "Bob", False, None, "group@g.us",
        )
        assert result.intent == Intent.CASUAL

    def test_emoji_with_spaces(self) -> None:
        c = IntentClassifier()
        result = c.classify(
            "\U0001f44d \u2764\ufe0f", "Alice", False, None, "group@g.us",
        )
        assert result.intent == Intent.CASUAL

    def test_text_with_emoji_is_not_casual(self) -> None:
        """Text mixed with emoji should NOT be classified as casual."""
        c = IntentClassifier()
        result = c.classify(
            "Thanks! \U0001f44d", "Alice", False, None, "group@g.us",
        )
        assert result.intent == Intent.UNKNOWN

    def test_empty_message_no_media(self) -> None:
        c = IntentClassifier()
        result = c.classify("", "Alice", False, None, "group@g.us")
        assert result.intent == Intent.CASUAL
        assert result.action == "skip"


# ---------------------------------------------------------------------------
# Unknown fallback (passed to Claude Code)
# ---------------------------------------------------------------------------


class TestUnknownFallback:
    def test_regular_text_is_unknown(self) -> None:
        c = IntentClassifier()
        result = c.classify("hello", "Alice", False, None, "group@g.us")
        assert result.intent == Intent.UNKNOWN
        assert result.action == "respond"

    def test_image_is_unknown(self) -> None:
        """Image messages need Claude to determine intent (receipt? food? school?)."""
        c = IntentClassifier()
        result = c.classify("", "HelperA", True, "image", "group@g.us")
        assert result.intent == Intent.UNKNOWN
        assert result.action == "respond"

    def test_audio_is_unknown(self) -> None:
        c = IntentClassifier()
        result = c.classify("", "Alice", True, "audio", "group@g.us")
        assert result.intent == Intent.UNKNOWN
        assert result.action == "respond"

    def test_text_with_media_is_unknown(self) -> None:
        c = IntentClassifier()
        result = c.classify(
            "check this out", "Alice", True, "image", "group@g.us",
        )
        assert result.intent == Intent.UNKNOWN
        assert result.action == "respond"


# ---------------------------------------------------------------------------
# Context buffer
# ---------------------------------------------------------------------------


class TestContextBuffer:
    def test_context_accumulates(self) -> None:
        c = IntentClassifier()
        chat = "group@g.us"
        c.classify("message 1", "Alice", False, None, chat)
        c.classify("message 2", "Bob", False, None, chat)
        assert len(c._context_buffer[chat]) == 2

    def test_context_caps_at_five(self) -> None:
        c = IntentClassifier()
        chat = "group@g.us"
        for i in range(7):
            c.classify(f"msg {i}", "Alice", False, None, chat)
        assert len(c._context_buffer[chat]) == 5
        # Oldest messages should have been dropped
        assert "msg 2" in c._context_buffer[chat][0]

    def test_empty_text_not_added_to_context(self) -> None:
        c = IntentClassifier()
        chat = "group@g.us"
        c.classify("", "Alice", True, "image", chat)
        assert chat not in c._context_buffer or len(c._context_buffer[chat]) == 0

    def test_s3_updates_context(self) -> None:
        """S3 short-circuit should still update context buffer."""
        c = IntentClassifier()
        chat = "group@g.us"
        c.classify("S3 hello", "Alice", False, None, chat)
        assert len(c._context_buffer[chat]) == 1

    def test_casual_emoji_does_not_update_context(self) -> None:
        """Emoji-only messages are dropped before context update."""
        c = IntentClassifier()
        chat = "group@g.us"
        c.classify("\U0001f44d", "Alice", False, None, chat)
        assert chat not in c._context_buffer or len(c._context_buffer[chat]) == 0
