"""Инструмент не должен упираться в свой предел и обрезаться посреди структуры.

Замерено на живом архиве 2026-08-02 (1533 документа, 76 входящих, 540 сообщений
за сутки): три инструмента отдавали модели по 11–12 тысяч знаков при пределе
12 000 — то есть обрезались молча, теряя хвост списка, а `to_llm_message` резал
JSON посреди структуры.

    inbox_list      11 936 → 6 069
    list_tags       11 075 → 2 310
    what_happened   11 939 → 9 756

Внутри лежало то, что модели не нужно: у входящих — `suggestions_json` со всей
внутренней кухней (`enrichment_version`, `policy_version`, штрафы и сигналы
оценки), у ленты — время в двух форматах, внутренний `conversation_id` и
заголовок разговора, ДОСЛОВНО повторяющий текст первого сообщения.

Цена была двойная: контекст модели вытеснялся служебными полями (это время
ответа, на которое жаловался владелец) и часть данных не доходила вовсе.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from friday.execution_kernel import ExecutionKernel, _inbox_row_for_llm, _timeline_event_for_llm
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import InboxItem, KnowledgeObject, RawObject, new_id
from friday.web_surfer import WebSurfer

#: Предел, на котором `to_llm_message` начинает резать. Упереться в него — значит
#: молча потерять хвост.
LIMIT = 12_000


@pytest.fixture
def kernel(settings, storage):
    storage.ensure_user("boss", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, settings)
    core.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    return core, auth.actor_for_user("boss", source="test"), storage


def _seed_knowledge(storage, index: int) -> None:
    raw = RawObject(
        id=new_id("raw"),
        user_id="boss",
        source="test",
        source_ref=new_id("src"),
        raw_content="тело " * 200,
        content_type="text",
        content_hash=hashlib.sha256(new_id("h").encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="boss",
            raw_object_id=raw.id,
            content="содержимое " * 200,
            content_type="text",
            title=f"Документ {index}",
            summary="сводка " * 40,
            tags_json=[f"метка{index}", f"тема{index % 7}", "общая"],
        )
    )


def test_the_tag_list_stays_within_the_budget(kernel) -> None:
    """Мутация: вернуть полный список меток — тест краснеет."""
    core, actor, storage = kernel
    for index in range(400):
        _seed_knowledge(storage, index)

    result = asyncio.run(core.execute("list_tags", {}, actor=actor))
    rendered = result.to_llm_message()
    assert len(rendered) < LIMIT, f"список меток занял {len(rendered)} знаков и обрезался"
    # И честно говорит, что показал не всё.
    assert result.data["total"] >= result.data["count"]
    if result.data["truncated"]:
        assert result.data["total"] > result.data["count"]


def test_an_inbox_row_carries_no_internal_machinery() -> None:
    """Мутация: отдать строку как есть — тест краснеет."""
    row = {
        "id": "inbox_1",
        "status": "pending",
        "title": "Рапорт",
        "preview": "текст " * 100,
        "suggested_tags_json": json.dumps(["рапорт", "командиру"]),
        "suggestions_json": json.dumps(
            {
                "importance": 0.22,
                "knowledge_kind": "note",
                "metadata": {
                    "enrichment_version": "moderate-v6",
                    "promotion_assessment": {
                        "policy_version": "moderate-v6",
                        "penalties": ["very_short", "low_context"],
                        "signals": {"a": 1, "b": 2},
                    },
                },
            }
        ),
        "raw_object_id": "raw_1",
        "user_id": "boss",
        "created_at": "2026-08-02T18:17:12+00:00",
    }
    trimmed = _inbox_row_for_llm(row)

    encoded = json.dumps(trimmed, ensure_ascii=False)
    for noise in ("enrichment_version", "policy_version", "penalties", "raw_object_id", "user_id"):
        assert noise not in encoded, f"внутреннее поле {noise} всё ещё уходит модели"
    assert trimmed["tags"] == ["рапорт", "командиру"]
    assert trimmed["importance"] == 0.22
    assert len(trimmed["preview"]) <= 180


def test_a_broken_suggestions_blob_does_not_break_the_row() -> None:
    """Кривой JSON в поле — не повод потерять всю строку входящих."""
    trimmed = _inbox_row_for_llm({"id": "inbox_2", "suggestions_json": "{сломано", "title": "Есть"})
    assert trimmed["id"] == "inbox_2"
    assert trimmed["title"] == "Есть"


def test_a_timeline_event_drops_the_duplicated_title() -> None:
    """Заголовок разговора, дословно равный тексту, — это удвоение."""
    event = {
        "kind": "message",
        "at": "2026-07-31T21:04:04+00:00",
        "at_local": "2026-08-01 00:04",
        "role": "user",
        "text": "Обобщи, какие основные темы есть в моей базе знаний.",
        "conversation": "Обобщи, какие основные темы есть в моей базе знаний.",
        "conversation_id": "conv_7365144f3a604ad5",
    }
    trimmed = _timeline_event_for_llm(event)

    assert "conversation" not in trimmed, "заголовок дублирует текст и всё ещё передаётся"
    assert "conversation_id" not in trimmed, "внутренний идентификатор уходит модели"
    assert trimmed["at"] == "2026-08-01 00:04", "осталось UTC вместо местного времени"


def test_a_different_conversation_title_is_kept() -> None:
    """Если заголовок добавляет смысл — он остаётся."""
    trimmed = _timeline_event_for_llm(
        {"kind": "message", "at_local": "2026-08-01 00:04", "text": "а сроки?", "conversation": "Поверка"}
    )
    assert trimmed["conversation"] == "Поверка"


def test_the_inbox_list_stays_within_the_budget(kernel) -> None:
    core, actor, storage = kernel
    for index in range(40):
        raw = RawObject(
            id=new_id("raw"),
            user_id="boss",
            source="telegram",
            source_ref=new_id("src"),
            raw_content="длинный текст материала " * 60,
            content_type="text",
            content_hash=hashlib.sha256(new_id("h").encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id="boss",
                raw_object_id=raw.id,
                suggested_tags_json=["рапорт", "командиру", "срочно"],
                suggestions_json={
                    "importance": 0.4,
                    "knowledge_kind": "note",
                    "title": f"Материал {index}",
                    "summary": "длинная сводка материала " * 20,
                    "metadata": {
                        "enrichment_version": "moderate-v6",
                        "promotion_assessment": {"policy_version": "moderate-v6", "penalties": ["short"]},
                    },
                },
            )
        )

    result = asyncio.run(core.execute("inbox_list", {"status": "pending"}, actor=actor))
    rendered = result.to_llm_message()
    assert len(rendered) < LIMIT, f"входящие заняли {len(rendered)} знаков и обрезались"
    assert result.data["total"] >= result.data["count"]
