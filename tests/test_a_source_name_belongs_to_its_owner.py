"""Имя внешнего источника принадлежит своему владельцу, а не всей системе.

Схема 28 сделала первичным ключом одно имя. Читается источник всегда парой
`name + user_id` — значит второй человек, объявив источник с уже занятым именем,
делал UPDATE ЧУЖОЙ строки: владелец в ней оставался прежний, а `dsn_env`
становился новый. Чужой источник начинал читать базу соседа.

Проверяется не «есть ли составной ключ», а последствие: после объявления
одноимённого источника у соседа первый читает ровно то, что объявлял сам.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from friday.storage import SCHEMA_VERSION, FridayStorage
from friday.storage._base import DATA_SOURCES_SCHEMA


def _declare(storage: FridayStorage, user_id: str, dsn_env: str) -> dict:
    return storage.register_data_source(
        user_id,
        name="hr",
        kind="postgres",
        dsn_env=dsn_env,
        description=f"источник {user_id}",
        created_by=user_id,
    )


def test_the_same_name_in_two_accounts_stays_two_sources(storage: FridayStorage) -> None:
    storage.ensure_user("alice", source="test", external_id="alice")
    storage.ensure_user("bob", source="test", external_id="bob")

    _declare(storage, "alice", "ALICE_HR_DSN")
    _declare(storage, "bob", "BOB_HR_DSN")

    alice = storage.get_data_source("alice", "hr")
    bob = storage.get_data_source("bob", "hr")
    assert alice is not None, "объявление соседа не должно уносить чужой источник"
    assert bob is not None
    # Суть дефекта: не «пропал», а стал смотреть в ЧУЖУЮ переменную окружения.
    assert alice["dsn_env"] == "ALICE_HR_DSN"
    assert bob["dsn_env"] == "BOB_HR_DSN"
    assert [row["name"] for row in storage.list_data_sources("alice")] == ["hr"]
    assert [row["name"] for row in storage.list_data_sources("bob")] == ["hr"]


def test_forgetting_your_source_leaves_the_neighbour_theirs(storage: FridayStorage) -> None:
    storage.ensure_user("alice", source="test", external_id="alice")
    storage.ensure_user("bob", source="test", external_id="bob")
    _declare(storage, "alice", "ALICE_HR_DSN")
    _declare(storage, "bob", "BOB_HR_DSN")

    assert storage.forget_data_source("bob", "hr") is True
    assert storage.get_data_source("bob", "hr") is None
    assert storage.get_data_source("alice", "hr") is not None


def test_redeclaring_your_own_source_updates_it(storage: FridayStorage) -> None:
    """Своё имя переобъявить можно — это правка, а не второй источник."""

    storage.ensure_user("alice", source="test", external_id="alice")
    _declare(storage, "alice", "ALICE_HR_DSN")
    updated = _declare(storage, "alice", "ALICE_HR_REPLICA_DSN")

    assert updated["dsn_env"] == "ALICE_HR_REPLICA_DSN"
    assert len(storage.list_data_sources("alice")) == 1


def test_a_database_written_by_schema_28_is_rebuilt_with_its_rows(settings, tmp_path) -> None:
    """Живая база уже носит схему 28: миграция обязана перенести строки."""

    # Настоящая база схемы 28, а не собранная руками пара таблиц: миграция
    # трогает её целиком, и сокращённый стенд соврал бы про соседние шаги.
    archive = Path(__file__).parent / "fixtures" / "schemas" / "schema-28.sqlite3.gz"
    path = tmp_path / "pre29.sqlite3"
    with gzip.open(archive, "rb") as packed, open(path, "wb") as raw:
        shutil.copyfileobj(packed, raw)
    conn = sqlite3.connect(path)
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO data_sources(name, user_id, kind, dsn_env, created_at) VALUES(?, ?, ?, ?, ?)",
        ("hr", owner, "postgres", "ALICE_HR_DSN", "2026-08-05T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    storage = FridayStorage(replace(settings, database_path=path))
    try:
        survived = storage.get_data_source(owner, "hr")
        assert survived is not None, "миграция потеряла объявленный источник"
        assert survived["dsn_env"] == "ALICE_HR_DSN"
        keys = sorted(
            (item[5], item[1])
            for item in storage.execute("PRAGMA table_info(data_sources)").fetchall()
            if item[5]
        )
        assert [name for _, name in keys] == ["user_id", "name"]
        # И перестроенная таблица действительно пускает тёзку соседа.
        storage.ensure_user("bob", source="test", external_id="bob")
        _declare(storage, "bob", "BOB_HR_DSN")
        assert storage.get_data_source(owner, "hr")["dsn_env"] == "ALICE_HR_DSN"
    finally:
        storage.close()


def test_the_rebuild_does_not_run_twice(settings, tmp_path) -> None:
    """Второе открытие уже перестроенной базы не должно её трогать."""

    path = tmp_path / "twice.sqlite3"
    first = FridayStorage(replace(settings, database_path=path))
    first.ensure_user("alice", source="test", external_id="alice")
    _declare(first, "alice", "ALICE_HR_DSN")
    first.close()

    second = FridayStorage(replace(settings, database_path=path))
    try:
        assert second.get_data_source("alice", "hr") is not None
        leftovers = second.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'data_sources_pre%'"
        ).fetchall()
        assert leftovers == [], "временная таблица миграции осталась в базе"
    finally:
        second.close()


def test_the_two_copies_of_the_definition_agree() -> None:
    """Определение таблицы живёт в двух местах — они обязаны совпадать.

    Константу читает миграция, копию внутри `CORE_SCHEMA` — создание с нуля.
    Разойдись они, и база, созданная сегодня, отличалась бы от перестроенной, а
    тесты (где база всегда с нуля) разницы не увидели бы. Тот же класс, что
    «правка, не доехавшая до места».
    """

    from friday.storage._base import CORE_SCHEMA, MISSION_TASKS_SCHEMA

    def _body(text: str, table: str) -> str:
        marker = f"CREATE TABLE IF NOT EXISTS {table} ("
        start = text.index(marker)
        end = text.index("\n);", start)
        return " ".join(text[start:end].split())

    for table, constant in (("data_sources", DATA_SOURCES_SCHEMA), ("mission_tasks", MISSION_TASKS_SCHEMA)):
        assert _body(CORE_SCHEMA, table) == _body(constant, table), (
            f"определение {table} разошлось между CORE_SCHEMA и отдельной константой"
        )


def test_the_schema_number_moved_with_the_table() -> None:
    """Перестройка таблицы без нового номера схемы до живой базы не доедет."""

    assert SCHEMA_VERSION >= 29


@pytest.mark.parametrize("name", ["hr", "warehouse"])
def test_a_source_is_only_visible_to_its_owner(storage: FridayStorage, name: str) -> None:
    storage.ensure_user("alice", source="test", external_id="alice")
    storage.ensure_user("bob", source="test", external_id="bob")
    storage.register_data_source("alice", name=name, kind="sqlite", dsn_env="ALICE_DSN", created_by="alice")

    assert storage.get_data_source("bob", name) is None
    assert storage.list_data_sources("bob") == []
    assert storage.forget_data_source("bob", name) is False
    assert storage.get_data_source("alice", name) is not None
