"""Надзор смотрит СВОИХ людей, а не всех подряд.

Последний незакрытый пункт многопользовательской части. Изоляция сделана,
обращение по имени сделано, смена прав через админку сделана — а надзор видел
ВСЁ: у кого есть право `admin.all_data.read`, тот читал деятельность любого.

Пока участник один, это незаметно. Но владелец просил заводить каждого написавшего
с ПОЛНЫМИ правами — значит право надзора получает каждый, и в сценарии с
несколькими начальниками каждый читал бы чужих подчинённых.

Правило в три строки: хозяин архива видит всех, руководитель — своих подчинённых
(через любое число уровней), остальные — только себя.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.oversight_scope import (
    hierarchy_is_configured,
    may_oversee,
    subordinates_of,
    supervisor_of,
)
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService
from friday.storage.models import RawObject, new_id
from friday.web_surfer import WebSurfer

OWNER = LEGACY_OWNER_USER_ID


def _hire(storage, user_id: str, *, supervisor: str = "", name: str = "") -> None:
    metadata = {"supervisor_id": supervisor} if supervisor else {}
    storage.ensure_user(user_id, preset_key="owner", display_name=name or user_id)
    storage.update_user(user_id, metadata_json=metadata, display_name=name or user_id)


def _arrival_without_author(storage, user_id: str, content: str) -> None:
    storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="test",
            source_ref=new_id("source"),
            raw_content=content,
            content_type="text",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )
    )


@pytest.fixture
def company(storage):
    """Владелец → начальник → двое подчинённых, и посторонний рядом."""
    _hire(storage, OWNER, name="Владелец")
    _hire(storage, "boss", supervisor=OWNER, name="Начальник")
    _hire(storage, "worker_a", supervisor="boss", name="Первый")
    _hire(storage, "worker_b", supervisor="boss", name="Второй")
    _hire(storage, "stranger", name="Посторонний")
    return storage


def test_a_supervisor_sees_their_own_people(company) -> None:
    assert may_oversee(company, "boss", "worker_a", owner_id=OWNER)
    assert may_oversee(company, "boss", "worker_b", owner_id=OWNER)


def test_a_supervisor_does_not_see_strangers(company) -> None:
    """Мутация: снять проверку — тест краснеет, и чужие подчинённые видны всем."""
    assert not may_oversee(company, "boss", "stranger", owner_id=OWNER)


def test_a_worker_sees_only_themselves(company) -> None:
    assert may_oversee(company, "worker_a", "worker_a", owner_id=OWNER)
    assert not may_oversee(company, "worker_a", "worker_b", owner_id=OWNER)
    assert not may_oversee(company, "worker_a", "boss", owner_id=OWNER)


def test_the_owner_sees_everyone(company) -> None:
    for target in ("boss", "worker_a", "worker_b", "stranger"):
        assert may_oversee(company, OWNER, target, owner_id=OWNER), target


def test_the_chain_goes_through_levels(company) -> None:
    """Начальник начальника видит подчинённых через уровень."""
    _hire(company, "chief", name="Директор")
    company.update_user("boss", metadata_json={"supervisor_id": "chief"})
    assert may_oversee(company, "chief", "worker_a", owner_id=OWNER)


def test_a_cycle_in_the_data_does_not_hang_the_check(company) -> None:
    """«А подчинён Б, Б подчинён А» — испорченные данные, но не зависание."""
    company.update_user("boss", metadata_json={"supervisor_id": "worker_a"})
    company.update_user("worker_a", metadata_json={"supervisor_id": "boss"})
    assert may_oversee(company, "boss", "worker_a", owner_id=OWNER) is True
    assert may_oversee(company, "stranger", "worker_a", owner_id=OWNER) is False


def test_nobody_is_their_own_supervisor(company) -> None:
    company.update_user("worker_a", metadata_json={"supervisor_id": "worker_a"})
    assert supervisor_of(company, "worker_a") == ""


def test_the_subordinate_list_matches_the_rule(company) -> None:
    people = set(subordinates_of(company, "boss", owner_id=OWNER))
    assert {"boss", "worker_a", "worker_b"} <= people
    assert "stranger" not in people


def test_without_any_hierarchy_the_rule_stays_asleep(storage) -> None:
    """Мутация: убрать проверку `hierarchy_is_configured` — тест краснеет.

    Введение поля «руководитель» не должно молча выключать работающий надзор:
    пока разметки нет ни у кого, каждый видел бы только себя, и владелец узнал
    бы об этом, когда кто-то не смог сделать привычное. Разметка — действие
    человека, до него поведение прежнее.
    """
    storage.ensure_user("a", preset_key="owner")
    storage.ensure_user("b", preset_key="owner")
    assert hierarchy_is_configured(storage) is False


def test_one_assignment_switches_the_rule_on(company) -> None:
    assert hierarchy_is_configured(company) is True


def test_the_tool_refuses_someone_elses_person(settings, company) -> None:
    """Проверяется подключённое: инструмент надзора, а не только правило."""
    auth = AuthorizationService(company)
    graph = KnowledgeGraph(company)
    core = ExecutionKernel(auth, settings)
    core.bind_services(company, graph, WebSurfer(settings), IngestionPipeline(settings, company, graph))
    actor = auth.actor_for_user("boss", source="test")

    result = asyncio.run(core.execute("user_activity", {"person": "Посторонний"}, actor=actor))

    assert result.data.get("denied") is True, result.data
    assert "подчинённый" in str(result.data.get("reason") or "")
    # И отказ оставляет след: чтение чужого — событие, даже когда не состоялось.
    trail = json.dumps(company.list_audit_log(limit=20), ensure_ascii=False, default=str)
    assert "out_of_scope" in trail


def test_the_tool_still_serves_ones_own_people(settings, company) -> None:
    auth = AuthorizationService(company)
    graph = KnowledgeGraph(company)
    core = ExecutionKernel(auth, settings)
    core.bind_services(company, graph, WebSurfer(settings), IngestionPipeline(settings, company, graph))
    actor = auth.actor_for_user("boss", source="test")

    result = asyncio.run(core.execute("user_activity", {"person": "Первый"}, actor=actor))

    # Форма ответа человеческая: служебные поля (`resolved`, `display_name`,
    # `confidence`) убраны — модель пересказывала их человеку дословно.
    assert not result.data.get("denied"), result.data
    assert result.data.get("человек") == "Первый", result.data


def test_nameless_arrivals_are_counted_in_the_targets_archive(settings, company) -> None:
    """Оговорка о неизвестном авторе относится к подчинённому, не начальнику."""

    _arrival_without_author(company, "boss", "Первый документ начальника")
    _arrival_without_author(company, "boss", "Второй документ начальника")
    assert company.arrivals_without_an_author("boss") == 2
    assert company.arrivals_without_an_author("worker_a") == 0

    auth = AuthorizationService(company)
    graph = KnowledgeGraph(company)
    core = ExecutionKernel(auth, settings)
    core.bind_services(company, graph, WebSurfer(settings), IngestionPipeline(settings, company, graph))
    actor = auth.actor_for_user("boss", source="test")

    result = asyncio.run(core.execute("user_activity", {"person": "Первый"}, actor=actor))

    wording = str(result.data.get("про загруженные материалы") or "")
    assert "все загрузки помечены авторами" in wording, result.data
    assert "2 материалов без отметки" not in wording, result.data
