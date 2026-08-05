"""Одна и та же заметка, предложенная дважды, — одна карточка во входящих.

ЗАМЕРЕНО 2026-08-04. Каждый мутирующий инструмент ядра позвали дважды подряд с
теми же аргументами и посчитали дельту строк по всем таблицам. `memory_save` и
`entity_create` добавляли по Raw Object и по карточке КАЖДЫЙ раз; `entity_link`,
`remind` и откат слияния — ничего.

Причина оказалась одна и та же у обоих: в конвейер передавался
`source_ref = new_id("toolref")`, свежий на каждый вызов. Поиск повтора по ключу
происхождения не совпадал сам с собой НИКОГДА, ветка `idempotent_replay` была
недостижима — при том, что естественный ключ (`content_hash`) тут же вычислялся и
ложился в ту же строку.

Ровно эта ошибка в этом же конвейере уже была найдена и вылечена для пересланных
документов: у Telegram ключ происхождения содержит `update_id`, и второй раз
пересланный файл давал два Raw Object, две карточки и два одинаковых знания.
Лечение там — запасной поиск по содержимому (`find_file_by_content_hash`), и здесь
сделан его брат.

Повторяет вызовы сама модель: увидев отказ, она пробует ещё раз, и это измерено
многократно. Текстом подсказки такое не чинится — только структурой.
"""

from __future__ import annotations

import json

import pytest

from friday.ingestion._candidates import AGENT_CANDIDATE_REPEAT_WINDOW_SEC


@pytest.fixture
def kernel_for(settings, storage):
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService
    from friday.web_surfer import WebSurfer

    def build(user_id: str = "alice", preset: str = "owner"):
        storage.ensure_user(user_id, preset_key=preset)
        auth = AuthorizationService(storage)
        graph = KnowledgeGraph(storage)
        kernel = ExecutionKernel(auth, settings)
        kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
        return kernel, auth.actor_for_user(user_id, source="test")

    return build


def _cards(storage, user_id: str = "alice") -> int:
    return storage.execute("SELECT COUNT(*) AS n FROM inbox WHERE user_id=?", (user_id,)).fetchone()["n"]


@pytest.mark.asyncio
async def test_the_same_note_twice_makes_one_card(storage, kernel_for) -> None:
    """Мутация: убрать запасной поиск по содержимому — снова две карточки."""
    kernel, actor = kernel_for()
    call = {"content": "Поверка манометра назначена на март", "title": "Поверка"}

    first = await kernel.execute("memory_save", dict(call), actor=actor)
    second = await kernel.execute("memory_save", dict(call), actor=actor)

    assert first.success and second.success, second.error
    assert _cards(storage) == 1, "во входящих две одинаковые карточки"
    assert second.data["idempotent_replay"] is True
    assert second.data["inbox_id"] == first.data["inbox_id"]


@pytest.mark.asyncio
async def test_the_repeat_says_nothing_was_saved(storage, kernel_for) -> None:
    """Ответ на повторе обязан говорить, что новой записи НЕ появилось.

    Иначе дедупликация чинит дубли и на их месте заводит ложное подтверждение:
    модель читает словарь, неотличимый от успеха, и отчитывается «сохранила».
    """
    kernel, actor = kernel_for()
    call = {"content": "Встречу перенесли на пятницу"}

    await kernel.execute("memory_save", dict(call), actor=actor)
    again = await kernel.execute("memory_save", dict(call), actor=actor)

    assert "не создано" in str(again.data.get("reason") or ""), "повтор выглядит как сохранение"


@pytest.mark.asyncio
async def test_an_entity_proposal_dedupes_too(storage, kernel_for) -> None:
    """Отдельная проверка, а не повтор предыдущей.

    У `entity_create` в метаданных НЕ БЫЛО поля `requested_by`, а поиск повтора
    сверяет именно автора. Ключ, дословно перенесённый с заметок, не сработал бы
    здесь ни разу — и замер показал бы «одно починено, другое нет».
    """
    kernel, actor = kernel_for()
    call = {"name": "Манометр МП-100", "entity_type": "other", "description": "поверочный"}

    first = await kernel.execute("entity_create", dict(call), actor=actor)
    second = await kernel.execute("entity_create", dict(call), actor=actor)

    assert first.success and second.success, second.error
    assert _cards(storage) == 1
    assert second.data["idempotent_replay"] is True


