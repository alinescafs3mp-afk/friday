"""Одна и та же просьба подтвердить не спрашивается дважды.

Хвост §14 разбора Codex. Заявка на подтверждение привязана к payload: решение
годится ровно для того набора аргументов, который показали человеку. Но ДВА
одинаковых предложения — тот же человек, тот же инструмент, тот же payload —
заводили две строки и посылали человеку два одинаковых вопроса.

Почему это не косметика. Человек отвечает на первый и видит второй; естественное
прочтение — «система не услышала», и он отвечает снова. Дальше хуже: одна заявка
остаётся неотвеченной до истечения срока и живёт в списке ожидающих, а вторая
исполнена. Список ожидающих перестаёт значить «ждёт решения».

Границы дедупликации, каждая со своей причиной:

* только АКТИВНЫЕ (`pending`). Решённая заявка — уже история; повторить просьбу
  после отказа человек вправе, и это другая просьба;
* только тот же payload. Слияние A+B и слияние A+C — разные действия, и хэш их
  различает;
* только тот же инструмент. У действий без аргументов payload пуст, и хэш пустого
  payload один и тот же — без инструмента в ключе просьбы о разных действиях
  склеились бы по совпадению;
* только тот же человек. В общем архиве двое могут просить одно и то же,
  и ответ одного не является ответом другого.

Каждая граница проверяется своей мутацией. Первая редакция этого файла ловила
три составляющие ключа из шести — инструмент, человека и арендатора не проверяло
ничто, и снять их можно было незаметно.
"""

from __future__ import annotations

import json
import threading

import pytest

from friday.storage.models import Entity, EntityResolutionCandidate, EntityType, new_id


def test_the_same_ask_twice_makes_one_row(storage) -> None:
    """Мутация: убрать поиск активной заявки — снова две строки и два вопроса."""
    storage.ensure_user("alice")
    payload = {"left": "ko_1", "right": "ko_2"}

    first = storage.create_action_approval("alice", tool="kg_merge", payload=payload)
    second = storage.create_action_approval("alice", tool="kg_merge", payload=payload)

    assert second["id"] == first["id"], "одна просьба завела две заявки"
    assert storage.count_action_approvals("alice", status="pending") == 1


