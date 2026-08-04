"""Связь не принимается, если её конца больше нет.

Живость концов проверялась при СОЗДАНИИ кандидата, но между предложением и решением
человека проходит время — за него сущность успевают удалить или слить. Воспроизведено
на стенде: конец удалён, кандидат принят, ребро в никуда создано.

Отменить это нельзя: решение по кандидату терминально (`accepted`/`rejected` — конец
пути), и человек, нажавший «принять», остаётся с ребром, которое ведёт в удалённый
узел. На корпусе владельца очередь предложений измеряется тысячами, а слияния идут
пачками — то есть окно между «предложено» и «решено» не редкость, а норма.

Чинится с двух сторон, потому что дороги две. Очередь больше не показывает пары, у
которых конец мёртв: принять то, чего нельзя принять, человеку не предлагают. А
прямое принятие (по идентификатору, мимо списка) отвечает внятным отказом — иначе
защита держалась бы на том, что человек ходит только через список.
"""

from __future__ import annotations

import pytest

from friday.storage.models import Entity, EntityType, new_id


def _pair(storage, user_id: str = "alice") -> tuple[str, str, str]:
    storage.ensure_user(user_id)
    left = Entity(id=new_id("ent"), user_id=user_id, name="Иванов", entity_type=EntityType.PERSON)
    right = Entity(id=new_id("ent"), user_id=user_id, name="ООО Ромашка", entity_type=EntityType.ORGANIZATION)
    storage.create_entity(left)
    storage.create_entity(right)
    candidate = storage.store_relation_candidate(
        user_id, left.id, right.id, "member_of", confidence=0.9, evidence={"reason": "из документа"}
    )
    return left.id, right.id, candidate["id"]


@pytest.mark.parametrize("dead_side", ["начало", "конец"])
def test_accepting_a_relation_onto_a_deleted_entity_is_refused(storage, dead_side):
    """Мутация: убрать проверку живости в `review_relation_candidate` — тест краснеет.

    Проверяется не только отказ, но и ОТСУТСТВИЕ ребра: молчаливый пропуск с
    зелёным ответом был бы тем же дефектом под другим соусом.

    Стороны перебираются обе. Найдено мутацией: проверка одного лишь начала
    оставляла тест зелёным, потому что в нём удалялось всегда начало — а связь
    одинаково мертва с любого конца.
    """
    left_id, right_id, candidate_id = _pair(storage)
    storage.soft_delete_entity(left_id if dead_side == "начало" else right_id, "alice")

    with pytest.raises(ValueError, match="больше не существует"):
        storage.review_relation_candidate("alice", candidate_id, "accepted", reviewed_by="alice")

    rows = storage.execute("SELECT COUNT(*) FROM relations WHERE user_id='alice'").fetchone()
    assert rows[0] == 0, "ребро в никуда всё-таки создано"


def test_a_pair_with_a_dead_end_leaves_the_queue(storage):
    """Человеку не предлагают решать то, что решить нельзя."""
    left_id, _right_id, _candidate_id = _pair(storage)
    assert storage.list_relation_candidates("alice", status="suggested"), "пара не попала в очередь"

    storage.soft_delete_entity(left_id, "alice")
    assert not storage.list_relation_candidates("alice", status="suggested"), (
        "пара с удалённым концом всё ещё ждёт решения человека"
    )


def test_the_counter_and_the_list_agree(storage):
    """Счётчик и выборка считают одно и то же множество.

    Условие живости стоит в общем фрагменте JOIN именно поэтому: разъехавшись, они
    дали бы «17 предложений» над списком из пятнадцати — и человек искал бы
    пропавшие две.
    """
    left_id, _right_id, _candidate_id = _pair(storage)
    _pair(storage)
    storage.soft_delete_entity(left_id, "alice")

    listed = storage.list_relation_candidates("alice", status="suggested")
    counted = storage.count_relation_candidates("alice", status="suggested")
    assert counted == len(listed) == 1, f"счётчик {counted}, в списке {len(listed)}"


def test_a_living_pair_is_still_accepted(storage):
    """Ошибка в другую сторону: обычная пара обязана приниматься как прежде."""
    _left_id, _right_id, candidate_id = _pair(storage)
    result = storage.review_relation_candidate("alice", candidate_id, "accepted", reviewed_by="alice")
    assert result and result["status"] == "accepted"
    rows = storage.execute("SELECT COUNT(*) FROM relations WHERE user_id='alice'").fetchone()
    assert rows[0] == 1, "принятая связь не создалась"


def test_rejecting_a_stale_pair_still_works(storage):
    """Отклонить можно и то, чего уже нет: это не создаёт ничего.

    Запрет на обе стороны сразу оставил бы такую пару неразрешимой навсегда — а
    отклонение как раз и есть способ закрыть её честно.
    """
    left_id, _right_id, candidate_id = _pair(storage)
    storage.soft_delete_entity(left_id, "alice")
    result = storage.review_relation_candidate("alice", candidate_id, "rejected", reviewed_by="alice")
    assert result and result["status"] == "rejected"