@pytest.mark.asyncio
async def test_a_note_and_an_entity_are_different_proposals(storage, kernel_for) -> None:
    """Заметка и сущность делят один `source`, и без вида в ключе склеились бы.

    Совпасть по содержимому они могут: текст предложения сущности человек вправе
    сохранить и заметкой. Тогда одна из двух просьб исчезла бы молча.
    """
    kernel, actor = kernel_for()
    shared = "Предложение сущности: Манометр МП-100\nТип: other"

    await kernel.execute("memory_save", {"content": shared}, actor=actor)
    await kernel.execute("entity_create", {"name": "Манометр МП-100", "entity_type": "other"}, actor=actor)

    assert _cards(storage) == 2, "предложение сущности заглушено заметкой с тем же текстом"


@pytest.mark.asyncio
async def test_a_settled_card_does_not_block_a_new_one(storage, kernel_for) -> None:
    """Разобранная карточка — история, и ответ «уже лежит» стал бы неправдой."""
    kernel, actor = kernel_for()
    call = {"content": "Заявку подписали"}

    first = await kernel.execute("memory_save", dict(call), actor=actor)
    with storage.transaction() as conn:
        conn.execute("UPDATE inbox SET status='classified' WHERE id=?", (first.data["inbox_id"],))

    again = await kernel.execute("memory_save", dict(call), actor=actor)

    assert again.data["idempotent_replay"] is False
    assert _cards(storage) == 2


@pytest.mark.asyncio
async def test_the_same_words_two_weeks_later_are_a_new_note(storage, kernel_for) -> None:
    """Через две недели человек вправе сказать то же самое СНОВА.

    Замеренный дефект — повтор в одном ходу. Без окна свежести ключ становится
    вечным, и его длину задавал бы не замысел, а то, насколько человек запустил
    очередь разбора: «встречу перенесли» второй раз просто пропало бы.
    """
    kernel, actor = kernel_for()
    call = {"content": "Встречу с подрядчиком перенесли"}

    first = await kernel.execute("memory_save", dict(call), actor=actor)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET received_at='2026-07-01T00:00:00+00:00' WHERE id=?",
            (first.data["raw_object_id"],),
        )

    again = await kernel.execute("memory_save", dict(call), actor=actor)

    assert again.data["idempotent_replay"] is False, "старая карточка глушит новую просьбу"
    assert _cards(storage) == 2
    assert AGENT_CANDIDATE_REPEAT_WINDOW_SEC > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "call"),
    [
        ("memory_save", {"content": "Поверка назначена на март"}),
        ("entity_create", {"name": "Манометр МП-100", "entity_type": "other"}),
    ],
)
async def test_in_a_shared_archive_each_person_gets_their_card(settings, storage, tool, call) -> None:
    """В общем архиве `user_id` один на всех, и людей различает только автор.

    Без него первая же заметка одного участника глушила бы такую же заметку
    другого, а разбирающий видел бы одну карточку с чужим авторством.

    Проверяются ОБА инструмента, и это не педантизм. Пустое поле автора
    сравнивается с пустым и совпадает, поэтому инструмент, который автора не
    пишет, дедуплицируется как ни в чём не бывало — ровно до того момента, когда
    архив становится общим. Мутация «убрать автора у предложения сущности»
    переживала проверку, где общий архив был только на дороге заметок.
    """
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import ActorContext, AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("tenant", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))

    def actor_of(person: str) -> ActorContext:
        return ActorContext(
            user_id="tenant",
            preset_key="owner",
            source="test",
            shared_tenant=True,
            person_id=person,
            identity_id=f"identity-of-{person}",
        )

    await kernel.execute(tool, dict(call), actor=actor_of("person-a"))
    await kernel.execute(tool, dict(call), actor=actor_of("person-b"))

    assert _cards(storage, "tenant") == 2, "предложение участника заглушено предложением соседа"
    authors = {
        json.loads(row["metadata_json"] or "{}").get("requested_by")
        for row in storage.execute("SELECT metadata_json FROM raw_objects WHERE user_id='tenant'").fetchall()
    }
    assert authors == {"person-a", "person-b"}
