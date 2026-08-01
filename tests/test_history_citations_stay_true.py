"""[K2] in an old answer is not [K2] in this one.

Labels are assigned per turn, by position in that turn's retrieval, so the same
marker names a different Knowledge Object from one message to the next. Earlier
answers stayed in the prompt verbatim while the legend beside them was rebuilt, so
the model read a claim the user had already been shown as though it came from an
unrelated note — and the deterministic `citation_overlap` guard did not catch it
(on a real mismatch it returned status ok with overlap 0.22). `_verify_response`
made it worse in its own way: it fetched evidence by the CITED ids, so it graded the
answer against the wrong note.

Each message stores the map that was true when it was written, so the rewrite is
label → knowledge id → whatever label that id holds now.
"""

from __future__ import annotations

import json

from friday.agent_runtime import _CITATION_OUT_OF_VIEW, _relabel_history_citations


def _message(content: str, citations: dict[str, str] | None = None) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "metadata_json": json.dumps({"knowledge_citations": citations or {}}),
    }


def test_a_marker_follows_its_record_to_the_new_number():
    history = _message("Срок аренды — до декабря [K1].", {"K1": "ko_lease"})
    current = {"ko_lease": "K3", "ko_other": "K1"}
    assert _relabel_history_citations(history["content"], history, current) == (
        "Срок аренды — до декабря [K3]."
    )


def test_a_record_absent_from_this_turn_loses_its_number_visibly():
    history = _message("Смета составила 240 тысяч [K2].", {"K2": "ko_estimate"})
    rewritten = _relabel_history_citations(history["content"], history, {"ko_lease": "K1"})
    assert "[K2]" not in rewritten
    assert _CITATION_OUT_OF_VIEW in rewritten


def test_several_markers_in_one_answer_are_each_remapped():
    history = _message(
        "По договору [K1] срок до декабря, по смете [K2] сумма 240 тысяч.",
        {"K1": "ko_lease", "K2": "ko_estimate"},
    )
    current = {"ko_estimate": "K1", "ko_lease": "K2"}
    rewritten = _relabel_history_citations(history["content"], history, current)
    assert "договору [K2]" in rewritten
    assert "смете [K1]" in rewritten


def test_a_message_with_no_stored_map_does_not_keep_a_stale_number():
    """Messages written before this existed carry no map; a wrong number is worse than none."""
    history = {"role": "assistant", "content": "Как в [K1].", "metadata_json": None}
    rewritten = _relabel_history_citations(history["content"], history, {"ko_lease": "K1"})
    assert "[K1]" not in rewritten
    assert _CITATION_OUT_OF_VIEW in rewritten


def test_text_without_markers_is_returned_untouched():
    history = _message("Договор подписан в марте.", {"K1": "ko_lease"})
    assert _relabel_history_citations(history["content"], history, {}) == history["content"]


def test_broken_metadata_does_not_break_the_prompt():
    history = {"role": "assistant", "content": "Как в [K1].", "metadata_json": "{not json"}
    rewritten = _relabel_history_citations(history["content"], history, {"ko_lease": "K1"})
    assert _CITATION_OUT_OF_VIEW in rewritten


def test_the_prompt_builder_actually_applies_it(settings, storage):
    """The helper being right is not the same as the prompt using it."""
    from friday.agent_runtime import AgentContext, AgentRuntime

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.settings = settings
    runtime.storage = storage

    context = AgentContext(conversation_id="conv_1", user_id="alice")
    context.conversation_history = [
        {"role": "user", "content": "Что по аренде?"},
        _message("Срок — до декабря [K1].", {"K1": "ko_lease"}),
    ]
    # This turn retrieved a different note first, so the lease note is no longer K1.
    # The builder rebuilds the map from these hits, which is exactly the swap.
    context.knowledge_hits = [
        {"id": "ko_other", "title": "Смета", "content": "Итог 240 тысяч."},
        {"id": "ko_lease", "title": "Договор аренды", "content": "Срок до декабря."},
    ]

    messages = runtime._build_initial_messages(context, "А по смете?", None, tool_enabled=False)  # noqa: SLF001
    assert context.knowledge_citations.get("K2") == "ko_lease", context.knowledge_citations
    assistant = [item for item in messages if item["role"] == "assistant"]
    assert assistant, "the history never reached the prompt"
    assert "[K2]" in assistant[0]["content"], (
        f"the old answer went into the prompt with a label pointing elsewhere: {assistant[0]['content']}"
    )
    assert "[K1]" not in assistant[0]["content"]
