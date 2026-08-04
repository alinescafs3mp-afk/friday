"""Слияние переживает отказ, а событие не теряет свою дату.

Две находки ревью уязвимых участков 2026-08-04, обе подтверждены замером на
изолированном стенде до правки.

ОТКАЗ ПОВЕРХ СЛИЯНИЯ. У пары в состоянии «слито» вызов с «не дубликат» менял
состояние на отказ, при том что сущности в графе уже слиты. Дальше пара не
всплывёт нигде — она решена, — а записанное решение противоречит тому, что
произошло. Хуже всего дорога: `entity_merge_decide(decision='reject')` НЕ требует
подтверждения человеком, в отличие от accept, то есть переписать состоявшееся
слияние могла сама модель.

Возврат в очередь остаётся разрешён: это откат слияния, у него своя дорога и свой
смысл. Разрешён и обратный ход «отказал, потом передумал и слил» — там человек
действует осознанно.

ВРЕМЯ СОБЫТИЯ. Слияние переносило алиасы, ссылки на документы, связи и
кандидатуры — и не трогало `entity_time`. Строка оставалась на слитой сущности,
которую читатель ленты уже не видит, и у события просто пропадала дата:
«Совещание 12 августа», слитое с дубликатом, переставало напоминать о себе вовсе.
"""

from __future__ import annotations

from friday.storage.models import (
    Entity,
    EntityResolutionCandidate,
    EntityType,
    ResolutionStatus,
    new_id,
)


def _entity(storage, name: str) -> Entity:
    made = Entity(id=new_id("ent"), user_id="alice", name=name, entity_type=EntityType.PERSON)
    storage.create_entity(made)
    return made


def _pair(storage, left: Entity, right: Entity):
    return storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id="alice",
            entity_a_id=left.id,
            entity_b_id=right.id,
            confidence=0.9,
            resolution_method="name_similarity",
            evidence_json={},
        )
    )


def test_a_rejection_does_not_overwrite_a_merge(storage) -> None:
    """Мутация: снять условие — состояние снова переписывается, тест краснеет."""
    storage.ensure_user("alice")
    left, right = _entity(storage, "Петров Пётр"), _entity(storage, "Петров П.П.")
    candidate = _pair(storage, left, right)
    storage.merge_entities("alice", left.id, right.id)

    changed = storage.resolve_candidate(
        candidate.id, ResolutionStatus.REJECTED, "alice", user_id="alice"
    )

    assert changed is False, "отказ переписал состоявшееся слияние"
    assert str(storage.get_resolution_candidate(candidate.id, "alice")["status"]) == "merged"


def test_an_undo_can_still_return_the_pair_to_the_queue(storage) -> None:
    """Обратная сторона: откат слияния обязан возвращать пару в очередь.

    Слишком широкий запрет («из merged не выйти никуда») сделал бы откат
    невозможным, а он существует и работает.
    """
    storage.ensure_user("alice")
    left, right = _entity(storage, "Иванов И."), _entity(storage, "Иванов Иван")
    candidate = _pair(storage, left, right)
    storage.merge_entities("alice", left.id, right.id)

    returned = storage.resolve_candidate(
        candidate.id, ResolutionStatus.SUGGESTED, "alice", user_id="alice"
    )

    assert returned is True
    assert str(storage.get_resolution_candidate(candidate.id, "alice")["status"]) == "suggested"


def test_a_rejected_pair_can_still_be_merged_later(storage) -> None:
    """«Отказал, потом передумал и слил» — законный ход человека."""
    storage.ensure_user("alice")
    left, right = _entity(storage, "Сидоров С."), _entity(storage, "Сидоров Семён")
    candidate = _pair(storage, left, right)
    storage.resolve_candidate(candidate.id, ResolutionStatus.REJECTED, "alice", user_id="alice")

    merged = storage.resolve_candidate(
        candidate.id, ResolutionStatus.MERGED, "alice", user_id="alice"
    )

    assert merged is True


def test_a_merge_carries_the_event_time_to_the_target(storage) -> None:
    """Мутация: убрать перенос — дата события снова остаётся на мёртвом узле."""
    storage.ensure_user("alice")
    target, source = _entity(storage, "Совещание"), _entity(storage, "Совещание по поверке")
    storage.set_entity_time(source.id, "alice", "2026-08-12")

    merged = storage.merge_entities("alice", source.id, target.id)

    rows = [
        dict(row)
        for row in storage.execute(
            "SELECT entity_id, occurred_at FROM entity_time WHERE user_id='alice'"
        ).fetchall()
    ]
    assert rows == [{"entity_id": merged["id"], "occurred_at": "2026-08-12"}], (
        "время события не переехало на цель слияния"
    )


def test_the_targets_own_time_wins(storage) -> None:
    """Дата цели не затирается: это тот узел, который человек оставил.

    Обратная сторона переноса, и она важнее самого переноса: молча заменить
    подтверждённую дату на дату дубликата хуже, чем потерять дату дубликата.
    """
    storage.ensure_user("alice")
    target, source = _entity(storage, "Совещание"), _entity(storage, "Совещание (копия)")
    storage.set_entity_time(target.id, "alice", "2026-08-12")
    storage.set_entity_time(source.id, "alice", "2026-09-30")

    merged = storage.merge_entities("alice", source.id, target.id)

    kept = storage.execute(
        "SELECT occurred_at FROM entity_time WHERE user_id='alice' AND entity_id=?",
        (merged["id"],),
    ).fetchone()
    assert kept["occurred_at"] == "2026-08-12", "дата цели затёрта датой дубликата"


def test_the_transfer_is_recorded_for_the_undo(storage) -> None:
    """Перенос попадает в запись истории — иначе откат не сможет его вернуть."""
    import json

    storage.ensure_user("alice")
    target, source = _entity(storage, "Приёмка"), _entity(storage, "Приёмка работ")
    storage.set_entity_time(source.id, "alice", "2026-08-20")

    merged = storage.merge_entities("alice", source.id, target.id)

    row = storage.execute(
        "SELECT transfer_json FROM entity_merge_history WHERE id=?", (merged["_merge_id"],)
    ).fetchone()
    transfer = json.loads(str(row["transfer_json"] or "{}"))
    assert transfer.get("time_moved"), "перенос времени не записан в историю слияния"
