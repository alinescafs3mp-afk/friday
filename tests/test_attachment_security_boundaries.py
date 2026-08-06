"""Security and completeness boundaries for conversational file evidence.

All payloads in this module are synthetic.  The tests pin the distinction
between a file that may be shown in one private conversation and durable,
globally reusable knowledge: an opaque pointer never substitutes for the
current ``files.read`` decision, incomplete/advisory projections never become
verified evidence, and a private file turn never reaches an outbound tool.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import friday.agent_runtime as agent_runtime_module
from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _attachment_evidence_chunks,
    _bounded_attachment_projection,
    _requires_complete_attachment_evidence,
)
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.permissions import AuthorizationService
from friday.server import _current_turn_file_attachment
from friday.storage.models import RawObject, new_id


def _stored_file(
    storage,
    tenant_id: str,
    text: str,
    *,
    filename: str,
    uploader: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RawObject:
    storage.ensure_user(tenant_id)
    owner = uploader or tenant_id
    if owner != tenant_id:
        storage.ensure_user(owner)
    raw_metadata = {
        "filename": filename,
        "uploaded_by": owner,
        "extraction_success": True,
        "text_extraction_success": True,
        **(metadata or {}),
    }
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant_id,
        source="synthetic-upload",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="file",
        metadata_json=raw_metadata,
    )
    storage.store_raw_object(raw)
    return raw


class _UnusedEnabledLLM:
    enabled = True
    model = "attachment-security-unused"
    total_budget_sec = 30.0

    async def chat(self, messages, **kwargs):  # pragma: no cover - patched roads own the turn
        del messages, kwargs
        raise AssertionError("unexpected direct model call")


def _patch_simple_turn(monkeypatch, runtime: AgentRuntime, seen: list[list[dict[str, Any]]]) -> None:
    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
        )

    async def generate(context, message, attachments):
        del context, message
        seen.append([dict(item) for item in (attachments or [])])
        return {"content": "Синтетический ответ по доступному материалу.", "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)


@pytest.mark.asyncio
async def test_revoked_files_read_hides_same_turn_and_restored_raw_text(
    settings,
    storage,
    monkeypatch,
):
    private_text = "REVOKED-PRIVATE-RAW-TEXT"
    storage.ensure_user("alice", preset_key="owner")
    raw = _stored_file(storage, "alice", private_text, filename="revoked.txt")
    auth = AuthorizationService(storage)
    actor = auth.actor_for_user("alice", source="test")
    auth.deny_permission("alice", "files.read")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(auth, settings),
    )
    seen: list[list[dict[str, Any]]] = []
    _patch_simple_turn(monkeypatch, runtime, seen)

    same_turn = await runtime.chat(
        "alice",
        "что в этом файле?",
        actor=actor,
        attachments=[
            {
                "raw_object_id": raw.id,
                "filename": "revoked.txt",
                "transient_text": private_text,
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )

    conversation = storage.create_conversation("alice", title="revoked restore")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "разбери файл",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "первый ответ",
        metadata={"attachment_context_used": True},
    )
    restored = await runtime.chat(
        "alice",
        "что ещё в файле?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert seen == [[], []]
    assert same_turn["attachment_context_available"] is False
    assert restored["attachment_context_available"] is False
    assert restored["restored_attachment_count"] == 0
    same_turn_rows = storage.get_conversation_messages(same_turn["conversation_id"], user_id="alice")
    user_metadata = json.loads(same_turn_rows[0]["metadata_json"])
    assert user_metadata["had_attachments"] is True
    assert user_metadata["private_context_lineage"] is True
    assert "conversation_attachment_raw_ids" not in user_metadata
    assert private_text not in json.dumps([same_turn, restored], ensure_ascii=False)


@pytest.mark.asyncio
async def test_unreadable_attachment_cannot_support_a_complete_count_claim(
    settings,
    storage,
    monkeypatch,
):
    private_text = "UNREADABLE-COMPLETE-COUNT-SENTINEL"
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    actor = auth.actor_for_user("alice", source="test")
    auth.deny_permission("alice", "files.read")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(auth, settings),
    )
    seen: list[list[dict[str, Any]]] = []
    _patch_simple_turn(monkeypatch, runtime, seen)

    result = await runtime.chat(
        "alice",
        "сколько всего позиций в этом файле? перечисли все",
        actor=actor,
        attachments=[
            {
                "filename": "unreadable.txt",
                "transient_text": private_text,
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )

    assert seen == [[]]
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 0
    assert result["attachment_context_available"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False
    assert result["verification"]["issues"] == ["attachment_verification_unavailable"]
    assert private_text not in json.dumps(result, ensure_ascii=False)


def test_replay_is_bound_to_one_exact_source_message_id_not_caption_equality(settings, storage):
    first = _stored_file(storage, "alice", "FIRST-RAW-TEXT", filename="first.txt")
    second = _stored_file(storage, "alice", "SECOND-RAW-TEXT", filename="second.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    first_source = storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "сделай сводку",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "conversation_attachment_raw_ids": [first.id],
        },
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "первая сводка",
        metadata={"attachment_context_used": True},
    )
    second_source = storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "сделай сводку",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "conversation_attachment_raw_ids": [second.id],
        },
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "вторая сводка",
        metadata={"attachment_context_used": True},
    )
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    ordinary, ordinary_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "сделай сводку",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )
    replay_first, first_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "сделай сводку",
        history,
        tenant_id="alice",
        person_id="alice",
        replay_source_message_id=str(first_source["id"]),
        allow_file_read=True,
    )
    replay_second, second_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "сделай сводку",
        history,
        tenant_id="alice",
        person_id="alice",
        replay_source_message_id=str(second_source["id"]),
        allow_file_read=True,
    )
    mismatched, mismatched_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "другой текст",
        history,
        tenant_id="alice",
        person_id="alice",
        replay_source_message_id=str(first_source["id"]),
        allow_file_read=True,
    )

    assert ordinary == [] and ordinary_expected == 0
    assert first_expected == second_expected == 1
    assert [item["raw_object_id"] for item in replay_first] == [first.id]
    assert [item["raw_object_id"] for item in replay_second] == [second.id]
    assert mismatched == [] and mismatched_expected == 0


@pytest.mark.parametrize("message", ["как там дела?", "создай документ Word"])
def test_broad_language_after_a_file_does_not_restore_it(settings, storage, message):
    raw = _stored_file(storage, "alice", "STALE-FILE-TEXT", filename="stale.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "посмотри файл",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "файл прочитан",
        metadata={"attachment_context_used": True},
    )
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        message,
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert restored == [] and expected == 0


@pytest.mark.parametrize(
    "message",
    [
        "Сколько их всего?",
        "А их сколько?",
        "Перечисли их.",
        "И это всё?",
        "Проверь ещё раз.",
        "Посчитай заново.",
        "Почему ты нашла только 10?",
    ],
)
def test_immediate_file_followups_restore_the_exact_private_source(
    settings,
    storage,
    message,
):
    raw = _stored_file(storage, "alice", "SYNTHETIC-COMPLETE-SOURCE", filename="source.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "посмотри файл",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "синтетический частичный ответ",
        metadata={"attachment_context_used": True},
    )
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        message,
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert expected == 1
    assert [item.get("raw_object_id") for item in restored] == [raw.id]
    assert restored[0]["transient_text"] == "SYNTHETIC-COMPLETE-SOURCE"


def test_repeated_regenerate_keeps_legacy_warning_and_persisted_pointer(settings):
    from friday.server import create_app

    with TestClient(create_app(replace(settings, verify_answers=False))) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        me = client.get("/api/me", headers=headers)
        assert me.status_code == 200, me.text
        user_id = str(me.json()["actor"]["user_id"])
        storage = client.app.state.storage

        legacy = storage.create_conversation(user_id, title="legacy attachment")
        storage.store_message(
            legacy["id"],
            user_id,
            "user",
            "что было в старом вложении?",
            metadata={"had_attachments": True, "attachment_count": 1},
        )
        storage.store_message(legacy["id"], user_id, "assistant", "первый ответ")
        legacy_responses = [
            client.post(
                "/api/me/regenerate",
                json={"conversation_id": legacy["id"]},
                headers=headers,
            )
            for _ in range(2)
        ]
        assert all(response.status_code == 200 for response in legacy_responses)
        assert all(
            "вложен" in str(response.json().get("regenerate_notice") or "").casefold()
            for response in legacy_responses
        )
        legacy_users = [
            row
            for row in storage.get_conversation_messages(legacy["id"], user_id=user_id)
            if row["role"] == "user"
        ]
        assert len(legacy_users) == 3
        for row in legacy_users:
            metadata = json.loads(row["metadata_json"] or "{}")
            assert metadata.get("had_attachments") is True
            assert "conversation_attachment_raw_ids" not in metadata

        raw = _stored_file(storage, user_id, "PERSISTED-REPLAY-TEXT", filename="kept.txt")
        persisted = storage.create_conversation(user_id, title="persisted attachment")
        storage.store_message(
            persisted["id"],
            user_id,
            "user",
            "что было в сохранённом вложении?",
            metadata={
                "had_attachments": True,
                "attachment_count": 1,
                "conversation_attachment_raw_ids": [raw.id],
            },
        )
        storage.store_message(
            persisted["id"],
            user_id,
            "assistant",
            "первый ответ",
            metadata={"attachment_context_used": True},
        )
        persisted_responses = [
            client.post(
                "/api/me/regenerate",
                json={"conversation_id": persisted["id"]},
                headers=headers,
            )
            for _ in range(2)
        ]
        assert all(response.status_code == 200 for response in persisted_responses)
        assert all(
            response.json()["attachment_context_available"] is True for response in persisted_responses
        )
        assert all(not response.json().get("regenerate_notice") for response in persisted_responses)
        persisted_users = [
            row
            for row in storage.get_conversation_messages(persisted["id"], user_id=user_id)
            if row["role"] == "user"
        ]
        assert len(persisted_users) == 3
        for row in persisted_users:
            metadata = json.loads(row["metadata_json"] or "{}")
            assert metadata["conversation_attachment_raw_ids"] == [raw.id]
            assert "PERSISTED-REPLAY-TEXT" not in row["metadata_json"]


@pytest.mark.asyncio
async def test_partial_two_file_restore_is_not_reported_as_available(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice", preset_key="owner")
    readable = _stored_file(storage, "alice", "ONLY-READABLE-FILE", filename="one.txt")
    missing_id = "raw_missing_synthetic_sibling"
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "сравни два файла",
        metadata={
            "had_attachments": True,
            "attachment_count": 2,
            "conversation_attachment_raw_ids": [readable.id, missing_id],
        },
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "частичный ответ",
        metadata={"attachment_context_used": True},
    )
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(auth, settings),
    )
    seen: list[list[dict[str, Any]]] = []
    _patch_simple_turn(monkeypatch, runtime, seen)

    result = await runtime.chat(
        "alice",
        "что ещё в этих файлах?",
        actor=auth.actor_for_user("alice", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert len(seen) == 1 and len(seen[0]) == 1
    assert seen[0][0]["raw_object_id"] == readable.id
    assert result["restored_attachment_count"] == 1
    assert result["attachment_context_expected_count"] == 2
    assert result["attachment_context_readable_count"] == 1
    assert result["attachment_context_available"] is False


def _fixed_text(prefix: str, suffix: str, size: int, fill: str) -> str:
    assert len(prefix) + len(suffix) <= size
    return prefix + fill * (size - len(prefix) - len(suffix)) + suffix


def test_synthesis_and_verifier_share_the_same_sequential_24k_projection(settings, storage):
    first = _fixed_text("FIRST-BEGIN|", "|FIRST-END", 12_000, "A")
    second_prefix = _fixed_text("SECOND-BEGIN|", "|SECOND-CUT-IN", 12_000, "B")
    second = second_prefix + "|SECOND-OUTSIDE-BUDGET"
    attachments = [
        {"filename": "first.txt", "transient_text": first, "extraction_success": True},
        {"filename": "second.txt", "transient_text": second, "extraction_success": True},
        {
            "filename": "third.txt",
            "transient_text": "THIRD-OUTSIDE-BUDGET",
            "extraction_success": True,
        },
    ]
    projected = _bounded_attachment_projection(attachments)
    expected = "".join(str(item.get("transient_text") or "") for item in projected)
    assert len(expected) == 24_000

    runtime = AgentRuntime(settings, storage)
    messages = runtime._build_initial_messages(  # noqa: SLF001
        AgentContext(conversation_id="conv", user_id="alice"),
        "прочитай файлы",
        attachments,
        tool_enabled=False,
    )
    synthesis_payload = next(
        str(item.get("content") or "")
        for item in messages
        if item.get("role") == "user" and "<attachment filename=" in str(item.get("content") or "")
    )
    synthesis_bodies = re.findall(
        r"<attachment[^>]*>\n(.*?)\n</attachment>", synthesis_payload, flags=re.DOTALL
    )
    # A third file whose share is zero remains visible as an honest structural
    # caveat.  It is not source text and therefore is deliberately absent from
    # verifier evidence; compare only the projected file bodies themselves.
    synthesis_text = "".join(body for body in synthesis_bodies if body != "(содержимое недоступно)")
    evidence = _attachment_evidence_chunks(attachments)
    evidence_text = "".join(str(chunk["output"]).split("\n", 1)[1] for chunk in evidence)

    assert synthesis_text == expected
    assert evidence_text == expected
    assert "FIRST-END" in synthesis_text and "SECOND-CUT-IN" in synthesis_text
    assert "SECOND-OUTSIDE-BUDGET" not in synthesis_text
    assert "THIRD-OUTSIDE-BUDGET" not in synthesis_text
    assert attachments[1]["transient_text"] == second, "projection mutated caller-owned input"


def test_tiny_leading_files_do_not_consume_the_verifier_tail_budget():
    tail = "|TAIL-VERIFIER-MUST-SEE"
    third = ("C" * (23_992 - len(tail))) + tail
    attachments = [
        {"filename": "one.txt", "transient_text": "A", "verification_eligible": True},
        {"filename": "two.txt", "transient_text": "B", "verification_eligible": True},
        {"filename": "three.txt", "transient_text": third, "verification_eligible": True},
    ]
    projected_text = "".join(
        str(item.get("transient_text") or "") for item in _bounded_attachment_projection(attachments)
    )
    evidence = _attachment_evidence_chunks(attachments)
    evidence_text = "".join(str(chunk["output"]).split("\n", 1)[1] for chunk in evidence)

    assert len(projected_text) == 23_994
    assert len(evidence) == 8
    assert evidence_text == projected_text
    assert tail in evidence_text


@pytest.mark.parametrize(
    "answer",
    [
        "Только Иван, Пётр и Анна.",
        "В документе 16 отдельных позиций.",
        "В документе три отдельные позиции.",
        "Их 16.",
        "Я насчитала 16.",
        "Никого другого нет.",
        "Других нет.",
        "Иван, Пётр и Анна — и всё.",
        "На этом всё.",
        "Итого три.",
        "Ровно 16.",
        "Трое.",
    ],
)
def test_answer_only_exhaustiveness_language_requires_complete_attachment(answer):
    assert _requires_complete_attachment_evidence("Кто указан в документе?", answer)


@pytest.mark.parametrize(
    "answer",
    [
        "Позиция 3 — Иван.",
        "На странице 3 указан Иван.",
        "Показана часть списка.",
    ],
)
def test_ordinals_and_explicit_partiality_are_not_complete_attachment_claims(answer):
    assert not _requires_complete_attachment_evidence("Кто указан в документе?", answer)


@pytest.mark.parametrize(
    "coverage_flag",
    [
        {"text_truncated": True},
        {"parse_pages_truncated": True, "parse_pages_read": 1, "parse_total_pages": 3},
        {"parse_deadline_reached": True},
    ],
    ids=["text-budget", "page-budget", "deadline"],
)
@pytest.mark.asyncio
async def test_incomplete_attachment_cannot_turn_an_all_or_count_answer_verified(
    settings,
    storage,
    monkeypatch,
    coverage_flag,
):
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(auth, settings),
    )

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)

    async def generate(context, message, attachments):
        del context, message, attachments
        return {"content": "Всего три позиции, перечислены все.", "tools_used": []}

    async def optimistic_verifier(query, response, context, *, tool_evidence=None):
        del query, response, context, tool_evidence
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", optimistic_verifier)
    result = await runtime.chat(
        "alice",
        "сколько всего позиций в файле? перечисли все",
        actor=auth.actor_for_user("alice", source="test"),
        attachments=[
            {
                "filename": "incomplete.txt",
                "transient_text": "POSITION-1\nPOSITION-2\nPOSITION-3",
                "extraction_success": True,
                "verification_eligible": True,
                **coverage_flag,
            }
        ],
        enable_tools=False,
    )

    assert result["attachment_context_available"] is True
    assert result["attachment_coverage_complete"] is False
    assert result["attachment_verification_complete"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False


@pytest.mark.parametrize(
    "model_answer",
    [
        "В документе 3 позиции.",
        "Указаны только Иван, Пётр и Анна.",
        "Больше никого в документе нет.",
        "Это полный состав документа.",
    ],
)
@pytest.mark.asyncio
async def test_incomplete_attachment_rejects_answer_only_exhaustiveness_claims(
    model_answer,
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(auth, settings),
    )

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)

    async def generate(context, message, attachments):
        del context, message, attachments
        return {"content": model_answer, "tools_used": []}

    async def optimistic_verifier(query, response, context, *, tool_evidence=None):
        del query, response, context, tool_evidence
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", optimistic_verifier)
    result = await runtime.chat(
        "alice",
        "Кто указан в документе?",
        actor=auth.actor_for_user("alice", source="test"),
        attachments=[
            {
                "filename": "incomplete.txt",
                "transient_text": "Иван\nПётр\nАнна",
                "extraction_success": True,
                "verification_eligible": True,
                "text_truncated": True,
            }
        ],
        enable_tools=False,
    )

    assert result["attachment_verification_complete"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_repair_cannot_introduce_a_verified_count_from_incomplete_attachment(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(auth, settings),
    )
    verification_calls = 0

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)

    async def generate(context, message, attachments):
        del context, message, attachments
        return {"content": "В видимом фрагменте названы несколько позиций.", "tools_used": []}

    async def verifier(query, response, context, *, tool_evidence=None):
        nonlocal verification_calls
        del query, response, context, tool_evidence
        verification_calls += 1
        if verification_calls == 1:
            return {"status": "failed", "ok": False, "score": 0.0, "issues": ["synthetic"]}
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    async def repair(*args, **kwargs):
        del args, kwargs
        return "В документе 3 позиции."

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verifier)
    monkeypatch.setattr(runtime, "_repair_once", repair)
    result = await runtime.chat(
        "alice",
        "Кто указан в документе?",
        actor=auth.actor_for_user("alice", source="test"),
        attachments=[
            {
                "filename": "incomplete.txt",
                "transient_text": "Иван\nПётр\nАнна",
                "extraction_success": True,
                "verification_eligible": True,
                "text_truncated": True,
            }
        ],
        enable_tools=False,
    )

    assert verification_calls == 2
    assert result["message"] == "В документе 3 позиции."
    assert result["attachment_verification_complete"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False


@pytest.mark.parametrize("kind", ["vision", "voice"])
@pytest.mark.asyncio
async def test_advisory_vision_and_voice_are_not_verifier_or_grounding_evidence(
    settings,
    storage,
    monkeypatch,
    kind,
):
    advisory_text = f"ADVISORY-{kind.upper()}-TEXT"
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(auth, settings),
    )
    shown: list[list[dict[str, Any]]] = []
    grounding: dict[str, Any] = {}

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            answer_mode="personal_knowledge_missing",
            outward_verdict=("архив", None),
        )

    async def generate(context, message, attachments):
        del context, message
        shown.append([dict(item) for item in (attachments or [])])
        return {"content": "В распознанном материале есть одна строка.", "tools_used": []}

    async def should_not_verify(*args, **kwargs):
        del args, kwargs
        raise AssertionError("advisory OCR/transcript reached the verifier")

    def capture_grounding(*args, **kwargs):
        del args
        grounding.update(kwargs)
        return ""

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", should_not_verify)
    monkeypatch.setattr(agent_runtime_module, "_grounding_warning", capture_grounding)
    result = await runtime.chat(
        "alice",
        "что в этом файле?",
        actor=auth.actor_for_user("alice", source="test"),
        attachments=[
            {
                "filename": f"advisory-{kind}.bin",
                "transient_text": advisory_text,
                "extraction_success": True,
                "advisory_only": True,
                "verification_eligible": False,
            }
        ],
        enable_tools=False,
    )

    assert advisory_text in shown[0][0]["transient_text"]
    assert _attachment_evidence_chunks(shown[0]) == []
    assert result["attachment_context_available"] is True
    assert result["attachment_verification_complete"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False
    assert result["answer_grounded"] is None
    assert grounding["personal_data_reached_the_turn"] is False
    assert grounding["nothing_arrived"] is True


@pytest.mark.asyncio
async def test_advisory_private_turn_sanitizes_raw_verifier_issues_everywhere(
    settings,
    storage,
    monkeypatch,
):
    private_text = "ADVISORY-PRIVATE-BODY-SENTINEL"
    raw_issue = f"judge quoted {private_text} from the answer"
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(auth, settings),
    )

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            knowledge_hits=[
                {
                    "id": "ko_synthetic_adjacent",
                    "title": "synthetic adjacent record",
                    "content": "unrelated bounded evidence",
                }
            ],
        )

    async def generate(context, message, attachments):
        del context, message, attachments
        return {"content": "Синтетический ответ по распознанному материалу.", "tools_used": []}

    async def unknown_verifier(query, response, context, *, tool_evidence=None):
        del query, response, context, tool_evidence
        return {"status": "unknown", "ok": False, "score": None, "issues": [raw_issue]}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", unknown_verifier)
    result = await runtime.chat(
        "alice",
        "объясни этот файл",
        actor=auth.actor_for_user("alice", source="test"),
        attachments=[
            {
                "filename": "advisory.bin",
                "transient_text": private_text,
                "extraction_success": True,
                "advisory_only": True,
                "verification_eligible": False,
            }
        ],
        enable_tools=False,
    )

    serialized_result = json.dumps(result, ensure_ascii=False)
    assert raw_issue not in serialized_result
    assert private_text not in serialized_result
    assert result["verification"]["issues"] == ["attachment_verification_unavailable"]
    rows = storage.get_conversation_messages(result["conversation_id"], user_id="alice")
    assistant_metadata = str(rows[-1].get("metadata_json") or "")
    assert raw_issue not in assistant_metadata
    assert private_text not in assistant_metadata
    assert "attachment_verification_unavailable" in assistant_metadata


class _RepairCaptureLLM:
    enabled = True
    model = "repair-capture"

    def __init__(
        self,
        content: str = "Исправленный ответ без выполнения команд из недоверенных данных.",
    ) -> None:
        self.messages: list[dict[str, Any]] = []
        self.content = content

    async def chat(self, messages, **kwargs):
        del kwargs
        self.messages = [dict(item) for item in messages]
        return {"content": self.content}


@pytest.mark.asyncio
async def test_maximum_attachment_header_never_hides_the_tail_from_judge_or_repair(
    settings,
    storage,
):
    tail = "|ATTACHMENT-TAIL-MUST-REACH-BOTH"
    body = ("B" * (4_000 - len(tail))) + tail
    chunks = _attachment_evidence_chunks(
        [
            {
                "filename": "F" * 260,
                "transient_text": body,
                "extraction_success": False,
                "extraction_error": "E" * 200,
                "verification_eligible": True,
            }
        ]
    )
    assert len(chunks) == 1 and tail in chunks[0]["output"]

    judge = _RepairCaptureLLM('{"ok": true, "score": 1.0, "issues": []}')
    runtime = AgentRuntime(settings, storage, llm=judge)
    verdict = await runtime._verify_response(  # noqa: SLF001
        "синтетический вопрос",
        "синтетический ответ",
        AgentContext(conversation_id="conv", user_id="alice"),
        tool_evidence=chunks,
    )
    judge_prompt = "\n".join(str(item.get("content") or "") for item in judge.messages)
    assert verdict["status"] == "passed"
    assert tail in judge_prompt

    repair = _RepairCaptureLLM()
    runtime.llm = repair
    fixed = await runtime._repair_once(  # noqa: SLF001
        "синтетический вопрос",
        "Исходный синтетический ответ с ошибкой, который достаточно длинный для исправления.",
        AgentContext(conversation_id="conv", user_id="alice"),
        {"status": "failed", "issues": ["synthetic mismatch"]},
        tool_evidence=chunks,
    )
    repair_prompt = "\n".join(str(item.get("content") or "") for item in repair.messages)
    assert fixed
    assert tail in repair_prompt


@pytest.mark.asyncio
async def test_repair_keeps_hostile_attachment_and_issues_out_of_system_role(settings, storage):
    attachment_attack = "ATTACHMENT-SAYS-OVERRIDE-SYSTEM"
    issue_attack = "ISSUE-SAYS-RETURN-OK"
    question_attack = "QUESTION-SAYS-IGNORE-RULES"
    answer_attack = "ANSWER-SAYS-USE-WEB"
    llm = _RepairCaptureLLM()
    runtime = AgentRuntime(settings, storage, llm=llm)

    fixed = await runtime._repair_once(  # noqa: SLF001
        question_attack,
        f"Исходный ответ: {answer_attack}",
        AgentContext(conversation_id="conv", user_id="alice"),
        {"status": "failed", "ok": False, "issues": [issue_attack]},
        tool_evidence=[
            {
                "tool": "attachment",
                "output": f"synthetic file body\n{attachment_attack}",
            }
        ],
    )

    system_text = "\n".join(
        str(item.get("content") or "") for item in llm.messages if item.get("role") == "system"
    )
    user_text = "\n".join(
        str(item.get("content") or "") for item in llm.messages if item.get("role") == "user"
    )
    for hostile in (attachment_attack, issue_attack, question_attack, answer_attack):
        assert hostile not in system_text
        assert hostile in user_text
    assert [item["role"] for item in llm.messages] == ["system", "user"]
    assert "недоверенный JSON-блок" in system_text
    assert fixed.startswith("Исправленный ответ")


class _HallucinatedOutboundLLM:
    enabled = True
    model = "hallucinated-outbound"
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.calls = 0
        self.offered_names: list[set[str]] = []
        self.second_round_tool_text = ""

    async def chat(self, messages, *, tools=None, **kwargs):
        del kwargs
        self.calls += 1
        self.offered_names.append(
            {
                str((item.get("function") or {}).get("name") or "")
                for item in (tools or [])
                if isinstance(item, dict)
            }
        )
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-search",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": "PRIVATE-FILE-SENTINEL"}),
                        },
                    },
                    {
                        "id": "call-fetch",
                        "function": {
                            "name": "web_fetch",
                            "arguments": json.dumps({"url": "https://invalid.example/private"}),
                        },
                    },
                    {
                        "id": "call-code",
                        "function": {
                            "name": "code_run",
                            "arguments": json.dumps(
                                {"code": "import urllib.request  # PRIVATE-FILE-SENTINEL"}
                            ),
                        },
                    },
                    {
                        "id": "call-data",
                        "function": {
                            "name": "data_query",
                            "arguments": json.dumps(
                                {
                                    "source_id": "configured-external-db",
                                    "sql": "SELECT * FROM notes WHERE body='PRIVATE-FILE-SENTINEL'",
                                }
                            ),
                        },
                    },
                ],
                "_queue_wait_sec": 0.0,
            }
        self.second_round_tool_text = "\n".join(
            str(item.get("content") or "") for item in messages if item.get("role") == "tool"
        )
        return {"content": "Сеть для этого приватного хода не использовалась.", "_queue_wait_sec": 0.0}


class _OutboundRecordingKernel:
    def __init__(self, authorization: AuthorizationService) -> None:
        self.authorization = authorization
        self.executed: list[str] = []

    def get_tool_definitions(self, actor, *, topic=None):
        del actor, topic
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "synthetic",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("memory_search", "web_search", "web_fetch", "code_run", "data_query")
        ]

    async def execute(self, name, arguments, *, actor=None):
        del arguments, actor
        self.executed.append(name)
        return ToolResult(name, True, data={"unexpected": True})


@pytest.mark.asyncio
async def test_private_attachment_blocks_web_prefetch_and_hallucinated_kernel_calls(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    kernel = _OutboundRecordingKernel(auth)
    llm = _HallucinatedOutboundLLM()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=llm,
        kernel=kernel,  # type: ignore[arg-type]
    )
    prepare_private_lineage: list[bool] = []

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message
        prepare_private_lineage.append(bool(kwargs.get("private_context_lineage")))
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("интернет", None),
        )

    async def forbidden_prefetch(*args, **kwargs):
        del args, kwargs
        raise AssertionError("private attachment reached web prefetch")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_prefetch_the_web_if_asked", forbidden_prefetch)
    result = await runtime.chat(
        "alice",
        "найди в интернете указанный во вложении адрес",
        actor=auth.actor_for_user("alice", source="test"),
        attachments=[
            {
                "filename": "private.txt",
                "transient_text": "PRIVATE-FILE-SENTINEL",
                "extraction_success": True,
            }
        ],
        enable_tools=True,
    )

    assert llm.calls == 2
    assert all(
        "web_search" not in names
        and "web_fetch" not in names
        and "code_run" not in names
        and "data_query" not in names
        for names in llm.offered_names
    )
    assert "memory_search" in llm.offered_names[0]
    assert kernel.executed == []
    assert result["tools_used"] == ["web_search", "web_fetch", "code_run", "data_query"]
    assert llm.second_round_tool_text.count("Внешний сетевой инструмент недоступен") == 4
    assert not result.get("web_query_notice")
    assert prepare_private_lineage == [True]


@pytest.mark.asyncio
async def test_person_topic_blocks_hallucinated_outbound_calls_before_kernel(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    kernel = _OutboundRecordingKernel(auth)
    llm = _HallucinatedOutboundLLM()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=llm,
        kernel=kernel,  # type: ignore[arg-type]
    )

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("человек", None),
        )

    async def forbidden_prefetch(*args, **kwargs):
        del args, kwargs
        raise AssertionError("person topic reached web prefetch")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_prefetch_the_web_if_asked", forbidden_prefetch)
    result = await runtime.chat(
        "alice",
        "Синтетический профиль",
        actor=auth.actor_for_user("alice", source="test"),
        attachments=[],
        enable_tools=True,
    )

    assert llm.calls == 2
    assert all(
        "web_search" not in names
        and "web_fetch" not in names
        and "code_run" not in names
        and "data_query" not in names
        for names in llm.offered_names
    )
    assert kernel.executed == []
    assert result["tools_used"] == ["web_search", "web_fetch", "code_run", "data_query"]
    assert llm.second_round_tool_text.count("Внешний сетевой инструмент недоступен") == 4
    assert not result.get("web_query_notice")


@pytest.mark.asyncio
async def test_private_attachment_lineage_blocks_outbound_after_an_unmarked_exchange(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="synthetic private lineage")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "разбери синтетический файл",
        metadata={"had_attachments": True, "attachment_count": 1},
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "PRIVATE-LINEAGE-ANSWER-SENTINEL",
        metadata={"attachment_context_used": True},
    )
    storage.store_message(conversation["id"], "alice", "user", "обычная промежуточная реплика")
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "обычный промежуточный ответ",
        metadata={"attachment_context_used": False},
    )

    auth = AuthorizationService(storage)
    kernel = _OutboundRecordingKernel(auth)
    llm = _HallucinatedOutboundLLM()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=llm,
        kernel=kernel,  # type: ignore[arg-type]
    )
    prepare_private_lineage: list[bool] = []

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message
        prepare_private_lineage.append(bool(kwargs.get("private_context_lineage")))
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("интернет", None),
        )

    async def forbidden_prefetch(*args, **kwargs):
        del args, kwargs
        raise AssertionError("private lineage reached web prefetch")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_prefetch_the_web_if_asked", forbidden_prefetch)
    result = await runtime.chat(
        "alice",
        "Синтетический новый вопрос",
        actor=auth.actor_for_user("alice", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=True,
    )

    assert llm.calls == 2
    assert kernel.executed == []
    assert all(
        not {"web_search", "web_fetch", "code_run", "data_query"}.intersection(names)
        for names in llm.offered_names
    )
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice")
    latest_metadata = json.loads(rows[-1]["metadata_json"])
    assert latest_metadata["private_context_lineage"] is True
    assert latest_metadata["attachment_context_used"] is False
    assert result["tools_used"] == ["web_search", "web_fetch", "code_run", "data_query"]
    assert prepare_private_lineage == [True]


def test_private_lineage_scan_is_independent_of_prompt_character_budget():
    marked_assistant = {
        "role": "assistant",
        "content": "synthetic private answer",
        "metadata_json": json.dumps({"private_context_lineage": True}),
    }
    oversized_user = {
        "role": "user",
        "content": "x" * 10_000,
        "metadata_json": "{}",
    }

    assert AgentRuntime._history_has_private_context_lineage(  # noqa: SLF001
        [marked_assistant, oversized_user]
    )

    # Mutation controls: the exact boolean marker on a supported conversation
    # role is the authority, not a truthy string or arbitrary metadata carrier.
    for mutated_marker, mutated_role in ((False, "assistant"), ("true", "assistant"), (True, "tool")):
        mutated_assistant = {
            **marked_assistant,
            "role": mutated_role,
            "metadata_json": json.dumps({"private_context_lineage": mutated_marker}),
        }
        assert not AgentRuntime._history_has_private_context_lineage(  # noqa: SLF001
            [mutated_assistant, oversized_user]
        )


@pytest.mark.asyncio
async def test_private_lineage_survives_a_crash_after_oversized_user_persistence(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="synthetic crash lineage")
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "synthetic private answer",
        metadata={"private_context_lineage": True},
    )
    auth = AuthorizationService(storage)
    kernel = _OutboundRecordingKernel(auth)
    llm = _HallucinatedOutboundLLM()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=llm,
        kernel=kernel,  # type: ignore[arg-type]
    )

    async def crash_after_user_persistence(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("synthetic crash after user persistence")

    monkeypatch.setattr(runtime, "_prepare_context", crash_after_user_persistence)
    with pytest.raises(RuntimeError, match="synthetic crash after user persistence"):
        await runtime.chat(
            "alice",
            "x" * 10_000,
            actor=auth.actor_for_user("alice", source="test"),
            conversation_id=conversation["id"],
            attachments=[],
            enable_tools=False,
        )

    crashed_rows = storage.get_conversation_messages(conversation["id"], user_id="alice")
    crashed_user_metadata = json.loads(crashed_rows[-1]["metadata_json"])
    assert crashed_rows[-1]["role"] == "user"
    assert crashed_user_metadata["private_context_lineage"] is True

    # Push the original assistant marker out of the fetched 20-row tail.  The
    # crash-persisted user marker is now the sole authority, and its 10k body is
    # also outside the prompt's character-budgeted slice.
    for index in range(19):
        storage.store_message(
            conversation["id"],
            "alice",
            "assistant",
            f"synthetic neutral row {index}",
            metadata={"private_context_lineage": False},
        )

    seen_lineage: list[bool] = []

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message
        seen_lineage.append(bool(kwargs.get("private_context_lineage")))
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("интернет", None),
        )

    async def forbidden_prefetch(*args, **kwargs):
        del args, kwargs
        raise AssertionError("crash-persisted private lineage reached web prefetch")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_prefetch_the_web_if_asked", forbidden_prefetch)
    result = await runtime.chat(
        "alice",
        "synthetic next turn",
        actor=auth.actor_for_user("alice", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=True,
    )

    assert seen_lineage == [True]
    assert llm.calls == 2
    assert kernel.executed == []
    assert all(
        not {"web_search", "web_fetch", "code_run", "data_query"}.intersection(names)
        for names in llm.offered_names
    )
    assert result["tools_used"] == ["web_search", "web_fetch", "code_run", "data_query"]
    final_rows = storage.get_conversation_messages(conversation["id"], user_id="alice")
    next_user_metadata = json.loads(final_rows[-2]["metadata_json"])
    next_assistant_metadata = json.loads(final_rows[-1]["metadata_json"])
    assert next_user_metadata["private_context_lineage"] is True
    assert next_assistant_metadata["private_context_lineage"] is True


@pytest.mark.parametrize("verdict", ["поправка", "правило"])
@pytest.mark.asyncio
async def test_private_attachment_lineage_never_enters_global_learning(
    verdict,
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_UnusedEnabledLLM(),
        kernel=ExecutionKernel(AuthorizationService(storage), settings),
    )
    learned: list[str] = []

    async def classify(*args, **kwargs):
        del args, kwargs
        return verdict, "synthetic proposal"

    async def learn_correction(*args, **kwargs):
        del args, kwargs
        learned.append("correction")

    async def learn_rule(*args, **kwargs):
        del args, kwargs
        learned.append("rule")
        return True

    monkeypatch.setattr(runtime, "_web_query_by_arbiter", classify)
    monkeypatch.setattr(runtime, "_learn_a_correction", learn_correction)
    monkeypatch.setattr(runtime, "_learn_a_standing_rule", learn_rule)

    context = await runtime._prepare_context(  # noqa: SLF001
        "alice",
        "Исправь синтетическое утверждение в этом разговоре",
        "conv_private_learning",
        prior_history=[
            {"role": "user", "content": "разбери синтетический файл"},
            {"role": "assistant", "content": "PRIVATE-CONTEXT-SENTINEL"},
        ],
        person_id="alice",
        private_context_lineage=True,
    )

    assert context.outward_verdict == (verdict, "synthetic proposal")
    assert learned == []
    user = storage.get_user("alice")
    metadata = json.loads(str((user or {}).get("metadata_json") or "{}"))
    assert not metadata.get("corrections")
    assert not metadata.get("standing_rules")


def test_short_voice_question_is_not_duplicated_as_attachment_evidence(settings, monkeypatch):
    from friday.server import create_app

    transcript = "VOICE-PROJECTION-ONE-COPY|" + ("x" * 2_100) + "|VOICE-TAIL-SURVIVES"
    app = create_app(replace(settings, verify_answers=False))
    captured: dict[str, Any] = {}

    with TestClient(app) as client:

        async def ingest_voice(user_id, _title, _content, **kwargs):
            raw = _stored_file(
                app.state.storage,
                user_id,
                transcript,
                filename=str(kwargs.get("filename") or "voice.oga"),
                uploader=str((kwargs.get("metadata") or {}).get("uploaded_by") or user_id),
                metadata={
                    "text_extraction_success": False,
                    "transcription": {"engine": "synthetic"},
                },
            )
            return {
                "raw_object_id": raw.id,
                "transcript_text": transcript,
                "queued_for_review": True,
                "promoted": False,
                "knowledge_object": None,
                "extraction": {
                    "success": True,
                    "text_success": False,
                    "chars": len(transcript),
                },
            }

        async def chat_spy(user_id, message, **kwargs):
            captured.update(
                user_id=user_id,
                message=message,
                attachments=[dict(item) for item in kwargs.get("attachments") or []],
                answer_with_voice=kwargs.get("answer_with_voice"),
            )
            return {
                "conversation_id": "conv_voice_projection",
                "answer": "ok",
                "message": {"role": "assistant", "content": "ok"},
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.ingestion, "ingest_file", ingest_voice)
        monkeypatch.setattr(app.state.agent, "chat", chat_spy)
        response = client.post(
            "/api/chat",
            json={
                "document": {
                    "filename": "voice.oga",
                    "mime_type": "audio/ogg",
                    "content_base64": base64.b64encode(b"synthetic-voice").decode("ascii"),
                    "media_kind": "voice",
                    "duration": 4,
                }
            },
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text

    assert captured["message"] == transcript
    assert captured["answer_with_voice"] is True
    # The exact transcript is already the user message.  It must not also be a
    # private attachment: that would duplicate the prompt and disable web tools
    # for an ordinary voice question such as «найди погоду».
    assert captured["attachments"] == []


def test_voice_question_cut_at_the_24k_bound_is_explicit(settings, monkeypatch):
    from friday.server import create_app

    transcript = "VOICE-LONG-START|" + ("x" * 24_100) + "|VOICE-LONG-TAIL"
    app = create_app(replace(settings, verify_answers=False))
    captured: dict[str, Any] = {}

    with TestClient(app) as client:

        async def ingest_voice(user_id, _title, _content, **kwargs):
            raw = _stored_file(
                app.state.storage,
                user_id,
                transcript,
                filename=str(kwargs.get("filename") or "voice.oga"),
                uploader=str((kwargs.get("metadata") or {}).get("uploaded_by") or user_id),
                metadata={
                    "text_extraction_success": False,
                    "transcription": {"engine": "synthetic"},
                },
            )
            return {
                "raw_object_id": raw.id,
                "transcript_text": transcript,
                "queued_for_review": True,
                "promoted": False,
                "knowledge_object": None,
                "extraction": {
                    "success": True,
                    "text_success": False,
                    "chars": len(transcript),
                },
            }

        async def chat_spy(user_id, message, **kwargs):
            captured.update(
                user_id=user_id,
                message=message,
                attachments=[dict(item) for item in kwargs.get("attachments") or []],
            )
            return {
                "conversation_id": "conv_long_voice_projection",
                "message": "synthetic answer",
                "grounding_warning": "",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.ingestion, "ingest_file", ingest_voice)
        monkeypatch.setattr(app.state.agent, "chat", chat_spy)
        response = client.post(
            "/api/chat",
            json={
                "document": {
                    "filename": "long-voice.oga",
                    "mime_type": "audio/ogg",
                    "content_base64": base64.b64encode(b"synthetic-long-voice").decode("ascii"),
                    "media_kind": "voice",
                    "duration": 179,
                }
            },
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

    assert len(captured["message"]) == 24_000
    assert captured["message"].startswith("VOICE-LONG-START|")
    assert "VOICE-LONG-TAIL" not in captured["message"]
    assert captured["attachments"] == []
    assert payload["voice_transcript_truncated"] is True
    assert payload["file_ingestion"]["voice_transcript_truncated"] is True
    assert "распознано не полностью" in payload["grounding_warning"]


def test_content_source_replay_projects_native_raw_text_in_the_same_turn():
    native_text = "NATIVE-RAW-REPLAY-TEXT"
    attachment = _current_turn_file_attachment(
        filename="replayed.docx",
        file_ingestion={
            "raw_object_id": "raw_synthetic_replay",
            "idempotent_replay": True,
        },
        raw={
            "raw_content": native_text,
            "metadata_json": json.dumps(
                {
                    "filename": "replayed.docx",
                    "uploaded_by": "alice",
                    "extraction_success": True,
                    "text_extraction_success": True,
                }
            ),
        },
    )

    assert attachment["transient_text"] == native_text
    assert attachment["extraction_success"] is True
    assert attachment["verification_eligible"] is True
    assert attachment["advisory_only"] is False


@pytest.mark.asyncio
async def test_shared_tenant_file_dedup_is_scoped_to_exact_uploader(settings, storage):
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    tenant_id = "shared-tenant"
    first_person = "person-one"
    second_person = "person-two"
    for user_id in (tenant_id, first_person, second_person):
        storage.ensure_user(user_id, preset_key="owner")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))

    same_bytes = ("Одинаковые байты одного синтетического документа. " * 5).encode()
    first = await pipeline.ingest_file(
        tenant_id,
        None,
        same_bytes,
        filename="first.txt",
        source_ref="shared-source:same-bytes",
        metadata={"uploaded_by": first_person},
        force_review=True,
    )
    second = await pipeline.ingest_file(
        tenant_id,
        None,
        same_bytes,
        filename="second.txt",
        source_ref="shared-source:same-bytes",
        metadata={"uploaded_by": second_person},
        force_review=True,
    )

    # Different bytes with identical normalized extracted text exercise the
    # second dedup road (`text_sha256`) under the same uploader boundary.
    text_first = await pipeline.ingest_file(
        tenant_id,
        None,
        b"same extracted\ntext for two people",
        filename="first-resave.txt",
        source_ref="shared-source:same-text",
        metadata={"uploaded_by": first_person},
        force_review=True,
    )
    text_second = await pipeline.ingest_file(
        tenant_id,
        None,
        b"same extracted text for two people",
        filename="second-resave.txt",
        source_ref="shared-source:same-text",
        metadata={"uploaded_by": second_person},
        force_review=True,
    )

    assert first["raw_object_id"] != second["raw_object_id"]
    assert text_first["raw_object_id"] != text_second["raw_object_id"]
    assert second.get("idempotent_replay") is not True
    assert text_second.get("idempotent_replay") is not True
    source_rows = storage.execute(
        "SELECT source_ref FROM raw_objects WHERE user_id=? ORDER BY source_ref",
        (tenant_id,),
    ).fetchall()
    assert len(source_rows) == 4
    assert all(str(row["source_ref"]).startswith("uploader:") for row in source_rows)

    runtime = AgentRuntime(settings, storage)
    assert (
        runtime._owned_file_attachment(  # noqa: SLF001
            str(second["raw_object_id"]),
            tenant_id=tenant_id,
            person_id=second_person,
        )
        is not None
    )
    assert (
        runtime._owned_file_attachment(  # noqa: SLF001
            str(first["raw_object_id"]),
            tenant_id=tenant_id,
            person_id=second_person,
        )
        is None
    )
