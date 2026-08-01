"""Модель предлагает — человек решает — служба исполняет (спека v3 §5).

До этого модель со способностью `kg.merge` сливала две сущности ОДНИМ вызовом
инструмента. Право отвечает на вопрос «этому актору вообще можно»; вопрос «человек
видел именно это действие и согласился» не задавался никем. На корпусе владельца
это 4609 сущностей, среди которых люди с похожими ФИО, и ошибка слияния означает
двух разных людей под одним узлом.

Риск живёт в АРГУМЕНТАХ, а не в инструменте: `entity_merge_decide` с
`decision=reject` ничего не меняет (пара уходит из очереди, узлы целы), а с
`accept` — переносит связи и оставляет одну сущность из двух. Поэтому гейт
спрашивает предикат по аргументам, а не имя инструмента.
"""

from __future__ import annotations

import pytest

from jericho.execution_kernel import ExecutionKernel
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.storage.models import Entity, EntityResolutionCandidate, EntityType, new_id
from jericho.web_surfer import WebSurfer


def _kernel(settings, storage):
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    return kernel, auth


def _candidate(storage, user_id: str) -> str:
    left = Entity(id=new_id("ent"), user_id=user_id, name="Иванов И.И.", entity_type=EntityType.PERSON)
    right = Entity(id=new_id("ent"), user_id=user_id, name="Иванов Иван", entity_type=EntityType.PERSON)
    storage.create_entity(left)
    storage.create_entity(right)
    stored = storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id=user_id,
            entity_a_id=left.id,
            entity_b_id=right.id,
            confidence=0.91,
            resolution_method="name_similarity",
            evidence_json={"reason": "похожие имена"},
        )
    )
    return stored.id


@pytest.mark.asyncio
async def test_the_model_cannot_merge_two_people_on_its_own(settings, storage):
    """Мутация: убрать `entity_merge_decide` из HIGH_RISK_TOOLS — тест краснеет."""
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    result = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )

    assert result.success is False, "слияние выполнено без человека"
    assert result.data["status"] == "approval_required"
    approval = storage.get_action_approval(result.data["approval_id"], "alice")
    assert approval["status"] == "pending"
    assert approval["tool"] == "entity_merge_decide"
    # Действие ДЕЙСТВИТЕЛЬНО не произошло: кандидат всё ещё ждёт решения.
    row = storage.get_resolution_candidate(candidate_id, "alice")
    assert str(row["status"]) == "suggested"
    # И модели сказано не повторять — иначе она устроит очередь одинаковых заявок.
    assert "не повторяй" in result.error


@pytest.mark.asyncio
async def test_a_harmless_decision_still_goes_straight_through(settings, storage):
    """`reject` не меняет ни одного узла — спрашивать человека не о чем.

    Обратная сторона гейта: если бы он смотрел на имя инструмента, а не на
    аргументы, каждое «не дубликаты» тоже требовало бы подтверждения, и очередь
    из 45 947 кандидатур стала бы неразбираемой.
    """
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    result = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "reject"}, actor=actor
    )

    assert result.success is True, result.error
    assert result.data["status"] == "rejected"
    assert storage.count_action_approvals("alice") == 0


@pytest.mark.asyncio
async def test_an_approved_action_runs_once_and_only_once(settings, storage):
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]

    # Пока человек не решил — исполнять нечего.
    too_early = await kernel.execute_approved(approval_id, actor=actor)
    assert too_early.success is False
    assert "нельзя использовать" in too_early.error

    storage.decide_action_approval(approval_id, "alice", decision="approve", decided_by="alice")
    done = await kernel.execute_approved(approval_id, actor=actor)
    assert done.success is True, done.error
    assert done.data["status"] == "merged"
    assert storage.get_action_approval(approval_id, "alice")["status"] == "done"

    # Второй раз то же подтверждение не сработает: слияние не должно случиться дважды.
    again = await kernel.execute_approved(approval_id, actor=actor)
    assert again.success is False


