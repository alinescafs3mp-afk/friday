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
