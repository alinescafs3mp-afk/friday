"""A reasoning model narrates before it answers; the router has to keep only the answer.

The LAN endpoint (vLLM, model ``dispatcher``) emits its chain-of-thought inside
``message.content`` and leaves ``message.reasoning`` empty, so nothing separates the
monologue from the answer except a literal ``</think>`` — with no opening tag. These
fixtures are real responses captured from it, not invented ones, including the two
shapes that defeated the previous heuristic.

Getting this wrong is not cosmetic. Every answer, every Inbox suggestion and every
JSON-parsed enrichment reads ``content``.
"""

from __future__ import annotations

from friday.agent_runtime.llm import LLMRouter

# Captured verbatim; only the middle of the longer monologues is elided.
LEADS_WITH_MARKER = (
    "Here's a thinking process:\n\n"
    '1.  **Analyze User Input:**\n   - Question: "Ответь одним словом: столица Франции?"\n'
    "2.  **Formulate Output:**\n   - Париж\n\n"
    "   Output: Париж✅\n"
    "</think>\n\nПариж"
)
LEADS_WITH_PROSE = (
    "Here user asked for the capital of France in one word. The answer is straightforward: "
    'Paris. "Париж" is the correct Russian word.\n</think>\n\nПариж'
)
JSON_ANSWER = (
    "The user wants strict JSON. I should return only the object with no prose around it.\n"
    '</think>\n\n{"title":"тест","kind":"note"}'
)


def test_answer_is_taken_after_the_closing_tag() -> None:
    assert LLMRouter._strip_thinking(LEADS_WITH_MARKER) == "Париж"
    assert LLMRouter._strip_thinking(LEADS_WITH_PROSE) == "Париж"
    assert LLMRouter._strip_thinking(JSON_ANSWER) == '{"title":"тест","kind":"note"}'


def test_the_old_marker_heuristic_would_have_destroyed_both() -> None:
    """Why the rule changed, pinned so it cannot quietly regress.

    The previous implementation cut everything BEFORE a marker phrase. This model puts
    its reasoning AFTER it, so that returned the empty string — and the caller's
    fallback turned every answer into "Не удалось сформировать ответ.". When the model
    opened with prose instead (same prompt, same temperature 0), the marker was absent
    and the whole monologue was returned as the answer.
    """
    assert LEADS_WITH_MARKER.index("Here's a thinking process:") == 0
    assert LEADS_WITH_MARKER[:0] == ""  # what the old rule produced
    assert "thinking process" not in LEADS_WITH_PROSE.casefold()  # old rule: no cut at all
    # The new rule extracts the answer from both, and never leaks the tag.
    for sample in (LEADS_WITH_MARKER, LEADS_WITH_PROSE, JSON_ANSWER):
        cleaned = LLMRouter._strip_thinking(sample)
        assert cleaned and "</think>" not in cleaned and "thinking process" not in cleaned.casefold()


def test_a_response_truncated_mid_thought_yields_no_answer() -> None:
    """Reasoning consumes the output budget: at 2000 tokens this model still had not
    closed the tag on an entity-extraction prompt. There is no answer in there, and
    handing back the monologue would put the model's notes into knowledge."""
    truncated = "I need to extract entities. Let me enumerate them carefully. First, Анна is a"
    assert LLMRouter._strip_thinking(truncated, "length") == ""
    # The same text with a normal stop is a plain answer from a non-reasoning runtime.
    assert LLMRouter._strip_thinking(truncated, "stop") == truncated


def test_a_plain_answer_passes_through_untouched() -> None:
    assert LLMRouter._strip_thinking("Париж", "stop") == "Париж"
    assert LLMRouter._strip_thinking('{"a": 1}', "stop") == '{"a": 1}'


def test_marker_fallback_still_serves_runtimes_that_lead_with_it() -> None:
    """Kept for runtimes whose monologue really does precede the answer."""
    assert LLMRouter._strip_thinking("Париж\n\nLet me think about it more", "stop") == "Париж"
