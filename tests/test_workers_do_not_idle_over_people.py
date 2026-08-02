"""Фоновые работники обрабатывают АРЕНДАТОРОВ, а не людей.

Найдено тотальным аудитом. Одиннадцать фоновых операций — дедуп, разрешение
сущностей, качество знаний, входящие, жизненный цикл, синхронизация хранилища —
перебирают `list_user_ids()`. Пока арендатор и человек были одним и тем же, это
было верно.

С общим архивом (`FRIDAY_SHARED_ARCHIVE`, заказ владельца) материал у всех один.
Замерено на живой машине: пять активных учёток, весь материал под одним
арендатором. Значит четыре прохода из пяти — холостые, а `_knowledge_dedup_all`
вдобавок делил на это число бюджет времени: очередь слияний получала 120 секунд
из 600, впятеро меньше, чем настроено.

Тест держит обе половины: и число проходов, и неурезанный бюджет.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from friday.permissions import LEGACY_OWNER_USER_ID


class _Recorder:
    """Работник без окружения: интересен только перебор арендаторов."""

    def __init__(self, settings, storage):
        self.settings = settings
        self.storage = storage
        self.seen: list[str] = []
        self.budget: float | None = None

    _tenants = None  # заполняется ниже настоящим методом


def _worker(settings, storage):
    from friday.workers import WorkersManager

    recorder = _Recorder(settings, storage)
    # Берём ровно тот метод, что работает в бою, а не его пересказ: проверять
    # надо подключённое, а не механизм рядом.
    recorder._tenants = WorkersManager._tenants.__get__(recorder)  # type: ignore[attr-defined]
    recorder._for_each_user = WorkersManager._for_each_user.__get__(recorder)  # type: ignore[attr-defined]
    return recorder


def _seed_people(storage, count: int) -> None:
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    for index in range(count):
        storage.ensure_user(f"telegram:test:{9000 + index}", source="telegram")


@pytest.mark.asyncio
async def test_the_shared_archive_is_one_tenant_not_five(settings, storage):
    """Мутация: вернуть перебор `list_user_ids` — тест краснеет."""
    _seed_people(storage, 4)
    worker = _worker(replace(settings, shared_archive=True), storage)

    async def operation(user_id: str) -> None:
        worker.seen.append(user_id)

    await worker._for_each_user(operation)
    assert worker.seen == [LEGACY_OWNER_USER_ID], (
        f"фоновая работа прошла {len(worker.seen)} раз(а) вместо одного"
    )


@pytest.mark.asyncio
async def test_without_the_shared_archive_every_person_is_a_tenant(settings, storage):
    """Обычная настройка не меняется: у каждого свой материал."""
    _seed_people(storage, 4)
    worker = _worker(replace(settings, shared_archive=False), storage)

    async def operation(user_id: str) -> None:
        worker.seen.append(user_id)

    await worker._for_each_user(operation)
    assert len(worker.seen) == 5, "перестали обходить людей там, где архив у каждого свой"


@pytest.mark.asyncio
async def test_the_dedup_budget_is_not_divided_by_the_number_of_people(settings, storage):
    """Очередь слияний получает всё настроенное время, а не долю от числа учёток.

    Замерено до правки: 600 с бюджета делились на пять учёток — 120 с реальной
    работы. Разбор очереди слияний идёт ровно столько, сколько ему дали.
    """
    _seed_people(storage, 4)
    tuned = replace(settings, shared_archive=True, dedup_scan_max_seconds=600.0)
    worker = _worker(tuned, storage)

    tenants = max(1, len(await worker._tenants()))
    share = max(1.0, float(tuned.dedup_scan_max_seconds) / tenants)
    assert share == 600.0, f"дедупу досталось {share:.0f} с из 600 — бюджет поделён на людей"


def test_the_method_is_the_one_the_supervisor_uses() -> None:
    """Проверяется подключённое: `_for_each_user` зовёт `_tenants`, а не список людей."""
    import inspect

    from friday.workers import WorkersManager

    source = inspect.getsource(WorkersManager._for_each_user)
    assert "self._tenants()" in source
    assert "list_user_ids" not in source

    dedup = inspect.getsource(WorkersManager._knowledge_dedup_all)
    assert "self._tenants()" in dedup
    assert "list_user_ids" not in dedup