@pytest.mark.asyncio
async def test_a_rejected_request_never_executes(settings, storage):
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]
    storage.decide_action_approval(approval_id, "alice", decision="reject", decided_by="alice")

    result = await kernel.execute_approved(approval_id, actor=actor)
    assert result.success is False
    assert str(storage.get_resolution_candidate(candidate_id, "alice")["status"]) == "suggested"


@pytest.mark.asyncio
async def test_substituted_arguments_do_not_execute(settings, storage):
    """Подтверждение годится только для тех аргументов, что показали человеку.

    Мутация: не передавать `payload` в `claim_action_approval` — тест краснеет.
    """
    storage.ensure_user("alice", preset_key="owner")
    first = _candidate(storage, "alice")
    second = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": first, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]
    storage.decide_action_approval(approval_id, "alice", decision="approve", decided_by="alice")

    # Подмена в самой записи: ровно то, чего боится спека — «argument drift».
    storage.execute(
        "UPDATE action_approvals SET payload_json=? WHERE id=?",
        (f'{{"candidate_id":"{second}","decision":"accept"}}', approval_id),
    )
    storage.commit()

    result = await kernel.execute_approved(approval_id, actor=actor)
    assert result.success is False, "исполнены аргументы, которых человек не видел"
    assert str(storage.get_resolution_candidate(second, "alice")["status"]) == "suggested"


@pytest.mark.asyncio
async def test_losing_the_capability_after_the_decision_blocks_execution(settings, storage):
    """Подтверждение не заменяет право: перед эффектом права проверяются заново."""
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]
    storage.decide_action_approval(approval_id, "alice", decision="approve", decided_by="alice")

    storage.set_permission_override("alice", "kg.merge", "deny")
    stripped = auth.actor_for_user("alice", source="test")
    result = await kernel.execute_approved(approval_id, actor=stripped)

    assert result.success is False, "действие выполнено актором, у которого отобрали право"
    assert str(storage.get_resolution_candidate(candidate_id, "alice")["status"]) == "suggested"


@pytest.mark.asyncio
async def test_a_conflict_dismissal_is_free_but_a_verdict_is_not(settings, storage):
    """`dismiss` ничего не трогает; `keep_a` объявляет знание устаревшим."""
    from tests.test_conflict_triage_in_chat import _seed_conflict

    storage.ensure_user("alice", preset_key="owner")
    conflict_id = _seed_conflict(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    verdict = await kernel.execute(
        "conflict_decide", {"conflict_id": conflict_id, "decision": "keep_a"}, actor=actor
    )
    assert verdict.success is False and verdict.data["status"] == "approval_required"

    dismissal = await kernel.execute(
        "conflict_decide", {"conflict_id": conflict_id, "decision": "dismiss"}, actor=actor
    )
    assert dismissal.success is True, dismissal.error


@pytest.mark.asyncio
async def test_a_timed_out_execution_is_uncertain_not_failed(settings, storage):
    """Истёкшее время — неизвестный исход, а не отказ.

    Разница не косметическая: `failed` можно повторить, `uncertain` — нельзя.
    Обработчик мог довести побочный эффект до конца ровно в тот момент, когда его
    перестали ждать, и повтор сделал бы работу дважды.

    Мутация: записывать здесь `finish_action_approval(success=False)` — тест
    краснеет.
    """
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]
    storage.decide_action_approval(approval_id, "alice", decision="approve", decided_by="alice")

    async def _never_returns(**_kwargs):
        import asyncio

        await asyncio.sleep(60)

    kernel._tools["entity_merge_decide"].handler = _never_returns  # noqa: SLF001
    import jericho.execution_kernel as kernel_module

    original = kernel_module.asyncio.timeout

    def _instant_timeout(_seconds):
        return original(0.05)

    kernel_module.asyncio.timeout = _instant_timeout
    try:
        result = await kernel.execute_approved(approval_id, actor=actor)
    finally:
        kernel_module.asyncio.timeout = original

    assert result.success is False
    assert "timed out" in result.error.casefold()
    after = storage.get_action_approval(approval_id, "alice")
    assert after["status"] == "uncertain", (
        f"исход записан как {after['status']!r} — повтор сделал бы слияние второй раз"
    )
    # И повторить его нельзя: неизвестный исход ждёт человека, а не второй попытки.
    retry = await kernel.execute_approved(approval_id, actor=actor)
    assert retry.success is False
    assert storage.get_action_approval(approval_id, "alice")["status"] == "uncertain"


