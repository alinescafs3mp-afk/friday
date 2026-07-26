"""The promotion decision: is this text worth keeping, and as what?

Heuristic-only and deterministic — no model call — because it runs on every
captured message. It decides *promote / review / transient*; a human still
confirms anything it routes to the Inbox.
"""

from __future__ import annotations

from jericho.ingestion._base import (
    _ACTION_REQUEST_START,
    _CODE_RE,
    _DATE_RE,
    _DECLARATIVE_FACT,
    _DURABLE_NOUN,
    _EMAIL_RE,
    _EXPLICIT_DONT_SAVE,
    _EXPLICIT_KNOWLEDGE_LABEL,
    _EXPLICIT_SAVE,
    _GREETING,
    _LIST_RE,
    _LOW_VALUE_PREFERENCE,
    _NAMED_SUBJECT_CONTEXT,
    _NAMED_TOKEN_RE,
    _ONLY_SYMBOLS_RE,
    _PERSONAL_FACT,
    _PREFERENCE_FACT,
    _QUESTION_START,
    _ROLE_FACT,
    _SPECIFIC_VALUE_RE,
    _URL_RE,
    PromotionAction,
    PromotionAssessment,
    _clamp,
    _detect_knowledge_kind,
    _estimate_content_quality,
    re,
)


