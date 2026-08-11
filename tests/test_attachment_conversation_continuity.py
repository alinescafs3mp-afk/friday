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


def _record_upload(storage, conversation_id: str, user_id: str, raw: RawObject, caption: str) -> None:
    storage.store_message(
        conversation_id,
        user_id,
        "user",
        caption,
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "attachment_origin": "upload",
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    storage.store_message(
        conversation_id,
        user_id,
        "assistant",
        f"прочитан {caption}",
        metadata={"attachment_context_used": True},
    )


def _current_attachment(raw: RawObject) -> dict[str, object]:
    metadata = raw.metadata_json if isinstance(raw.metadata_json, dict) else {}
    return {
        "raw_object_id": raw.id,
        "filename": str(metadata.get("filename") or "attachment"),
        "transient_text": raw.raw_content,
        "extraction_success": True,
    }


def _patch_attachment_generation(runtime, monkeypatch):  # noqa: ANN001
    seen: list[tuple[str, list[dict]]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def generate(context, message, attachments):  # noqa: ANN001
        del context
        snapshot = [dict(item) for item in (attachments or [])]
        seen.append((str(message), snapshot))
        names = [str(item.get("filename") or "attachment") for item in snapshot]
        return {"content": "Синтетический ответ: " + ", ".join(names), "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    return seen


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
    assert "прочитать не удалось" in unreadable_turn["message"]
    assert transient_turn["attachment_context_available"] is True
    assert after_transient["restored_attachment_count"] == 0
    assert after_transient["attachment_context_available"] is False
    assert seen[6] == [], "a no-save file allowed an older persisted file to return"

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


@pytest.mark.parametrize(
    ("query", "expected_indices"),
    [
        ("Что указано в файле «alpha-plan.txt»?", [0]),
        ("Что сказано в первом загруженном файле?", [0]),
        ("Что сказано во втором документе?", [1]),
        ("Что сказано в последнем файле?", [2]),
        ("Сравни файлы «alpha-plan.txt» и «beta-budget.txt»", [0, 1]),
        ("Сравни первый и третий загруженные файлы", [0, 2]),
        ("Обобщи все загруженные файлы", [0, 1, 2]),
    ],
)
def test_conversation_catalog_resolves_names_ordinals_and_sets(
    settings,
    storage,
    query,
    expected_indices,
):
    files = [
        _pending_file(storage, "alice", "alice", "ALPHA-ONLY", filename="alpha-plan.txt"),
        _pending_file(storage, "alice", "alice", "BETA-ONLY", filename="beta-budget.txt"),
        _pending_file(storage, "alice", "alice", "GAMMA-LATEST", filename="gamma-latest.txt"),
    ]
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", files[0], "alpha")
    storage.store_message(conversation["id"], "alice", "user", "обычный вопрос между загрузками")
    storage.store_message(conversation["id"], "alice", "assistant", "обычный ответ")
    _record_upload(storage, conversation["id"], "alice", files[1], "beta")
    _record_upload(storage, conversation["id"], "alice", files[2], "gamma")
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        query,
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    expected_ids = [files[index].id for index in expected_indices]
    assert [item["raw_object_id"] for item in restored] == expected_ids
    assert expected == len(expected_ids)
    exposed = json.dumps(restored, ensure_ascii=False)
    for index, raw in enumerate(files):
        if index not in expected_indices:
            assert raw.raw_content not in exposed


@pytest.mark.parametrize(
    ("filenames", "query", "expected_indices", "expected_count"),
    [
        (["report.pdf", "old-report.pdf", "third.txt"], "Что в report.pdf?", [0], 1),
        (["report.pdf", "old-report.pdf", "third.txt"], "Что в old-report.pdf?", [1], 1),
        (["report.pdf", "annual report.pdf", "third.txt"], "Что в annual report.pdf?", [1], 1),
        (
            ["report.pdf", "annual report.pdf", "third.txt"],
            "Сравни annual report.pdf и report.pdf",
            [0, 1],
            2,
        ),
        (
            ["report.pdf", "old-report.pdf", "third.txt"],
            "Сравни report.pdf и третий файл",
            [0, 2],
            2,
        ),
        (["plain.txt", "first-report.txt", "third.txt"], "Что в first-report.txt?", [1], 1),
        (["one.txt", "two.txt", "three.txt"], "Сравни 1-й и 3-й файлы", [0, 2], 2),
        (["one.txt", "two.txt", "three.txt"], "Сравни файлы №1 и №3", [0, 2], 2),
        (
            ["one.txt", "two.txt", "three.txt", "four.txt"],
            "Обобщи первые 2 файла",
            [0, 1],
            2,
        ),
        (
            ["one.txt", "two.txt", "three.txt", "four.txt"],
            "Обобщи последние 2 файла",
            [2, 3],
            2,
        ),
        (["report.pdf", "scan.jpg"], "Сравни report.pdf и scan.jpg.", [0, 1], 2),
        (["report.pdf", "scan.jpg"], "Что в scan.jpg.", [1], 1),
        (["one.txt", "two.txt", "three.txt"], "Что в пятом файле?", [], 1),
        (["one.txt", "two.txt", "three.txt"], "Что в файле №99?", [], 1),
    ],
)
def test_catalog_selector_boundaries_ranges_and_mixed_references(
    settings,
    storage,
    filenames,
    query,
    expected_indices,
    expected_count,
):
    files = [
        _pending_file(
            storage,
            "alice",
            "alice",
            f"CATALOG-CONTENT-{index}",
            filename=filename,
        )
        for index, filename in enumerate(filenames)
    ]
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    for raw in files:
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        query,
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert [item["raw_object_id"] for item in restored] == [files[index].id for index in expected_indices]
    assert expected == expected_count
    exposed = json.dumps(restored, ensure_ascii=False)
    for index, raw in enumerate(files):
        if index not in expected_indices:
            assert raw.raw_content not in exposed


def test_indirect_content_clue_selects_the_unique_older_file(settings, storage):
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 — срок согласования 14 дней\nTARGET-TAIL",
        filename="old-contract.txt",
    )
    decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "LATEST-DECOY-WITHOUT-THE-ANCHOR",
        filename="latest.txt",
    )
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", target, "old")
    _record_upload(storage, conversation["id"], "alice", decoy, "latest")
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Обобщи тот из моих файлов, где встречается «ORION-77»",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert expected == 1
    assert [item["raw_object_id"] for item in restored] == [target.id]
    assert "TARGET-TAIL" in restored[0]["transient_text"]
    assert "LATEST-DECOY" not in json.dumps(restored, ensure_ascii=False)


@pytest.mark.asyncio
async def test_fresh_conversation_resolves_an_exact_filename_from_the_uploaders_global_catalog(
    settings,
    storage,
    monkeypatch,
):
    alpha = _pending_file(
        storage,
        "alice",
        "alice",
        "GLOBAL-ALPHA-ONLY",
        filename="alpha.pdf",
    )
    newest_decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "GLOBAL-NEWEST-DECOY-MUST-STAY-OUT",
        filename="newest.pdf",
    )
    upload_conversation = storage.create_conversation("alice")
    _record_upload(storage, upload_conversation["id"], "alice", alpha, "alpha upload")
    _record_upload(storage, upload_conversation["id"], "alice", newest_decoy, "newest upload")
    fresh_conversation = storage.create_conversation("alice")

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    result = await runtime.chat(
        "alice",
        "Что в alpha.pdf?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=fresh_conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in attachments] for _message, attachments in seen] == [[alpha.id]]
    assert result["restored_attachment_count"] == 1
    assert result["attachment_context_expected_count"] == 1
    assert "GLOBAL-NEWEST-DECOY" not in json.dumps([result, seen], ensure_ascii=False)


@pytest.mark.asyncio
async def test_fresh_conversation_global_indirect_clue_requires_one_unique_file(
    settings,
    storage,
    monkeypatch,
):
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 GLOBAL-UNIQUE-TARGET",
        filename="orion-primary.pdf",
    )
    decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "GLOBAL-DECOY-WITHOUT-CLUE",
        filename="newest-decoy.pdf",
    )
    first_upload_conversation = storage.create_conversation("alice")
    _record_upload(storage, first_upload_conversation["id"], "alice", target, "target upload")
    _record_upload(storage, first_upload_conversation["id"], "alice", decoy, "decoy upload")

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    unique_conversation = storage.create_conversation("alice")
    unique = await runtime.chat(
        "alice",
        "Что по ORION-77 в моих файлах?",
        actor=actor,
        conversation_id=unique_conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    duplicate = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 GLOBAL-SECOND-PRIVATE-MATCH",
        filename="orion-duplicate.pdf",
    )
    duplicate_upload_conversation = storage.create_conversation("alice")
    _record_upload(
        storage,
        duplicate_upload_conversation["id"],
        "alice",
        duplicate,
        "duplicate upload",
    )
    ambiguous_conversation = storage.create_conversation("alice")
    ambiguous = await runtime.chat(
        "alice",
        "Что по ORION-77 в моих файлах?",
        actor=actor,
        conversation_id=ambiguous_conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in attachments] for _message, attachments in seen] == [
        [target.id]
    ]
    assert unique["restored_attachment_count"] == 1
    assert unique["attachment_context_expected_count"] == 1
    assert ambiguous["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" in ambiguous["message"].casefold()
    assert "GLOBAL-SECOND-PRIVATE-MATCH" not in json.dumps(ambiguous, ensure_ascii=False)


@pytest.mark.asyncio
async def test_all_files_beyond_the_message_catalog_cap_is_never_certified_as_only_the_tail(
    settings,
    storage,
    monkeypatch,
):
    first = _pending_file(storage, "alice", "alice", "CAP-FIRST-BODY", filename="alpha.pdf")
    second = _pending_file(storage, "alice", "alice", "CAP-SECOND-BODY", filename="beta.pdf")
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", first, "alpha upload")
    for index in range(1_001):
        storage.store_message(conversation["id"], "alice", "assistant", f"filler-{index:04d}")
    _record_upload(storage, conversation["id"], "alice", second, "beta upload")

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    all_files = await runtime.chat(
        "alice",
        "Обобщи все файлы",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )
    exact_second = await runtime.chat(
        "alice",
        "Что в beta.pdf?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    exact_calls = [attachments for message, attachments in seen if message == "Что в beta.pdf?"]
    assert [[item["raw_object_id"] for item in call] for call in exact_calls] == [[second.id]]
    assert exact_second["attachment_context_expected_count"] == 1
    assert "CAP-FIRST-BODY" not in json.dumps(exact_calls, ensure_ascii=False)

    all_calls = [attachments for message, attachments in seen if message == "Обобщи все файлы"]
    if all_calls:
        assert [[item["raw_object_id"] for item in call] for call in all_calls] == [[first.id, second.id]]
    else:
        assert any(
            phrase in all_files["message"].casefold()
            for phrase in ("полнота", "не удалось однозначно", "неизвест")
        )


def test_document_catalog_excludes_voice_and_wrong_uploader(settings, storage):
    document = _pending_file(storage, "shared", "alice", "OWN-DOCUMENT", filename="report.pdf")
    ignored = _pending_file(storage, "shared", "alice", "IGNORED-DOCUMENT", filename="ignored.pdf")
    storage.execute(
        "UPDATE inbox SET status='ignored' WHERE raw_object_id=? AND user_id=?",
        (ignored.id, "shared"),
    )
    voice = _pending_file(storage, "shared", "alice", "VOICE-TRANSCRIPT", filename="voice.ogg")
    voice.metadata_json.update({"media_kind": "voice", "mime_type": "audio/ogg"})
    storage.execute(
        "UPDATE raw_objects SET metadata_json=? WHERE id=?",
        (json.dumps(voice.metadata_json), voice.id),
    )
    foreign = _pending_file(storage, "shared", "bob", "FOREIGN-DOCUMENT", filename="foreign.pdf")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    for raw in (document, ignored, voice, foreign):
        _record_upload(storage, conversation["id"], "alice", raw, raw.id)
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Обобщи все загруженные файлы",
        history,
        tenant_id="shared",
        person_id="alice",
        allow_file_read=True,
    )

    assert expected == 1
    assert [item["raw_object_id"] for item in restored] == [document.id]
    assert "IGNORED-DOCUMENT" not in json.dumps(restored, ensure_ascii=False)
    assert "VOICE-TRANSCRIPT" not in json.dumps(restored, ensure_ascii=False)
    assert "FOREIGN-DOCUMENT" not in json.dumps(restored, ensure_ascii=False)


@pytest.mark.asyncio
async def test_named_pair_becomes_the_exact_deictic_active_set(
    settings,
    storage,
    monkeypatch,
):
    alpha = _pending_file(
        storage,
        "alice",
        "alice",
        "ALPHA-PAIR-ONLY",
        filename="alpha-plan.txt",
    )
    beta = _pending_file(
        storage,
        "alice",
        "alice",
        "BETA-PAIR-ONLY",
        filename="beta-budget.txt",
    )
    gamma = _pending_file(
        storage,
        "alice",
        "alice",
        "GAMMA-LATEST-MUST-STAY-OUT",
        filename="gamma-latest.txt",
    )
    conversation = storage.create_conversation("alice")
    for raw in (alpha, beta, gamma):
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    selected = await runtime.chat(
        "alice",
        "Сравни файлы alpha-plan.txt и beta-budget.txt",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )
    continued = await runtime.chat(
        "alice",
        "А что в них?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    expected_ids = [alpha.id, beta.id]
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [
        expected_ids,
        expected_ids,
    ]
    assert selected["restored_attachment_count"] == 2
    assert continued["restored_attachment_count"] == 2
    assert selected["attachment_context_expected_count"] == 2
    assert continued["attachment_context_expected_count"] == 2
    assert "GAMMA-LATEST-MUST-STAY-OUT" not in json.dumps(seen, ensure_ascii=False)

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    selected_row = next(
        row
        for row in rows
        if row.get("role") == "user" and row.get("content") == "Сравни файлы alpha-plan.txt и beta-budget.txt"
    )
    selected_metadata = json.loads(str(selected_row.get("metadata_json") or "{}"))
    assert selected_metadata["conversation_attachment_raw_ids"] == expected_ids


@pytest.mark.asyncio
async def test_both_files_reuses_the_previously_selected_pair(
    settings,
    storage,
    monkeypatch,
):
    alpha = _pending_file(storage, "alice", "alice", "ALPHA-SELECTED", filename="alpha.txt")
    beta = _pending_file(storage, "alice", "alice", "BETA-SELECTED", filename="beta.txt")
    latest = _pending_file(
        storage,
        "alice",
        "alice",
        "LATEST-UNSELECTED",
        filename="latest.txt",
    )
    conversation = storage.create_conversation("alice")
    for raw in (alpha, beta, latest):
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    await runtime.chat(
        "alice",
        "Сравни alpha.txt и beta.txt",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )
    both = await runtime.chat(
        "alice",
        "Сравни оба файла",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    expected_ids = [alpha.id, beta.id]
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [
        expected_ids,
        expected_ids,
    ]
    assert both["restored_attachment_count"] == 2
    assert both["attachment_context_expected_count"] == 2
    assert "LATEST-UNSELECTED" not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "prior_filename"),
    [
        ("Сравни этот файл с alpha-plan.txt", "alpha-plan.txt"),
        ("Сравни с report.pdf", "report.pdf"),
    ],
)
async def test_current_file_can_be_compared_with_one_exact_named_prior_file(
    settings,
    storage,
    monkeypatch,
    query,
    prior_filename,
):
    alpha = _pending_file(
        storage,
        "alice",
        "alice",
        "ALPHA-PRIOR-ONLY",
        filename=prior_filename,
    )
    beta = _pending_file(
        storage,
        "alice",
        "alice",
        "BETA-PRIOR-DECOY",
        filename="beta-budget.txt",
    )
    gamma = _pending_file(
        storage,
        "alice",
        "alice",
        "GAMMA-CURRENT-ONLY",
        filename="gamma-current.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", alpha, "alpha")
    _record_upload(storage, conversation["id"], "alice", beta, "beta")

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    result = await runtime.chat(
        "alice",
        query,
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[_current_attachment(gamma)],
        enable_tools=False,
    )

    expected_ids = [alpha.id, gamma.id]
    assert len(seen) == 1
    assert [item["raw_object_id"] for item in seen[0][1]] == expected_ids
    assert result["restored_attachment_count"] == 1
    assert result["attachment_context_expected_count"] == 2
    assert result["attachment_context_readable_count"] == 2
    exposed = json.dumps(seen, ensure_ascii=False)
    assert "ALPHA-PRIOR-ONLY" in exposed
    assert "GAMMA-CURRENT-ONLY" in exposed
    assert "BETA-PRIOR-DECOY" not in exposed

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    comparison_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    comparison_metadata = json.loads(str(comparison_row.get("metadata_json") or "{}"))
    assert comparison_metadata["conversation_attachment_raw_ids"] == expected_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "Сравни alpha-plan.txt и beta-budget.txt",
        "Сравни первые 2 файла",
    ],
)
async def test_complete_selector_does_not_add_an_unrequested_current_file(
    settings,
    storage,
    monkeypatch,
    query,
):
    alpha = _pending_file(
        storage,
        "alice",
        "alice",
        "ALPHA-EXPLICIT-ONLY",
        filename="alpha-plan.txt",
    )
    beta = _pending_file(
        storage,
        "alice",
        "alice",
        "BETA-EXPLICIT-ONLY",
        filename="beta-budget.txt",
    )
    current = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-MUST-NOT-BE-ADDED",
        filename="current.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", alpha, "alpha")
    _record_upload(storage, conversation["id"], "alice", beta, "beta")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[_current_attachment(current)],
        enable_tools=False,
    )

    expected_ids = [alpha.id, beta.id]
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [expected_ids]
    assert result["attachment_context_expected_count"] == 2
    assert result["restored_attachment_count"] == 2
    assert "CURRENT-MUST-NOT-BE-ADDED" not in json.dumps(seen, ensure_ascii=False)
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert metadata["conversation_attachment_raw_ids"] == expected_ids
    assert metadata["conversation_uploaded_raw_ids"] == [current.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_target", "expected_restored"),
    [
        ("Что в report.pdf?", "prior", 1),
        ("Что в current.txt?", "current", 0),
    ],
)
async def test_explicit_name_replaces_an_unrequested_current_attachment(
    settings,
    storage,
    monkeypatch,
    query,
    expected_target,
    expected_restored,
):
    prior = _pending_file(
        storage,
        "alice",
        "alice",
        "PRIOR-REPORT-ONLY",
        filename="report.pdf",
    )
    current = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-FILE-ONLY",
        filename="current.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", prior, "prior report")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[_current_attachment(current)],
        enable_tools=False,
    )

    expected = prior if expected_target == "prior" else current
    excluded = current if expected_target == "prior" else prior
    assert len(seen) == 1
    assert [item["raw_object_id"] for item in seen[0][1]] == [expected.id]
    assert result["restored_attachment_count"] == expected_restored
    assert result["attachment_context_expected_count"] == 1
    exposed = json.dumps(seen, ensure_ascii=False)
    assert expected.raw_content in exposed
    assert excluded.raw_content not in exposed

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    request_metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert request_metadata["conversation_attachment_raw_ids"] == [expected.id]


