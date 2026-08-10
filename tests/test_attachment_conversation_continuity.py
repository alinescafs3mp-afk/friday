"""A pending upload remains evidence in its own private conversation.

Inbox review controls promotion into reusable knowledge.  It must not erase a
file from the conversation in which its uploader is still asking about it, and
conversation continuity must not become a side door into another person's file
or ambient retrieval for unrelated questions.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _attachment_evidence_chunks,
)
from friday.permissions import ActorContext
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id


def _pending_file(
    storage,
    tenant_id: str,
    uploader: str,
    text: str,
    *,
    filename: str,
    extraction_success: bool = True,
) -> RawObject:
    storage.ensure_user(tenant_id)
    if uploader != tenant_id:
        storage.ensure_user(uploader)
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant_id,
        source="upload",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="file",
        metadata_json={
            "filename": filename,
            "uploaded_by": uploader,
            "extraction_success": extraction_success,
            "text_extraction_success": extraction_success,
        },
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=tenant_id,
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
            suggested_action="review",
        )
    )
    return raw


class _EnabledButUnusedLLM:
    enabled = True
    model = "attachment-test"

    async def chat(self, messages, **kwargs):  # pragma: no cover - patched paths should own the turn
        del messages, kwargs
        raise AssertionError("unexpected direct LLM call")


@pytest.mark.asyncio
async def test_pending_file_continues_only_when_the_same_conversation_points_back_to_it(
    settings,
    storage,
    monkeypatch,
):
    first_text = "ROW-01\nROW-02\nROW-03"
    second_text = "NEW-ROW-01\nNEW-ROW-02"
    first = _pending_file(storage, "alice", "alice", first_text, filename="first.txt")
    second = _pending_file(storage, "alice", "alice", second_text, filename="second.txt")
    unreadable = _pending_file(
        storage,
        "alice",
        "alice",
        "[File: unreadable]",
        filename="unreadable.bin",
        extraction_success=False,
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen: list[list[dict]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def generate(context, message, attachments):
        del context, message
        seen.append(list(attachments or []))
        return {"content": "Короткий ответ по материалу.", "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    opened = await runtime.chat(
        "alice",
        "разбери состав",
        actor=actor,
        attachments=[
            {
                "raw_object_id": first.id,
                "filename": "first.txt",
                "transient_text": first_text,
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )
    continued = await runtime.chat(
        "alice",
        "кто ещё там?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[],
        enable_tools=False,
    )
    unrelated = await runtime.chat(
        "alice",
        "как создать новый документ Word?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[],
        enable_tools=False,
    )
    replaced = await runtime.chat(
        "alice",
        "что в новом файле?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[
            {
                "raw_object_id": second.id,
                "filename": "second.txt",
                "transient_text": second_text,
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )
    continued_after_replacement = await runtime.chat(
        "alice",
        "кто ещё там?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[],
        enable_tools=False,
    )
    unreadable_turn = await runtime.chat(
        "alice",
        "что в файле?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[
            {
                "raw_object_id": unreadable.id,
                "filename": "unreadable.bin",
                "transient_text": "",
                "extraction_success": False,
                "extraction_error": "unavailable",
            }
        ],
        enable_tools=False,
    )
    transient_turn = await runtime.chat(
        "alice",
        "не сохраняй, только посмотри файл",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[
            {
                "filename": "one-turn.txt",
                "transient_text": "ONE-TURN-ONLY",
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )
    after_transient = await runtime.chat(
        "alice",
        "кто ещё там?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[],
        enable_tools=False,
    )

    assert storage.get_knowledge_by_raw(first.id, "alice") is None, "conversation evidence was promoted"
    assert continued["restored_attachment_count"] == 1
    assert continued["attachment_context_available"] is True
    assert any(first_text in str(item.get("transient_text") or "") for item in seen[1])
    assert unrelated["restored_attachment_count"] == 0
    assert unrelated["attachment_context_available"] is False
    assert seen[2] == [], "an independent question inherited the old file"
    assert replaced["restored_attachment_count"] == 0
    assert len(seen[3]) == 1 and seen[3][0]["raw_object_id"] == second.id
    assert first_text not in json.dumps(seen[3], ensure_ascii=False), (
        "a current file did not replace the old one"
    )
    assert continued_after_replacement["restored_attachment_count"] == 1
    assert len(seen[4]) == 1 and seen[4][0]["raw_object_id"] == second.id
    assert first_text not in json.dumps(seen[4], ensure_ascii=False), "the replaced file became active again"
    assert unreadable_turn["restored_attachment_count"] == 0
    assert unreadable_turn["attachment_context_available"] is False
    assert transient_turn["attachment_context_available"] is True
    assert after_transient["restored_attachment_count"] == 0
    assert after_transient["attachment_context_available"] is False
    assert seen[7] == [], "a no-save file allowed an older persisted file to return"

    first_user = storage.get_conversation_messages(opened["conversation_id"], user_id="alice", limit=20)[0]
    metadata = json.loads(first_user["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [first.id]
    assert first_text not in first_user["metadata_json"]
    assert "first.txt" not in first_user["metadata_json"]


def test_shared_tenant_attachment_requires_the_exact_uploader(settings, storage):
    raw = _pending_file(storage, "shared", "alice", "ALICE-ONLY-ROW", filename="private.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("bob")
    storage.store_message(
        conversation["id"],
        "bob",
        "user",
        "получен материал",
        metadata={"conversation_attachment_raw_ids": [raw.id]},
    )
    history = storage.get_conversation_messages(conversation["id"], user_id="bob")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "что ещё в файле?",
        history,
        tenant_id="shared",
        person_id="bob",
        allow_file_read=True,
    )

    assert restored == [], "a shared-archive colleague received another uploader's pending file"
    assert expected == 1, "the structural missing-file count must remain audible without exposing it"


def test_only_budgeted_history_can_restore_an_attachment(settings, storage):
    raw = _pending_file(storage, "alice", "alice", "OLD-ROW", filename="old.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "разбери состав",
        metadata={"conversation_attachment_raw_ids": [raw.id]},
    )
    storage.store_message(conversation["id"], "alice", "assistant", "x" * 9_500)
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "что ещё в файле?",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )
    assert restored == [] and expected == 0


def test_exact_replay_restores_a_caption_for_regenerate(settings, storage):
    raw = _pending_file(storage, "alice", "alice", "REPLAY-ROW", filename="replay.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    source = storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "сводка по составу",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "сводка по составу",
        history,
        tenant_id="alice",
        person_id="alice",
        replay_source_message_id=str(source["id"]),
        allow_file_read=True,
    )

    assert expected == 1
    assert len(restored) == 1 and restored[0]["raw_object_id"] == raw.id


class _Judge:
    enabled = True
    model = "attachment-judge"

    def __init__(self, answer: str):
        self.answer = answer
        self.messages = []

    async def chat(self, messages, **kwargs):
        del kwargs
        self.messages = messages
        return {"content": self.answer}


@pytest.mark.asyncio
async def test_attachment_chunks_reach_cardinality_verifier_and_repair(settings, storage):
    rows = "\n".join(f"POSITION-{number:02d}: PERSON-{number:02d}" for number in range(1, 17))
    chunks = _attachment_evidence_chunks(
        [{"filename": "positions.txt", "transient_text": rows, "extraction_success": True}]
    )
    assert 1 <= len(chunks) <= 6
    combined = "\n".join(chunk["output"] for chunk in chunks)
    assert "POSITION-01" in combined and "POSITION-16" in combined

    judge = _Judge('{"ok": true, "request_satisfied": true, "score": 1.0, "issues": []}')
    runtime = AgentRuntime(settings, storage, llm=judge)
    verdict = await runtime._verify_response(  # noqa: SLF001
        "перечисли все позиции и посчитай их",
        "В документе 16 позиций.",
        AgentContext(conversation_id="conv", user_id="alice"),
        tool_evidence=[*chunks, {"tool": "web_search", "output": "TOOL-EVIDENCE-SENTINEL"}],
    )
    assert verdict["status"] == "passed"
    judge_system = "\n".join(str(item.get("content") or "") for item in judge.messages)
    assert "количество позиций" in judge_system
    assert "POSITION-16" in judge_system
    assert "TOOL-EVIDENCE-SENTINEL" in judge_system, "attachment chunks displaced real tool evidence"

    repair = _Judge("Исправленный полный ответ, в котором перечислены все шестнадцать отдельных позиций.")
    runtime.llm = repair
    fixed = await runtime._repair_once(  # noqa: SLF001
        "перечисли все позиции",
        "В документе десять позиций, перечислены не все.",
        AgentContext(conversation_id="conv", user_id="alice"),
        {"status": "failed", "issues": ["пропущены позиции"]},
        tool_evidence=[*chunks, {"tool": "web_search", "output": "TOOL-EVIDENCE-SENTINEL"}],
    )
    repair_prompt = "\n".join(str(item.get("content") or "") for item in repair.messages)
    assert fixed.startswith("Исправленный")
    assert "POSITION-16" in repair_prompt
    assert "число позиций" in repair_prompt
    assert "TOOL-EVIDENCE-SENTINEL" in repair_prompt


@pytest.mark.asyncio
async def test_short_attachment_answer_is_verified_without_persisting_file_text(
    settings,
    storage,
    monkeypatch,
):
    private_text = "PRIVATE-ROW-SENTINEL-16"
    raw = _pending_file(storage, "alice", "alice", private_text, filename="private.txt")
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=10_000),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    captured: dict[str, object] = {}

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def generate(context, message, attachments):
        del context, message, attachments
        return {"content": "Их 16.", "tools_used": []}

    async def verify(query, response, context, *, tool_evidence=None):
        del query, response, context
        captured["evidence"] = list(tool_evidence or [])
        return {
            "status": "passed",
            "ok": True,
            "score": 1.0,
            "issues": [f"quoted {private_text}"],
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    result = await runtime.chat(
        "alice",
        "сколько позиций в файле?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[
            {
                "raw_object_id": raw.id,
                "filename": "private.txt",
                "transient_text": private_text,
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )

    assert result["verification_status"] == "passed", "the minimum-length gate hid file evidence"
    evidence = json.dumps(captured.get("evidence"), ensure_ascii=False)
    assert private_text in evidence
    messages = storage.get_conversation_messages(result["conversation_id"], user_id="alice")
    for message in messages:
        assert private_text not in str(message.get("metadata_json") or "")
    assistant_metadata = json.loads(messages[-1]["metadata_json"])
    assert assistant_metadata["verification"]["issues"] == ["attachment_verification_note"]
    assert private_text not in json.dumps(result, ensure_ascii=False)