class ContentClassifier:
    """High-precision, moderate classifier for long-term knowledge promotion."""

    def classify(self, content: str) -> tuple[str, float, str]:
        assessment = self.assess(content)
        return assessment.category, assessment.confidence, assessment.reason

    def assess(self, content: str, *, force_knowledge: bool = False) -> PromotionAssessment:
        raw_text = (content or "").strip()
        text = " ".join(raw_text.split())
        if not text:
            return PromotionAssessment(
                category="unknown",
                confidence=1.0,
                action="transient",
                promotion_score=0.0,
                quality_score=0.0,
                knowledge_kind="note",
                reason="empty content",
                penalties=["empty"],
            )

        explicit_no_save = bool(_EXPLICIT_DONT_SAVE.search(text))
        if explicit_no_save:
            # A direct privacy/control instruction always wins over heuristics and
            # over a stale or accidentally supplied force flag. The user may
            # still promote the material later through an explicit Admin action.
            return PromotionAssessment(
                category="private_transient",
                confidence=1.0,
                action="transient",
                promotion_score=0.0,
                quality_score=0.0,
                knowledge_kind="note",
                reason="explicit instruction not to store as knowledge",
                penalties=["explicit_no_save"],
            )

        if force_knowledge:
            kind = _detect_knowledge_kind(raw_text)
            quality = _estimate_content_quality(raw_text, signals=["manual_promotion"])
            return PromotionAssessment(
                category="knowledge",
                confidence=1.0,
                action="promote",
                promotion_score=max(0.9, quality),
                quality_score=quality,
                knowledge_kind=kind,
                reason="manual promotion",
                signals=["manual_promotion"],
            )

        explicit_save = bool(_EXPLICIT_SAVE.search(text))
        slash_command = bool(text.startswith("/") and re.match(r"^/[A-Za-z0-9_]+", text))
        greeting = len(text) <= 80 and bool(_GREETING.fullmatch(text))
        only_symbols = len(text) <= 24 and bool(_ONLY_SYMBOLS_RE.fullmatch(text))
        question_score = 0.0
        if _QUESTION_START.search(text):
            question_score += 0.6
        if text.endswith("?"):
            question_score += 0.62
        if text.count("?") >= 2:
            question_score += 0.08
        action_request = bool(_ACTION_REQUEST_START.search(text))

        signals: list[str] = []
        penalties: list[str] = []
        promotion = 0.08

        def add_signal(name: str, weight: float) -> None:
            nonlocal promotion
            signals.append(name)
            promotion += weight

        if explicit_save:
            add_signal("explicit_save", 0.72)
        if _EXPLICIT_KNOWLEDGE_LABEL.search(raw_text):
            add_signal("explicit_label", 0.34)
        if _DURABLE_NOUN.search(text):
            add_signal("durable_subject", 0.18)
        if _DECLARATIVE_FACT.search(text):
            add_signal("declarative_fact", 0.20)
        if _PERSONAL_FACT.search(text):
            add_signal("personal_fact", 0.13)
        if _PREFERENCE_FACT.search(text):
            add_signal("personal_preference", 0.30)
        if _ROLE_FACT.search(text):
            add_signal("role_fact", 0.20)
        if _DATE_RE.search(text):
            add_signal("date_or_time", 0.12)
        if _URL_RE.search(text) or _EMAIL_RE.search(text):
            add_signal("reference", 0.10)
        if _SPECIFIC_VALUE_RE.search(text):
            add_signal("specific_value", 0.13)
        if _LIST_RE.search(raw_text):
            add_signal("structured_list", 0.13)
        if _CODE_RE.search(raw_text):
            add_signal("technical_artifact", 0.18)
        if _NAMED_TOKEN_RE.search(text) or _NAMED_SUBJECT_CONTEXT.search(text):
            add_signal("named_subject", 0.09)
        if len(text) >= 80:
            add_signal("substantive_length", 0.09)
        if len(text) >= 280:
            add_signal("rich_context", 0.09)
        if text.count(":") >= 1 and len(text) >= 45:
            add_signal("structured_statement", 0.06)

        if greeting:
            penalties.append("greeting_or_acknowledgement")
            promotion -= 0.95
        if only_symbols:
            penalties.append("symbols_only")
            promotion -= 0.9
        if slash_command:
            penalties.append("telegram_command")
            promotion -= 0.9
        if action_request and not explicit_save:
            penalties.append("pure_action_request")
            promotion -= 0.58
        if question_score >= 0.55 and not explicit_save:
            penalties.append("question_syntax")
            promotion -= min(0.68, question_score * 0.75)
        if len(text) < 20 and not explicit_save:
            penalties.append("very_short")
            promotion -= 0.15
        if len(text.split()) < 4 and not explicit_save:
            penalties.append("low_context")
            promotion -= 0.08

        promotion = _clamp(promotion)
        quality = _estimate_content_quality(raw_text, signals=signals, penalties=penalties)
        kind = _detect_knowledge_kind(raw_text)
        durable_signal_count = len(
            {
                "explicit_label",
                "durable_subject",
                "declarative_fact",
                "personal_fact",
                "personal_preference",
                "role_fact",
                "date_or_time",
                "reference",
                "structured_list",
                "technical_artifact",
                "named_subject",
                "specific_value",
            }
            & set(signals)
        )

        if slash_command:
            return PromotionAssessment(
                "command",
                0.99,
                "transient",
                promotion,
                quality,
                kind,
                "telegram command",
                signals,
                penalties,
            )
        if greeting or only_symbols:
            return PromotionAssessment(
                "greeting",
                0.98,
                "transient",
                promotion,
                quality,
                kind,
                "greeting or acknowledgement",
                signals,
                penalties,
            )
        if _LOW_VALUE_PREFERENCE.fullmatch(text):
            return PromotionAssessment(
                "chatter",
                0.94,
                "transient",
                min(promotion, 0.12),
                min(quality, 0.18),
                kind,
                "interpersonal or context-free preference chatter",
                signals,
                [*penalties, "low_value_preference"],
            )

        # Questions and action requests are conversation by default.  A long question containing
        # durable facts may be worth review, but it is never auto-promoted without save intent.
        if (question_score >= 0.55 or action_request) and not explicit_save:
            if durable_signal_count >= 3 and quality >= 0.48:
                return PromotionAssessment(
                    "question" if question_score >= 0.55 else "command",
                    min(0.98, max(question_score, 0.72)),
                    "review",
                    max(promotion, 0.38),
                    quality,
                    kind,
                    "request contains potentially durable context",
                    signals,
                    penalties,
                )
            return PromotionAssessment(
                "question" if question_score >= 0.55 else "command",
                min(0.98, max(question_score, 0.78)),
                "transient",
                promotion,
                quality,
                kind,
                "pure question or action request",
                signals,
                penalties,
            )

        strong_preference = (
            bool(_PREFERENCE_FACT.search(text))
            and not _LOW_VALUE_PREFERENCE.fullmatch(text)
            and len(text.split()) >= 3
        )
        strong_decision = (
            kind == "decision"
            and "declarative_fact" in signals
            and ("personal_fact" in signals or "named_subject" in signals)
            and "durable_subject" in signals
        )
        strong_named_fact = (
            kind == "fact"
            and ("declarative_fact" in signals or "role_fact" in signals)
            and "named_subject" in signals
            and "durable_subject" in signals
        )
        strong_timed_record = (
            kind in {"task", "event"}
            and "date_or_time" in signals
            and "durable_subject" in signals
            and ("named_subject" in signals or "declarative_fact" in signals)
        )
        strong_reference = (
            "reference" in signals and "durable_subject" in signals and "named_subject" in signals
        )

        if explicit_save:
            action: PromotionAction = "promote"
            category = "knowledge"
            confidence = 0.98
            reason = "explicit save intent"
        elif strong_preference and quality >= 0.34:
            action = "promote"
            category = "knowledge"
            promotion = max(promotion, 0.72)
            confidence = 0.91
            reason = "explicit durable personal preference"
        elif strong_decision and quality >= 0.36:
            action = "promote"
            category = "knowledge"
            promotion = max(promotion, 0.74)
            confidence = 0.92
            reason = "explicit durable decision"
        elif strong_named_fact and quality >= 0.36:
            action = "promote"
            category = "knowledge"
            promotion = max(promotion, 0.70)
            confidence = 0.90
            reason = "specific named factual relationship"
        elif strong_timed_record and quality >= 0.42:
            action = "promote"
            category = "knowledge"
            promotion = max(promotion, 0.70)
            confidence = 0.90
            reason = "specific dated task or event"
        elif strong_reference and quality >= 0.36:
            action = "promote"
            category = "knowledge"
            promotion = max(promotion, 0.70)
            confidence = 0.88
            reason = "specific durable reference"
        elif promotion >= 0.68 and quality >= 0.48 and durable_signal_count >= 2:
            action = "promote"
            category = "knowledge"
            confidence = min(0.96, 0.62 + promotion * 0.35)
            reason = "specific durable information"
        elif promotion >= 0.34 or durable_signal_count >= 1:
            action = "review"
            category = "knowledge" if promotion >= 0.5 else "unknown"
            confidence = min(0.86, 0.45 + abs(promotion - 0.5))
            reason = "potentially useful but uncertain"
        else:
            action = "transient"
            category = "unknown"
            confidence = min(0.9, 0.58 + (0.34 - promotion))
            reason = "insufficient durable value"

        return PromotionAssessment(
            category=category,
            confidence=_clamp(confidence),
            action=action,
            promotion_score=promotion,
            quality_score=quality,
            knowledge_kind=kind,
            reason=reason,
            signals=signals,
            penalties=penalties,
        )


_classifier = ContentClassifier()