@pytest.mark.asyncio
async def test_a_tool_that_reports_success_without_doing_it_is_not_believed(settings, storage):
    """Спека v3 §5: успешный вызов инструмента не доказывает успех задачи.

    Обработчик подменён на такой, который возвращает бодрое «merged» и не делает
    ничего. До независимой проверки заявка закрылась бы как `done`, человек увидел
    бы «готово», а два узла остались бы разными — и узнал бы он об этом случайно,
    через месяц.

    Проверка читает ХРАНИЛИЩЕ заново, а не ответ обработчика: в этом вся её суть.
    Исход при расхождении — `uncertain`, а не `failed`: обработчик отработал без
    ошибки, значит неизвестно, что именно случилось, и повторять нельзя.

    Мутация: убрать блок POSTCONDITIONS в `execute_approved` — тест краснеет.
    """
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]
    storage.decide_action_approval(approval_id, "alice", decision="approve", decided_by="alice")

    async def _lies(**_kwargs):
        return {"status": "merged", "candidate_id": candidate_id}

    kernel._tools["entity_merge_decide"].handler = _lies  # noqa: SLF001
    result = await kernel.execute_approved(approval_id, actor=actor)

    assert result.success is False, "система поверила инструменту на слово"
    assert "не подтвердился проверкой" in result.error
    after = storage.get_action_approval(approval_id, "alice")
    assert after["status"] == "uncertain", f"исход записан как {after['status']!r}"
    assert "постусловие" in after["error"]
    # И слияния действительно не было.
    assert str(storage.get_resolution_candidate(candidate_id, "alice")["status"]) == "suggested"


@pytest.mark.asyncio
async def test_a_real_merge_passes_the_postcondition(settings, storage):
    """Обратная сторона: настоящее слияние проверку проходит и закрывается как done."""
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]
    storage.decide_action_approval(approval_id, "alice", decision="approve", decided_by="alice")

    result = await kernel.execute_approved(approval_id, actor=actor)
    assert result.success is True, result.error
    assert storage.get_action_approval(approval_id, "alice")["status"] == "done"


@pytest.mark.asyncio
async def test_the_request_names_what_will_be_merged(settings, storage):
    """Человек решает по СМЫСЛУ, а не по идентификатору.

    «Объединить сущности по кандидату res_7f3a…» не говорит ему ничего:
    подтверждать по такой строке — значит подтверждать вслепую, а слепое
    подтверждение хуже отсутствия подтверждения, потому что выглядит как контроль.

    Мутация: вернуть идентификатор вместо имён — тест краснеет.
    """
    storage.ensure_user("alice", preset_key="owner")
    candidate_id = _candidate(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    summary = storage.get_action_approval(requested.data["approval_id"], "alice")["summary"]

    assert "Иванов И.И." in summary and "Иванов Иван" in summary, (
        f"заявка не называет, что именно сольют: {summary!r}"
    )
    assert candidate_id not in summary, "человеку показан идентификатор вместо имён"
    # Уверенность — вторая половина решения: 0.91 и 0.55 требуют разного внимания.
    assert "0.91" in summary
    # И сказано, что действие обратимо: это меняет цену ошибки.
    assert "/merges" in summary


@pytest.mark.asyncio
async def test_a_conflict_request_names_both_records(settings, storage):
    """Вердикт объявляет одну запись устаревшей — обе должны быть названы."""
    from tests.test_conflict_triage_in_chat import _seed_conflict

    storage.ensure_user("alice", preset_key="owner")
    conflict_id = _seed_conflict(storage, "alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "conflict_decide", {"conflict_id": conflict_id, "decision": "keep_a"}, actor=actor
    )
    summary = storage.get_action_approval(requested.data["approval_id"], "alice")["summary"]

    assert "устаревшей" in summary, f"не сказано, что произойдёт с проигравшей записью: {summary!r}"
    assert conflict_id not in summary
