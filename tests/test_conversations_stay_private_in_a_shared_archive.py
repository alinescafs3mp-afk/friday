"""Общими владелец сделал документы и записи — не разговоры.

Общий архив (`FRIDAY_SHARED_ARCHIVE`) устроен подменой арендатора: `user_id` у
всех участников один, человека называет `own_id`. Переписку это затрагивать не
должно — и в чате она осталась личной, потому что привязка каналов чинилась
отдельно.

Тотальный аудит нашёл ту же ошибку в инструменте `message_search`: он искал по
`actor.user_id`, то есть по ОБЩЕМУ арендатору. Любой участник мог получить чужие
реплики, набрав слово из них, — при том что докстрока инструмента обещает «own
chat history». Владелец при этом прямо просил по одному из чатов режим полной
приватности.

Рядом — две родственные: заявка на подтверждение уходила в чат владельца архива
вместо автора, а «кто принял решение» записывалось арендатором, одинаковым у
всех.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService
from friday.web_surfer import WebSurfer

SECRET = "перегородки в переговорной"


@pytest.fixture
def shared(settings, storage):
    from dataclasses import replace

    tuned = replace(settings, shared_archive=True)
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    storage.ensure_user("telegram:test:5002", source="telegram", preset_key="owner")
    auth = AuthorizationService(storage, shared_tenant=LEGACY_OWNER_USER_ID)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, tuned)
    core.bind_services(storage, graph, WebSurfer(tuned), IngestionPipeline(tuned, storage, graph))
    return core, auth, storage


def _say(storage, user_id: str, text: str) -> None:
    conversation = storage.create_conversation(user_id, title="разговор")
    storage.store_message(conversation["id"], user_id, "user", text)


def test_one_persons_words_do_not_surface_in_anothers_search(shared):
    """Мутация: вернуть `actor.user_id` — тест краснеет."""
    core, auth, storage = shared
    _say(storage, LEGACY_OWNER_USER_ID, f"надо заказать {SECRET}")

    stranger = auth.actor_for_user("telegram:test:5002", source="telegram")
    assert stranger.user_id == LEGACY_OWNER_USER_ID, "общий архив не включился — тест бессмыслен"

    result = asyncio.run(core.execute("message_search", {"query": "перегородки"}, actor=stranger))
    rendered = f"{result.to_llm_message()} {result.data}"
    assert SECRET not in rendered, "чужая реплика нашлась по слову из неё"


def test_a_person_still_finds_their_own_words(shared):
    """Инструмент не сломан: свою переписку человек по-прежнему находит."""
    core, auth, storage = shared
    _say(storage, "telegram:test:5002", f"надо заказать {SECRET}")

    person = auth.actor_for_user("telegram:test:5002", source="telegram")
    result = asyncio.run(core.execute("message_search", {"query": "перегородки"}, actor=person))
    assert result.success
    assert result.data.get("results"), "человек перестал находить собственную переписку"


def test_the_approval_notice_goes_to_its_author() -> None:
    """Заявка — тому, кто её вызвал, а не хозяину общего архива."""
    # Комментарии в счёт не идут: там `actor.user_id` объясняет, ПОЧЕМУ его
    # нельзя брать. Смотрим на исполняемые строки.
    code = "\n".join(
        line for line in inspect.getsource(ExecutionKernel._notify_pending_approval).splitlines()
        if not line.strip().startswith("#")
    )
    assert "actor.user_id" not in code, "заявка снова адресуется арендатору"
    assert code.count("actor.own_id") >= 3, "не все три места адресации переведены на человека"


def test_decisions_are_attributed_to_the_person() -> None:
    """«Кто решил» — человек. В общем архиве арендатор одинаков у всех."""
    source = inspect.getsource(ExecutionKernel)
    for field in ("reviewed_by", "resolved_by", "undone_by", '"requested_by"'):
        assert f"{field}=actor.user_id" not in source, f"{field} снова записывает арендатора"
