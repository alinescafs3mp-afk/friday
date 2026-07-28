"""Потолок пар означал «остальное выбросили», и знал об этом только лог.

Замер: на 1000 сущностей с общими словами потолок в 200 000 пар срабатывает уже
на первом проходе, на 2000 полный проход занимает 137 с при таймауте воркера
240 с. То есть список предложений УЖЕ был частичным — а отличить его от «сливать
больше нечего» было нельзя: маршрут возвращал короткий список без единого
признака обрыва, WARNING оставался в логе, которого рецензент не читает.

Теперь потолок значит «продолжим в следующий раз». Здесь проверяется:

1. Тик, упёршийся в бюджет, ГОВОРИТ об этом в ответе, а не в логе.
2. Следующий тик продолжает с того места, а не начинает сначала.
3. Несколько тиков в сумме дают ровно то же, что один полный проход, — иначе
   инкрементальность купила бы себе пропущенные пары.
4. Обход завершается: «полный проход закончен» — это отдельный факт, и только в
   этот момент «дубликатов нет» означает то, что написано.
"""

from __future__ import annotations

from jericho.storage.models import Entity, EntityType, new_id


def _entities(storage, user_id: str, count: int) -> None:
    """Имена с общими словами: именно на таком корпусе потолок и срабатывает."""
    storage.ensure_user(user_id)
    words = ("отдел", "склад", "проект", "смета", "закупка", "участок")
    for index in range(count):
        name = f"{words[index % len(words)]} {words[(index // 6) % len(words)]} {index}"
        storage.create_entity(
            Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type=EntityType.CONCEPT)
        )


def test_a_tick_that_hits_its_budget_says_so_in_the_answer(storage):
    _entities(storage, "alice", 60)
    _, report = storage.sweep_entity_duplicates("alice", max_pairs=5)

    assert report["partial"] is True
    assert report["complete"] is False
    assert report["keys_pending"] > 0, "обход упёрся в бюджет, но не сказал, сколько осталось"
    assert report["stopped_at"] is not None


def test_the_next_tick_continues_instead_of_starting_over(storage):
    _entities(storage, "alice", 60)
    _, first = storage.sweep_entity_duplicates("alice", max_pairs=5)
    _, second = storage.sweep_entity_duplicates("alice", max_pairs=5)

    assert first["partial"] and second["resumed"] is True
    assert second["keys_pending"] < first["keys_pending"], (
        "второй тик не сдвинулся — курсор не сохраняется, и обход не закончится никогда"
    )


def test_the_sweep_terminates_and_announces_the_completed_pass(storage):
    _entities(storage, "alice", 40)
    completed = 0
    for _ in range(200):
        _, report = storage.sweep_entity_duplicates("alice", max_pairs=20)
        if report["complete"]:
            completed = report["sweeps"]
            break
    assert completed == 1, "обход не дошёл до конца за 200 тиков"

    # После завершения курсор сброшен: следующий тик начинает новый проход.
    _, fresh = storage.sweep_entity_duplicates("alice", max_pairs=20)
    assert fresh["resumed"] is False


def test_ticks_together_see_everything_one_full_pass_sees(storage):
    """Инкрементальность не должна покупать себе пропущенные пары.

    Оракул здесь — сам полный проход: он и есть определение «всё». Сравниваются
    множества предложенных пар, а не их порядок.
    """
    _entities(storage, "alice", 50)

    whole = {item.pair_key for item in storage.find_duplicate_candidates("alice", min_confidence=0.4)}

    in_pieces: set[str] = set()
    for _ in range(200):
        candidates, report = storage.sweep_entity_duplicates("alice", min_confidence=0.4, max_pairs=15)
        in_pieces.update(item.pair_key for item in candidates)
        if report["complete"]:
            break

    assert whole, "оракул пуст — корпус не порождает ни одной пары, тест ничего не проверяет"
    assert in_pieces == whole, (
        f"обход по кускам потерял {len(whole - in_pieces)} пар и выдумал {len(in_pieces - whole)}"
    )


def test_a_small_graph_completes_in_one_tick(storage):
    _entities(storage, "alice", 6)
    _, report = storage.sweep_entity_duplicates("alice")
    assert report["complete"] is True
    assert report["keys_pending"] == 0
    assert report["sweeps"] == 1


def test_a_corrupt_cursor_restarts_instead_of_failing_the_tick(storage):
    _entities(storage, "alice", 10)
    storage.kv_set("entity_dedup:cursor:alice", "{не json")
    _, report = storage.sweep_entity_duplicates("alice")
    assert report["resumed"] is False
    assert report["complete"] is True


def test_the_route_reports_the_state_of_the_walk(settings):
    """Пустой список предложений при незакрытом обходе — это «ещё не смотрели»."""
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=owner).json()["items"][0]["id"]
        _entities(storage, user_id, 20)

        response = client.post("/api/admin/resolutions/detect", json={"user_id": user_id}, headers=owner)
        assert response.status_code == 200, response.text
        payload = response.json()
        for field in ("entities", "keys_total", "keys_pending", "partial", "complete", "pending_total"):
            assert field in payload, f"отчёт обхода не несёт {field}"
        assert payload["entities"] == 20
