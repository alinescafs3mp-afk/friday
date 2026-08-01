"""Подтверждение опасного действия: одноразовое, привязанное, с честным исходом.

Спека v3 §5. Слой хранилища проверяется отдельно от ядра, потому что все три его
свойства — атомарность заявления, привязка к аргументам и различимость исходов —
это свойства ИМЕННО SQL, и подделать их на уровне выше нельзя.
"""

from __future__ import annotations

import concurrent.futures

import pytest

from friday.storage._approvals import payload_digest


def _approval(storage, **kwargs):
    payload = kwargs.pop("payload", {"candidate_id": "res_1", "decision": "accept"})
    return storage.create_action_approval(
        "alice",
        tool=kwargs.pop("tool", "entity_merge_decide"),
        payload=payload,
        summary=kwargs.pop("summary", "Слить «Иванов И.И.» и «Иванов Иван»"),
        **kwargs,
    )


def test_a_new_request_has_not_happened_yet(storage):
    record = _approval(storage)
    assert record["status"] == "pending"
    assert record["decided_by"] is None and record["claimed_at"] is None
    # Аргументы хранятся в каноническом виде и вместе со своим отпечатком.
    assert record["payload"] == {"candidate_id": "res_1", "decision": "accept"}
    assert record["payload_hash"] == payload_digest(record["payload"])
    assert record["expires_at"] > record["created_at"]


def test_only_an_approved_request_can_be_claimed(storage):
    record = _approval(storage)
    assert storage.claim_action_approval(record["id"], "alice") is None, (
        "неподтверждённое действие удалось забрать под исполнение"
    )
    storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")
    claimed = storage.claim_action_approval(record["id"], "alice")
    assert claimed is not None and claimed["status"] == "claimed"


def test_a_rejected_request_stays_rejected(storage):
    record = _approval(storage)
    storage.decide_action_approval(record["id"], "alice", decision="reject", decided_by="alice")
    assert storage.claim_action_approval(record["id"], "alice") is None
    # Второе решение по уже решённой заявке не проходит: решают один раз.
    assert (
        storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")
        is None
    )
    assert storage.get_action_approval(record["id"], "alice")["status"] == "rejected"


def test_a_claim_happens_exactly_once_under_concurrency(storage):
    """Мутация: заменить атомарный UPDATE на «прочитать, проверить, записать».

    Два исполнителя, одно решение. Побочный эффект должен случиться один раз —
    иначе подтверждение «слить A и B» становится подтверждением «слить дважды».
    """
    record = _approval(storage)
    storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: storage.claim_action_approval(record["id"], "alice"), range(8))
        )

    winners = [item for item in results if item is not None]
    assert len(winners) == 1, f"подтверждение заявлено {len(winners)} раз вместо одного"


def test_substituted_arguments_do_not_pass_the_claim(storage):
    """Человек согласился на КОНКРЕТНОЕ действие, а не на действие вообще.

    Мутация: убрать сверку хэша в `claim_action_approval` — тест краснеет.
    """
    record = _approval(storage, payload={"candidate_id": "res_1", "decision": "accept"})
    storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")

    substituted = storage.claim_action_approval(
        record["id"], "alice", payload={"candidate_id": "res_999", "decision": "accept"}
    )
    assert substituted is None, "подтверждение сработало для другого набора аргументов"
    # И заявка при этом НЕ сгорела: честный вызов с теми же аргументами проходит.
    honest = storage.claim_action_approval(
        record["id"], "alice", payload={"candidate_id": "res_1", "decision": "accept"}
    )
    assert honest is not None


def test_key_order_does_not_change_the_binding(storage):
    """Тот же смысл — тот же отпечаток; иначе привязка ломалась бы на ровном месте."""
    record = _approval(storage, payload={"decision": "accept", "candidate_id": "res_1"})
    storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")
    claimed = storage.claim_action_approval(
        record["id"], "alice", payload={"candidate_id": "res_1", "decision": "accept"}
    )
    assert claimed is not None


