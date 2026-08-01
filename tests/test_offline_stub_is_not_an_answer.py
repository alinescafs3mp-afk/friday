"""A stub printed because the model is unreachable is not a cited answer.

`_offline_response` listed the top hits as `- [K1] …`, and `[K#]` is the citation
vocabulary — the shared post-processing in `chat()` parses those markers as real
citations. So with the LLM enabled but down, the stub came back as a grounded,
cited answer: `answer_grounded` true, a «📎 Источники» legend of five labels, five
rows written to knowledge_usage with an answer counted — and the model had not
generated a word. With the LLM switched off, `knowledge_citations` was empty and
the very same text carrying [K1]..[K5] was captioned "no explicit references to
your records", contradicting itself on screen.

The titles still get listed. That is useful and honest. What is removed is the
claim that the model cited them.
"""

from __future__ import annotations

import re

from friday.agent_runtime import AgentContext, AgentRuntime


def _context(**overrides) -> AgentContext:
    base = {
        "kb_size": 12,
        "knowledge_hits": [
            {"id": "ko_1", "title": "Договор аренды", "summary": "Срок до декабря."},
            {"id": "ko_2", "title": "Смета", "summary": "Итог 240 тысяч."},
        ],
        "answer_mode": "personal_knowledge",
        "pending_inbox": 0,
    }
    base.update(overrides)
    context = AgentContext.__new__(AgentContext)
    for key, value in base.items():
        object.__setattr__(context, key, value)
    return context


def test_the_stub_carries_no_citation_markers():
    text = AgentRuntime._offline_response(_context())  # noqa: SLF001
    assert "LLM сейчас недоступна" in text
    assert not re.search(r"\[K\d+\]", text), f"the stub still speaks the citation vocabulary: {text}"


def test_the_stub_still_names_what_it_found():
    text = AgentRuntime._offline_response(_context())  # noqa: SLF001
    assert "Договор аренды" in text
    assert "Смета" in text


def test_an_empty_base_says_so_without_citing():
    text = AgentRuntime._offline_response(_context(kb_size=0, knowledge_hits=[]))  # noqa: SLF001
    assert not re.search(r"\[K\d+\]", text)


def test_no_confident_hit_says_so_without_citing():
    text = AgentRuntime._offline_response(_context(knowledge_hits=[]))  # noqa: SLF001
    assert not re.search(r"\[K\d+\]", text)
    assert "надёжного совпадения нет" in text