@pytest.mark.asyncio
async def test_uploaded_current_file_stays_in_catalog_when_prior_file_is_the_active_selection(
    settings,
    storage,
    monkeypatch,
):
    prior = _pending_file(
        storage,
        "alice",
        "alice",
        "PRIOR-REPORT-TWO-TURN",
        filename="report.pdf",
    )
    current = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-TWO-TURN",
        filename="current.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", prior, "prior report")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    selected_prior = await runtime.chat(
        "alice",
        "Что в report.pdf?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[_current_attachment(current)],
        enable_tools=False,
    )
    selected_current = await runtime.chat(
        "alice",
        "Что в current.txt?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [
        [prior.id],
        [current.id],
    ]
    assert selected_prior["restored_attachment_count"] == 1
    assert selected_current["restored_attachment_count"] == 1
    assert selected_prior["attachment_context_expected_count"] == 1
    assert selected_current["attachment_context_expected_count"] == 1
    assert "CURRENT-TWO-TURN" not in json.dumps(seen[0], ensure_ascii=False)
    assert "PRIOR-REPORT-TWO-TURN" not in json.dumps(seen[1], ensure_ascii=False)

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    prior_query_row = next(
        row for row in rows if row.get("role") == "user" and row.get("content") == "Что в report.pdf?"
    )
    current_query_row = next(
        row for row in rows if row.get("role") == "user" and row.get("content") == "Что в current.txt?"
    )
    prior_metadata = json.loads(str(prior_query_row.get("metadata_json") or "{}"))
    current_metadata = json.loads(str(current_query_row.get("metadata_json") or "{}"))
    assert prior_metadata["conversation_attachment_raw_ids"] == [prior.id]
    assert prior_metadata["conversation_uploaded_raw_ids"] == [current.id]
    assert current_metadata["conversation_attachment_raw_ids"] == [current.id]
    assert "conversation_uploaded_raw_ids" not in current_metadata