def test_two_at_once_still_make_one_row(storage) -> None:
    """Две одновременные просьбы из разных потоков — тоже одна заявка.

    Это отдельная проверка, а не повтор предыдущей. «Прочитать, проверить,
    записать» двумя запросами — классическая гонка: оба потока не находят ничего,
    оба вставляют, и дедупликация, зелёная в один поток, не работает ровно там,
    где заявки и появляются парами — при двух почти одновременных предложениях.

    Держит инвариант не сам поиск, а место, где он стоит: `transaction()` открывает
    `BEGIN IMMEDIATE` под общим замком писателей, так что второй поток входит в
    поиск уже ПОСЛЕ вставки первого. Барьер сводит потоки к вызову одновременно.
    """
    storage.ensure_user("alice")
    payload = {"left": "ko_1", "right": "ko_2"}
    gate = threading.Barrier(2)
    made: dict[int, str] = {}
    broke: list[BaseException] = []

    def ask(slot: int) -> None:
        try:
            gate.wait(timeout=5)
            made[slot] = storage.create_action_approval("alice", tool="kg_merge", payload=payload)["id"]
        except BaseException as exc:  # noqa: BLE001 — падение потока обязано быть видимым
            broke.append(exc)

    threads = [threading.Thread(target=ask, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not broke, f"одновременная просьба уронила поток: {broke!r}"
    assert storage.count_action_approvals("alice", status="pending") == 1, "гонка завела вторую заявку"
    assert made[0] == made[1], "человек получил два уведомления об одном и том же"


def test_a_different_payload_is_a_different_ask(storage) -> None:
    """Слияние A+B и слияние A+C — разные действия.

    Обратная сторона, и она важнее самой дедупликации: склеить их значило бы
    показать человеку одно, а сделать другое.
    """
    storage.ensure_user("alice")

    first = storage.create_action_approval("alice", tool="kg_merge", payload={"left": "a", "right": "b"})
    other = storage.create_action_approval("alice", tool="kg_merge", payload={"left": "a", "right": "c"})

    assert other["id"] != first["id"]
    assert storage.count_action_approvals("alice", status="pending") == 2


def test_another_tenant_asks_for_themselves(storage) -> None:
    """Отдельные арендаторы просят каждый за себя.

    Проверка идёт по БАЗЕ, а не по возвращённому идентификатору, и это не
    придирка. Мутация «убрать `user_id` из ключа» переживала первую редакцию
    теста: поиск находил чужую заявку, `_approval_row` не отдавал её чужому
    арендатору, и `or record` возвращал ФАНТОМ — словарь со свежим `id`, которого
    нет ни в одной строке. Идентификаторы при этом честно различались, и тест был
    зелёным на коде, который заявку не записал вовсе.
    """
    for name in ("alice", "bob"):
        storage.ensure_user(name)
    payload = {"left": "ko_1", "right": "ko_2"}

    mine = storage.create_action_approval("alice", tool="kg_merge", payload=payload)
    theirs = storage.create_action_approval("bob", tool="kg_merge", payload=payload)

    assert theirs["id"] != mine["id"]
    assert storage.get_action_approval(theirs["id"], "bob") is not None, "заявки нет в базе — вернулся фантом"


def test_in_a_shared_archive_the_person_is_the_key(storage) -> None:
    """Ровно тот случай, ради которого `requested_by` стоит в ключе.

    У владельца общий архив включён, то есть `user_id` у участников ОДИН.
    Разделяет их только `requested_by`. Без него первая же просьба одного
    участника глушила бы такую же просьбу другого: второй не увидел бы вопроса
    вовсе, а решение первого исполнило бы действие — но `_approval_row` отдаёт
    заявку только автору, так что второму вернулась бы пустота вместо заявки.
    """
    storage.ensure_user("tenant")
    payload = {"left": "ko_1", "right": "ko_2"}

    mine = storage.create_action_approval("tenant", tool="kg_merge", payload=payload, requested_by="person-a")
    theirs = storage.create_action_approval("tenant", tool="kg_merge", payload=payload, requested_by="person-b")

    assert theirs["id"] != mine["id"], "просьба участника заглушена просьбой соседа"
    assert storage.count_action_approvals("tenant", person_id="person-b") == 1
    assert storage.count_action_approvals("tenant", person_id="person-a") == 1


def test_the_same_payload_for_another_tool_is_another_ask(storage) -> None:
    """Одинаковый payload у разных инструментов — обычное дело, а не редкость.

    У действий без аргументов payload пуст, и хэш пустого payload один и тот же.
    Сними инструмент с ключа — и просьба «удалить материалы» вернула бы висящую
    заявку на слияние сущностей: человек подтвердил бы одно, исполнилось бы
    другое. Это ровно тот вред, ради предотвращения которого заявка вообще
    привязана к payload.
    """
    storage.ensure_user("alice")

    merge = storage.create_action_approval("alice", tool="kg_merge")
    purge = storage.create_action_approval("alice", tool="purge_user_data")

    assert purge["id"] != merge["id"]
    assert purge["tool"] == "purge_user_data", "человеку показали бы не то действие, которое он просил"


def test_a_decided_ask_does_not_block_a_new_one(storage) -> None:
    """Повторить просьбу после отказа человек вправе.

    Решённая заявка — история. Если бы она глушила новую, единственный отказ
    закрывал бы действие навсегда, и починить это можно было бы только руками в
    базе.
    """
    storage.ensure_user("alice")
    payload = {"left": "ko_1", "right": "ko_2"}
    first = storage.create_action_approval("alice", tool="kg_merge", payload=payload)
    # `decision="reject"`, а не выдуманное мной `approved=False`: параметра с
    # таким именем нет, а значение — глагол, не причастие.
    storage.decide_action_approval(first["id"], "alice", decision="reject", decided_by="alice")

    again = storage.create_action_approval("alice", tool="kg_merge", payload=payload)

    assert again["id"] != first["id"]
    assert again["status"] == "pending"


def test_an_expired_ask_does_not_block_a_new_one(storage) -> None:
    """Истёкшая заявка — тоже не ответ.

    Согласие, данное на прошлой неделе, относилось к прошлой картине мира; так же
    и молчание. Заглушить новую просьбу истёкшей значило бы, что действие не
    предложат никогда.
    """
    storage.ensure_user("alice")
    payload = {"left": "ko_1", "right": "ko_2"}
    first = storage.create_action_approval("alice", tool="kg_merge", payload=payload)
    # Срок сдвигается ЯВНО, а не отрицательным `ttl_sec`: `_plus_seconds` берёт
    # `max(1, …)`, поэтому отрицательным сроком просроченную заявку не завести —
    # первая редакция теста этого не знала и проверяла не тот случай.
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE action_approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (first["id"],),
        )

    again = storage.create_action_approval("alice", tool="kg_merge", payload=payload)

    assert again["id"] != first["id"]


@pytest.mark.asyncio
async def test_proposing_the_same_thing_twice_asks_the_person_once(settings, storage) -> None:
    """То, ради чего всё и делается: человек получает ОДИН вопрос, а не два.

    Проверяется не строка в таблице, а то, что доходит до потребителя. Между
    дедупликацией заявки и молчанием чата стоит целая дорога: ядро исполнения
    заводит заявку, потом отдельно ставит уведомление с `dedup_key` вида
    `approval:{id}`. Пока идентификаторы были разными, разными были и ключи, и
    очередь честно уносила человеку два одинаковых вопроса — то есть дедуп строк
    без этой проверки доказывал бы половину.

    Повтор здесь не выдуман: модель, увидев отказ «действие не выполнено»,
    пробует ещё раз — ровно поэтому в тексте отказа стоит «не повторяй вызов».
    Текст этот, как измерено на живой модели не раз, механизмом не является.
    """
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("alice", preset_key="owner")
    storage.update_user("alice", metadata_json=json.dumps({"chat_id": "42"}))
    left = Entity(id=new_id("ent"), user_id="alice", name="Иванов И.И.", entity_type=EntityType.PERSON)
    right = Entity(id=new_id("ent"), user_id="alice", name="Иванов Иван", entity_type=EntityType.PERSON)
    storage.create_entity(left)
    storage.create_entity(right)
    candidate = storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id="alice",
            entity_a_id=left.id,
            entity_b_id=right.id,
            confidence=0.9,
            resolution_method="name_similarity",
            evidence_json={"reason": "похожие имена"},
        )
    ).id

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("alice", source="test")
    call = {"candidate_id": candidate, "decision": "accept"}

    first = await kernel.execute("entity_merge_decide", call, actor=actor)
    second = await kernel.execute("entity_merge_decide", call, actor=actor)

    assert second.data["approval_id"] == first.data["approval_id"], "повтор завёл вторую заявку"
    assert storage.count_action_approvals("alice", status="pending") == 1
    pushed = [row for row in storage.list_pending_notifications(limit=20) if row["kind"] == "approval"]
    assert len(pushed) == 1, f"человеку ушло {len(pushed)} одинаковых вопроса вместо одного"
