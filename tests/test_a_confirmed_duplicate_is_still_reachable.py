"""«Подтвердить» на дубликате означало «спрятать навсегда».

Список конфликтов в админке запрашивался с ЖЁСТКО ЗАШИТЫМ `status=suggested`, а
селектора статуса не было. Подтверждённый конфликт после этого не показывался нигде:
ни в списке, ни на дашборде, ни в дайджесте — а команд для конфликтов в CLI нет вовсе.
Разрешить его технически по-прежнему можно (`confirmed` → `resolved` разрешён), но
УЗНАТЬ его идентификатор было неоткуда.

На живой базе владельца в очереди 207 предложенных дубликатов при схожести 0.95–1.00,
затронуто 294 объекта — 19% архива. Цена ошибки «нажал не туда» здесь высокая.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path("jericho/admin_ui/static/app.js").read_text(encoding="utf-8")


def test_the_conflict_list_is_not_pinned_to_one_status():
    """Зашитый статус — это и был дефект."""
    assert "conflicts?user_id=${q(uid)}&status=suggested" not in SOURCE, (
        "статус конфликтов снова зашит: подтверждённые исчезнут из админки"
    )
    assert "state.conflictStatus" in SOURCE


def test_every_status_the_api_knows_is_offered():
    """Иначе получится новая ловушка: часть решений уводит запись в невидимое."""
    block = SOURCE[SOURCE.index("Противоречия и дубликаты") : SOURCE.index("Противоречия и дубликаты") + 600]
    for status in ("suggested", "confirmed", "dismissed", "resolved"):
        assert status in block, f"статус {status} нельзя выбрать в интерфейсе"


def test_switching_status_resets_the_page_offset():
    """Иначе человек переключит фильтр и попадёт на четвёртую страницу пустого набора."""
    # По СТРОКЕ, а не до первой точки с запятой: она есть и внутри тела действия.
    match = re.search(r"^actions\.filterConflictStatus=.*$", SOURCE, re.M)
    assert match, "переключателя статуса нет"
    assert "conflictsOffset=0" in match.group(0)


def test_the_review_endpoint_still_accepts_the_transition(settings, storage):
    """Проверка ФАКТА, на котором стоит правка: подтверждённый конфликт разрешим.

    Если бы он был терминальным, показывать его было бы бессмысленно — и правку
    следовало бы делать другую.
    """
    import inspect

    from jericho.storage._knowledge import KnowledgeMixin

    source = inspect.getsource(KnowledgeMixin.review_knowledge_conflict)
    assert "confirmed" in source, "подтверждённый конфликт больше не разрешим — перечитайте правку"


# --- кластеры дубликатов ------------------------------------------------------


def _pair(storage, user_id: str, first: str, second: str) -> str:
    """Конфликт «почти дубликат» между двумя записями."""
    conflict = storage.store_knowledge_conflict(
        user_id,
        knowledge_a_id=first,
        knowledge_b_id=second,
        conflict_type="near_duplicate",
        confidence=0.97,
        evidence={"cosine": 0.97},
    )
    return str(conflict["id"])


def _knowledge(storage, user_id: str, index: int) -> str:
    import hashlib

    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    text = f"Документ {index} про поставку оборудования. " * 5
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="t",
        source_ref=new_id("s"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_a_side_already_superseded_cannot_be_named_the_winner(storage):
    """Замерено: 207 пар складываются в 126 кластеров — 19 троек, 7 четвёрок, 3 пятёрки.

    То есть больше половины пар решаются не поодиночке, и после первого решения одна
    из сторон соседней пары уже погашена. Объявить её «оставить» значило бы назначить
    главной запись, которая сама указывает на другую.
    """
    import pytest as _pytest

    storage.ensure_user("alice")
    a, b, c = (_knowledge(storage, "alice", index) for index in range(3))
    first = _pair(storage, "alice", a, b)
    second = _pair(storage, "alice", b, c)

    storage.resolve_conflict("alice", first, a, reviewed_by="alice")

    with _pytest.raises(ValueError, match="deprecated"):
        storage.resolve_conflict("alice", second, b, reviewed_by="alice")


def test_the_surviving_side_can_still_win_its_other_pairs(storage):
    """Иначе проверка сделала бы кластеры неразрешимыми вовсе."""
    storage.ensure_user("alice")
    a, b, c = (_knowledge(storage, "alice", index) for index in range(3))
    first = _pair(storage, "alice", a, b)
    second = _pair(storage, "alice", a, c)

    storage.resolve_conflict("alice", first, a, reviewed_by="alice")
    resolved = storage.resolve_conflict("alice", second, a, reviewed_by="alice")

    assert resolved is not None


def test_the_listing_says_which_side_is_already_gone(storage):
    """Админка физически не могла это показать: стадия в проекцию не входила."""
    storage.ensure_user("alice")
    a, b, c = (_knowledge(storage, "alice", index) for index in range(3))
    first = _pair(storage, "alice", a, b)
    _pair(storage, "alice", b, c)
    storage.resolve_conflict("alice", first, a, reviewed_by="alice")

    remaining = [
        row
        for row in storage.list_knowledge_conflicts("alice", status="suggested")
        if str(row["knowledge_a_id"]) == b or str(row["knowledge_b_id"]) == b
    ]
    assert remaining, "вторая пара кластера пропала из списка"
    row = remaining[0]
    side = "a" if str(row["knowledge_a_id"]) == b else "b"
    assert row[f"knowledge_{side}_stage"] == "deprecated"
    assert row[f"knowledge_{side}_superseded_by"] == a


def test_every_conflict_query_returns_the_same_shape(storage):
    """Проекция была в ТРЁХ копиях, и они разошлись при первой же правке."""
    storage.ensure_user("alice")
    a, b = (_knowledge(storage, "alice", index) for index in range(2))
    conflict_id = _pair(storage, "alice", a, b)

    by_id = storage.get_knowledge_conflict("alice", conflict_id)
    listed = storage.list_knowledge_conflicts("alice", status="suggested")[0]
    assert set(by_id) == set(listed)
    for field in ("knowledge_a_stage", "knowledge_b_superseded_by"):
        assert field in by_id