@pytest.mark.asyncio
async def test_ambiguous_indirect_content_clue_fails_closed_but_a_no_hit_topic_is_ordinary(
    settings,
    storage,
    monkeypatch,
):
    first = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 FIRST-PRIVATE-MATCH",
        filename="first-orion.txt",
    )
    second = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 SECOND-PRIVATE-MATCH",
        filename="second-orion.txt",
    )
    conversation = storage.create_conversation("alice")
    for raw in (first, second):
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    ambiguous = await runtime.chat(
        "alice",
        "Что по ORION-77?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )
    weather = await runtime.chat(
        "alice",
        "Что по погоде?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [(message, attachments) for message, attachments in seen] == [
        ("Что по погоде?", []),
    ]
    assert ambiguous["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" in ambiguous["message"].casefold()
    assert weather["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" not in weather["message"].casefold()
    assert "FIRST-PRIVATE-MATCH" not in json.dumps([ambiguous, weather, seen], ensure_ascii=False)
    assert "SECOND-PRIVATE-MATCH" not in json.dumps([ambiguous, weather, seen], ensure_ascii=False)

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    ambiguous_row = next(
        row for row in rows if row.get("role") == "user" and row.get("content") == "Что по ORION-77?"
    )
    ambiguous_metadata = json.loads(str(ambiguous_row.get("metadata_json") or "{}"))
    assert "conversation_attachment_raw_ids" not in ambiguous_metadata


@pytest.mark.asyncio
async def test_two_current_files_satisfy_both_without_adding_a_prior_file(
    settings,
    storage,
    monkeypatch,
):
    prior = _pending_file(storage, "alice", "alice", "PRIOR-MUST-STAY-OUT", filename="prior.txt")
    current_one = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-ONE",
        filename="current-one.txt",
    )
    current_two = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-TWO",
        filename="current-two.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", prior, "prior")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        "Сравни оба файла",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[_current_attachment(current_one), _current_attachment(current_two)],
        enable_tools=False,
    )

    expected_ids = [current_one.id, current_two.id]
    assert len(seen) == 1
    assert [item["raw_object_id"] for item in seen[0][1]] == expected_ids
    assert result["restored_attachment_count"] == 0
    assert result["attachment_context_expected_count"] == 2
    assert "PRIOR-MUST-STAY-OUT" not in json.dumps(seen, ensure_ascii=False)
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(
        row for row in rows if row.get("role") == "user" and row.get("content") == "Сравни оба файла"
    )
    request_metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert request_metadata["conversation_attachment_raw_ids"] == expected_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("with_unrelated_catalog", [False, True])