def test_a_changed_policy_invalidates_the_decision(storage):
    record = _approval(storage, policy_epoch="epoch-1")
    storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")
    assert storage.claim_action_approval(record["id"], "alice", policy_epoch="epoch-2") is None
    assert storage.claim_action_approval(record["id"], "alice", policy_epoch="epoch-1") is not None


def test_an_expired_request_cannot_be_decided_or_claimed(storage):
    record = _approval(storage, ttl_sec=1)
    storage.execute(
        "UPDATE action_approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (record["id"],),
    )
    storage.commit()
    assert (
        storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")
        is None
    ), "решение принято по просроченной заявке"
    assert storage.expire_action_approvals() >= 1
    assert storage.get_action_approval(record["id"], "alice")["status"] == "expired"


def test_an_interrupted_execution_is_uncertain_and_not_replayed(storage):
    """Спека v3 §5: неизвестный исход не повторяют автоматически.

    Процесс умер между заявлением и записью результата. Побочный эффект мог уже
    случиться — значит запись уходит в `uncertain` и ждёт сверки человеком, а
    повторное заявление невозможно.

    Мутация: вернуть такие записи в `approved` (то есть разрешить повтор) — тест
    краснеет.
    """
    record = _approval(storage)
    storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")
    storage.claim_action_approval(record["id"], "alice")
    storage.execute(
        "UPDATE action_approvals SET claimed_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (record["id"],),
    )
    storage.commit()

    assert storage.reconcile_stale_claims() == 1
    after = storage.get_action_approval(record["id"], "alice")
    assert after["status"] == "uncertain"
    assert "неизвест" in after["error"]
    assert storage.claim_action_approval(record["id"], "alice") is None, (
        "неизвестный исход можно заявить повторно — побочный эффект случился бы дважды"
    )


def test_a_finished_action_records_its_outcome(storage):
    record = _approval(storage)
    storage.decide_action_approval(record["id"], "alice", decision="approve", decided_by="alice")
    storage.claim_action_approval(record["id"], "alice")
    done = storage.finish_action_approval(
        record["id"], "alice", success=True, result={"status": "merged"}
    )
    assert done["status"] == "done" and done["result"] == {"status": "merged"}
    # Повторное завершение не проходит: заявка уже не «исполняется».
    assert storage.finish_action_approval(record["id"], "alice", success=True) is None


def test_approvals_do_not_cross_tenants(storage):
    record = _approval(storage)
    storage.ensure_user("bob")
    assert storage.get_action_approval(record["id"], "bob") is None
    assert (
        storage.decide_action_approval(record["id"], "bob", decision="approve", decided_by="bob") is None
    )
    assert storage.count_action_approvals("bob") == 0
    assert storage.count_action_approvals("alice", status="pending") == 1


def test_a_bad_risk_class_is_refused(storage):
    with pytest.raises(ValueError):
        storage.create_action_approval("alice", tool="entity_merge_decide", risk="whatever")


def test_the_hygiene_worker_is_actually_registered(settings, storage):
    """Механизм, который никто не зовёт, работает только в тестах.

    `expire_action_approvals` и `reconcile_stale_claims` были написаны вместе с
    подтверждениями — и не вызывались НИОТКУДА. Просроченное согласие продолжало бы
    числиться действующим, а исполнение, прерванное смертью процесса, навсегда
    осталось бы в статусе «исполняется»: человек не узнал бы, что про его действие
    никто ничего не знает.

    Проверяется РЕГИСТРАЦИЯ в супервизоре, а не сам метод: зелёный тест на метод
    ничего не говорит о том, что продакшен его зовёт.

    Мутация: убрать `supervisor.register("approval_hygiene", ...)` — тест краснеет.
    """
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.workers import WorkersManager

    graph = KnowledgeGraph(storage)
    workers = WorkersManager(settings, storage, IngestionPipeline(settings, storage, graph), graph)
    workers.register_all()
    # `snapshot()` показывает состояние ВЫПОЛНЕНИЯ и до первого тика пуст, поэтому
    # смотрим сам список зарегистрированных задач.
    names = {task.name for task in workers.supervisor._tasks}  # noqa: SLF001
    assert "approval_hygiene" in names, (
        "гигиена подтверждений не зарегистрирована — просроченное и оборванное висит вечно"
    )
