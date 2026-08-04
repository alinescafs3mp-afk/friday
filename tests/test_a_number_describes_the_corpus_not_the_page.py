"""Показанное и найденное — разные числа, и путать их нельзя.

Один класс, найденный большим ревью 2026-08-04 сразу в пяти местах. Число
считается верно и отвечает на другой вопрос: вместо «сколько есть» получается
«сколько я попросил» или «сколько строк лежит в таблице».

  1. Плитки обзора считали надгробия. Удаление здесь мягкое, и голый COUNT
     показывал удалённое вместе с живым: замерено на живой базе — плитка
     «Знаний 1562» стояла рядом со страницей знаний, где их 1536.
  2. Разбивка «Здоровье графа» по видам считалась питоном по выборке с потолком
     5000, рядом с честным агрегатом «всего».
  3. «Что предстоит» выдавало длину собственной выборки (потолок 100, показ 40)
     за число запланированного.
  4. Группы Inbox обрезались сотней молча, а заголовок «Группы непроверенного
     (N)» считал N по показанным — то есть уменьшался вместе с обрезом.
  5. «Сколько подошло» поиск считает и терял на основной дороге: модель видела
     страницу и говорила о ней как обо всём архиве.

Цена одинаковая во всех пяти: человек принимает решение по числу, которое
выглядит фактом о его данных, а описывает размер запроса. Проверяется здесь
ПОТРЕБИТЕЛЬ — ответ маршрута и то, что уходит модели.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.storage.models import InboxItem, InboxStatus, KnowledgeObject, RawObject, new_id


def _knowledge(storage, user_id: str, text: str, *, deleted: bool = False) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=text[:40],
    )
    storage.store_knowledge_object(knowledge)
    if deleted:
        storage.soft_delete_knowledge_object(knowledge.id, user_id)
    return knowledge.id


@pytest.mark.asyncio
async def test_the_overview_tiles_do_not_count_tombstones(settings, storage):
    """Мутация: убрать `WHERE deleted_at IS NULL` — тест краснеет."""
    from friday.admin_api._overview import overview
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import ActorContext, AuthorizationService

    storage.ensure_user("alice", preset_key="owner")
    _knowledge(storage, "alice", "живая запись раз")
    _knowledge(storage, "alice", "живая запись два")
    _knowledge(storage, "alice", "снесённая запись", deleted=True)
    storage.commit()

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")

    class _Request:
        def __init__(self) -> None:
            self.app = type(
                "App",
                (),
                {
                    "state": type(
                        "S", (), {"storage": storage, "auth_service": auth, "kg": graph, "settings": settings}
                    )()
                },
            )()
            self.state = type("RS", (), {"actor": actor})()

    answer = await overview(_Request())

    assert answer["counts"]["knowledge_objects"] == 2, "надгробие сосчитано как знание"
    assert answer["counts"]["raw_objects"] == 3, "сырьё удалению не подвергалось — счёт не должен меняться"


def test_the_graph_breakdown_is_an_aggregate_not_a_page(storage):
    """Сумма по видам обязана сходиться с «всего» на любом размере корпуса.

    Мутация: вернуть подсчёт питоном по `list_entities(limit=5000)` — на большом
    корпусе разбивка застынет, а «всего» останется честным.
    """
    from friday.knowledge_graph import KnowledgeGraph
    from friday.storage.models import Entity, EntityType

    storage.ensure_user("alice")
    for index in range(7):
        storage.create_entity(
            Entity(
                id=new_id("ent"),
                user_id="alice",
                name=f"Объект {index}",
                entity_type=(EntityType.PERSON if index % 2 else EntityType.PROJECT).value,
            )
        )
    storage.commit()

    stats = KnowledgeGraph(storage).get_stats("alice")

    assert sum(stats["entities_by_type"].values()) == stats["entity_count"], (
        "разбивка и «всего» посчитаны по разным множествам: "
        f"{stats['entities_by_type']} против {stats['entity_count']}"
    )


def test_the_inbox_grouping_says_how_much_it_left_out(storage):
    """Обрез существует, но перестаёт быть молчаливым.

    Мутация: убрать `groups_total` из ответа — потребитель снова не отличит
    «показано всё» от «показана сотня из тысячи».
    """
    storage.ensure_user("alice")
    for index in range(5):
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=new_id("src"),
            raw_content=f"материал {index}",
            content_type="text",
            content_hash=hashlib.sha256(f"m{index}".encode()).hexdigest(),
            metadata_json={"import_source_path": f"/архив/файл{index}.pdf"},
        )
        storage.store_raw_object(raw)
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inb"),
                user_id="alice",
                raw_object_id=raw.id,
                status=InboxStatus.PENDING,
            )
        )
    storage.commit()

    everything = storage.group_pending_inbox("alice", by="extension")
    assert everything["items_total"] == 5
    assert everything["groups_total"] == len(everything["groups"])

    cut = storage.group_pending_inbox("alice", by="directory", max_groups=1)
    assert len(cut["groups"]) == 1
    assert cut["items_total"] == 5, "очередь сжалась вместе с показом"


@pytest.mark.asyncio
async def test_upcoming_reports_the_calendar_not_the_page(settings, storage):
    """«Запланировано N» — свойство календаря, а не размер выборки.

    Мутация: вернуть `"total": len(items)` — тест краснеет.
    """
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import ActorContext, AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    from datetime import date, timedelta

    today = date.today()
    for index in range(45):
        result = await kernel.execute(
            "remind",
            {"what": f"дело {index}", "when": (today + timedelta(days=1)).isoformat()},
            actor=actor,
        )
        assert result.success, result.error

    answer = await kernel.execute("upcoming", {"days": 7}, actor=actor)

    assert answer.success
    assert answer.data["total"] == 45, f"календарь описан длиной страницы: {answer.data['total']}"
    assert answer.data["shown"] == 40, "показ должен остаться ограниченным"