async def test_package_json_without_an_exact_private_match_is_an_ordinary_question(
    settings,
    storage,
    monkeypatch,
    with_unrelated_catalog,
):
    conversation = storage.create_conversation("alice")
    if with_unrelated_catalog:
        unrelated = _pending_file(
            storage,
            "alice",
            "alice",
            "PRIVATE-UNRELATED-FILE",
            filename="private-notes.txt",
        )
        _record_upload(storage, conversation["id"], "alice", unrelated, "unrelated")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        "Как устроен package.json?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert len(seen) == 1 and seen[0][1] == []
    assert result["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" not in result["message"].casefold()
    assert "PRIVATE-UNRELATED-FILE" not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "filenames"),
    [
        ("Что в файле missing.xyz?", ["alpha-plan.txt", "beta-budget.txt", "latest.txt"]),
        ("Что в файле report.txt?", ["report.txt", "report.txt", "latest.txt"]),
        ("Что в пятом файле?", ["one.txt", "two.txt", "latest.txt"]),
        ("Что в файле №99?", ["one.txt", "two.txt", "latest.txt"]),
        ("Что в файле voice.ogg?", ["voice.ogg", "report.pdf", "latest.txt"]),
    ],
)
async def test_unknown_or_duplicate_filename_never_falls_back_to_latest_file(
    settings,
    storage,
    monkeypatch,
    query,
    filenames,
):
    first = _pending_file(storage, "alice", "alice", "FIRST-PRIVATE", filename=filenames[0])
    second = _pending_file(storage, "alice", "alice", "SECOND-PRIVATE", filename=filenames[1])
    latest = _pending_file(
        storage,
        "alice",
        "alice",
        "LATEST-MUST-NEVER-REACH-GENERATION",
        filename=filenames[2],
    )
    conversation = storage.create_conversation("alice")
    for raw in (first, second, latest):
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    generated: list[list[dict]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def forbidden_generate(context, message, attachments):  # noqa: ANN001
        del context, message
        generated.append([dict(item) for item in (attachments or [])])
        raise AssertionError("an unresolved filename reached response generation")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", forbidden_generate)

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert generated == []
    assert result["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" in result["message"].casefold()
    assert "LATEST-MUST-NEVER-REACH-GENERATION" not in json.dumps(result, ensure_ascii=False)
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    request_metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert "conversation_attachment_raw_ids" not in request_metadata


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
