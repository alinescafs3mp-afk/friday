"""Неизвестный исход выясняется НАБЛЮДЕНИЕМ, а не остаётся висеть навсегда.

Спека v3 §5: «Uncertain side effects require reconciliation, not automatic
replay», и отдельно — человек видит, «что осталось неизвестным и как это
исправить».

Что было сделано раньше: заявка, чьё исполнение оборвалось (смерть процесса
между заявлением и записью результата, либо истёкшее время), уходила в
`uncertain`. `reconcile_stale_claims` честно писала в комментарии «ждут сверки
человеком» — и на этом всё заканчивалось. Сверки не было: заявка висела
неизвестностью до конца времён.

Для ШАГОВ МИССИИ сверка существовала (`ExecutiveService._reconcile_uncertain`) и
работала через `POSTCONDITIONS` — проверки, читающие факт из хранилища. Разница
между двумя путями только в том, откуда берутся аргументы: у шага миссии из
чекпойнта, у заявки из `payload_json`. Проверка одна и та же.

Повтора здесь нет и быть не может: наблюдение УСТАНАВЛИВАЕТ исход, а не
производит его.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _uncertain(storage, *, tool: str, payload: dict, user: str = "alice") -> str:
    """Заявка, доведённая до неизвестного исхода тем же путём, что в бою."""
    approval = storage.create_action_approval(
        user,
        tool=tool,
        payload=payload,
        risk="high",
        summary=f"Опасное действие {tool}",
        requested_by=user,
    )
    approval_id = str(approval["id"])
    assert storage.decide_action_approval(approval_id, user, decision="approve", decided_by=user)
    assert storage.claim_action_approval(approval_id, user)
    assert storage.mark_action_approval_uncertain(approval_id, user, error="исход неизвестен")
    return approval_id


def test_an_effect_that_did_happen_is_closed_as_done(storage) -> None:
    """Наблюдение сказало «случилось» — заявка перестаёт быть неизвестностью."""
    storage.ensure_user("alice", preset_key="admin")
    approval_id = _uncertain(storage, tool="code_run", payload={"code": "print(1)"})

    settled = storage.settle_uncertain_approval(
        approval_id, "alice", happened=True, detail="объект найден в хранилище"
    )

    assert settled and settled["status"] == "done"
    assert "сверено наблюдением" in str(settled["error"])


def test_an_effect_that_did_not_happen_is_not_reopened(storage) -> None:
    """`failed`, а не `pending`: решение человека уже потрачено.

    Узнать, что эффекта не было, — не то же самое, что получить право повторить.
    Новое действие требует нового решения.
    """
    storage.ensure_user("alice", preset_key="admin")
    approval_id = _uncertain(storage, tool="code_run", payload={"code": "print(1)"})

    settled = storage.settle_uncertain_approval(
        approval_id, "alice", happened=False, detail="в хранилище ничего не изменилось"
    )

    assert settled and settled["status"] == "failed"
    assert storage.approval_is_terminal(settled)


def test_only_an_uncertain_approval_can_be_settled(storage) -> None:
    """Сверка, запоздавшая на такт, не переписывает уже установленный исход."""
    storage.ensure_user("alice", preset_key="admin")
    approval = storage.create_action_approval(
        "alice", tool="code_run", payload={"code": "1"}, risk="high", summary="s", requested_by="alice"
    )
    approval_id = str(approval["id"])
    storage.decide_action_approval(approval_id, "alice", decision="approve", decided_by="alice")
    storage.claim_action_approval(approval_id, "alice")
    storage.finish_action_approval(approval_id, "alice", success=True, result={"ok": True})

    assert (
        storage.settle_uncertain_approval(approval_id, "alice", happened=False, detail="поздно")
        is None
    ), "сверка переписала исход, который уже известен точнее"
    assert storage.get_action_approval(approval_id, "alice")["status"] == "done"


def test_another_tenant_cannot_settle_it(storage) -> None:
    """Чужой исход закрывает чужой арендатор — этого быть не должно."""
    storage.ensure_user("alice", preset_key="admin")
    storage.ensure_user("bob", preset_key="admin")
    approval_id = _uncertain(storage, tool="code_run", payload={"code": "1"})

    assert storage.settle_uncertain_approval(approval_id, "bob", happened=True, detail="x") is None
    assert storage.get_action_approval(approval_id, "alice")["status"] == "uncertain"


def test_the_worker_settles_what_it_can_observe(settings, storage) -> None:
    """Мутация: убрать вызов сверки из гигиены — заявка снова висит навсегда.

    Проверяется РАБОТНИК, а не помощник: механизм, который никто не зовёт, работой
    не является.
    """
    from friday.workers import WorkersManager

    storage.ensure_user("alice", preset_key="admin")
    # `entity_merge_decide` — инструмент с постусловием: оно читает, объединены ли
    # сущности на самом деле. Кандидата нет, значит слияния не было.
    approval_id = _uncertain(
        storage, tool="entity_merge_decide", payload={"candidate_id": "res_missing", "decision": "merge"}
    )
    runner = WorkersManager.__new__(WorkersManager)
    runner.storage = storage

    settled = runner._reconcile_uncertain_approvals()

    assert settled == 1, "работник не выяснил исход, который поддаётся наблюдению"
    record = storage.get_action_approval(approval_id, "alice")
    assert record["status"] == "failed"
    assert "сверено наблюдением" in str(record["error"])


def test_a_tool_without_a_check_stays_uncertain(settings, storage) -> None:
    """Решить за человека, чем кончилось необратимое действие, нельзя.

    У инструмента нет постусловия — значит наблюдать нечего, и заявка честно
    остаётся неизвестностью.
    """
    from friday.workers import WorkersManager

    storage.ensure_user("alice", preset_key="admin")
    approval_id = _uncertain(storage, tool="speak", payload={"text": "привет"})
    runner = WorkersManager.__new__(WorkersManager)
    runner.storage = storage

    assert runner._reconcile_uncertain_approvals() == 0
    assert storage.get_action_approval(approval_id, "alice")["status"] == "uncertain"


def test_the_reconciliation_is_wired_into_the_hygiene() -> None:
    """Проверяется подключённое: сверка стоит в работнике, а не рядом с ним."""
    import inspect

    from friday.workers import WorkersManager

    source = inspect.getsource(WorkersManager._approval_hygiene)
    assert "_reconcile_uncertain_approvals" in source, "сверку заявок никто не зовёт"


def test_the_listing_crosses_tenants_on_purpose(storage) -> None:
    """Фоновый работник ходит по всем арендаторам: своего человека у него нет."""
    storage.ensure_user("alice", preset_key="admin")
    storage.ensure_user("bob", preset_key="admin")
    _uncertain(storage, tool="code_run", payload={"code": "1"}, user="alice")
    _uncertain(storage, tool="code_run", payload={"code": "2"}, user="bob")

    owners = {str(row["user_id"]) for row in storage.list_uncertain_approvals(limit=10)}

    assert owners == {"alice", "bob"}


def test_a_broken_payload_does_not_stop_the_pass(settings, storage) -> None:
    """Одна кривая запись не должна оставлять остальные висеть."""
    from friday.workers import WorkersManager

    storage.ensure_user("alice", preset_key="admin")
    broken = _uncertain(storage, tool="entity_merge_decide", payload={})
    good = _uncertain(
        storage, tool="entity_merge_decide", payload={"candidate_id": "res_x", "decision": "merge"}
    )
    runner = WorkersManager.__new__(WorkersManager)
    runner.storage = storage

    assert runner._reconcile_uncertain_approvals() == 1
    assert storage.get_action_approval(broken, "alice")["status"] == "uncertain"
    assert storage.get_action_approval(good, "alice")["status"] == "failed"


# --- вторая половина спеки: человек видит, что неизвестно и что делать --------


def test_the_guidance_names_what_is_unknown() -> None:
    """«Действий с НЕИЗВЕСТНЫМ исходом: 3» — число, по которому нечего проверить."""
    from friday.telegram_bridge._commands import CommandsMixin

    text = CommandsMixin._uncertain_guidance(
        {
            "total": 2,
            "items": [
                {"summary": "Объединить «Иванов И.И.» и «Иванов Иван»", "error": "исход неизвестен"},
                {"summary": "Выполнить код очистки", "error": ""},
            ],
        }
    )

    assert "Объединить «Иванов И.И.»" in text, "человеку не назвали, что именно проверять"
    assert "Выполнить код очистки" in text
    assert "повтор" in text.lower(), "не сказано, почему нельзя просто повторить"


def test_the_guidance_is_silent_when_nothing_is_unknown() -> None:
    from friday.telegram_bridge._commands import CommandsMixin

    assert CommandsMixin._uncertain_guidance({"total": 0, "items": []}) == ""


def test_a_long_list_says_how_many_are_hidden() -> None:
    """Молчаливый обрез — класс, который на этом проекте ловился четырежды за сутки."""
    from friday.telegram_bridge._commands import CommandsMixin

    text = CommandsMixin._uncertain_guidance(
        {"total": 9, "items": [{"summary": f"Действие {i}"} for i in range(9)]}
    )

    assert "и ещё 4" in text, text


def test_pending_approvals_no_longer_hide_the_unknown() -> None:
    """Мутация: вернуть подсказку внутрь ветки «ничего не ждёт» — тест краснеет.

    Прежняя редакция показывала неизвестные исходы только когда очередь пуста:
    одна ожидающая заявка полностью скрывала то, про что система сама не знает,
    чем кончилось.
    """
    import inspect

    from friday.telegram_bridge._commands import CommandsMixin

    source = inspect.getsource(CommandsMixin._process_update)
    guidance_at = source.index("tail = self._uncertain_guidance(")
    empty_at = source.index("if not items:", guidance_at - 2000)
    assert guidance_at < empty_at, "подсказка снова спрятана в ветку «ничего не ждёт»"
    # И она доезжает до непустого списка тоже.
    assert '"\\n".join(lines) + tail' in source, "к списку заявок подсказка не добавляется"


def test_the_json_of_a_payload_survives_the_round_trip(storage) -> None:
    """Аргументы заявки читаются из `payload_json` — на них и держится сверка."""
    storage.ensure_user("alice", preset_key="admin")
    approval_id = _uncertain(
        storage, tool="entity_merge_decide", payload={"candidate_id": "res_1", "decision": "merge"}
    )

    row = next(r for r in storage.list_uncertain_approvals(limit=10) if r["id"] == approval_id)

    assert json.loads(row["payload_json"])["candidate_id"] == "res_1"
