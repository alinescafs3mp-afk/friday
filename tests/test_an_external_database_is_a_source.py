"""Внешняя база — источник, в который ходят читать, а не хранилище.

Заказ владельца 2026-08-05: «научить Пятницу ходить за данными в какую-то СУБД по
сети — postgres, mysql, или ещё что». Не переезд: свой архив остаётся на месте.

Три свойства, без которых это опасная игрушка, и каждое проверяется здесь:

1. **только чтение**, и проверяется ТЕКСТ запроса, а не намерение того, кто его
   прислал — запрос сюда приходит от модели;
2. **строка подключения не хранится в базе** — резервные копии архива переживают
   всё, а экспорт аккаунта отдаётся человеку целиком;
3. **обрез назван вслух** — «первые двести строк» и «всего двести строк» разные
   факты, и молчаливый обрез читается как второй.
"""

from __future__ import annotations

import sqlite3

import pytest

from friday.data_sources import (
    DataSource,
    UnsafeQueryError,
    assert_read_only,
    describe_source,
    run_query,
)


@pytest.fixture
def external(tmp_path):
    """Чужая база — настоящий файл, а не заглушка."""

    path = tmp_path / "external.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT, unit TEXT);
        CREATE TABLE shifts(id INTEGER PRIMARY KEY, employee_id INTEGER, hours INTEGER);
        INSERT INTO employees(name, unit) VALUES ('Иванов','1 рота'),('Петров','2 рота');
        INSERT INTO shifts(employee_id, hours) VALUES (1, 12), (1, 8), (2, 24);
        """
    )
    connection.commit()
    connection.close()
    return DataSource(name="hr", kind="sqlite", dsn_env="TEST_HR_DSN"), str(path)


@pytest.mark.parametrize(
    "query",
    [
        "delete from employees",
        "update employees set unit='x'",
        "drop table employees",
        "insert into employees(name) values ('x')",
        "select 1; drop table employees",
        "with x as (select 1) delete from employees",
        "attach database '/tmp/other.db' as other",
        "",
    ],
)
def test_writing_is_refused_by_reading_the_query(query):
    """Не доверием, а разбором текста: запрос приходит от модели."""

    with pytest.raises(UnsafeQueryError):
        assert_read_only(query)


def test_a_comment_cannot_hide_a_second_statement():
    """`SELECT 1 -- ; DROP TABLE` — один оператор только после срезания комментария.

    Проверять до срезания значило бы искать точку с запятой там, где сервер её
    уже не увидит, и наоборот.
    """

    assert assert_read_only("select 1 -- ; drop table employees") == "select 1"
    with pytest.raises(UnsafeQueryError):
        assert_read_only("select 1 /* безобидно */ ; drop table employees")


def test_a_plain_read_survives():
    assert assert_read_only("  SELECT unit, count(*) FROM employees GROUP BY unit;  ").startswith("SELECT")
    assert assert_read_only("with t as (select 1) select * from t").startswith("with")


def test_the_external_database_answers_and_stays_intact(external):
    source, dsn = external

    result = run_query(source, dsn, "select unit, count(*) as людей from employees group by unit")

    assert result["columns"] == ["unit", "людей"]
    assert {row["unit"]: row["людей"] for row in result["rows"]} == {"1 рота": 1, "2 рота": 1}
    assert result["truncated"] is False
    assert result["query"].startswith("select"), "запрос возвращается: человек должен видеть, что спросили"


def test_a_cut_result_says_so(external, monkeypatch):
    """Молчаливый обрез читается как «всего столько», а это неправда."""

    import friday.data_sources as module

    monkeypatch.setattr(module, "_MAX_ROWS", 1)
    source, dsn = external

    result = run_query(source, dsn, "select * from shifts")

    assert len(result["rows"]) == 1
    assert result["truncated"] is True
    assert result["row_limit"] == 1


def test_the_schema_is_readable_without_guessing(external):
    source, dsn = external

    described = describe_source(source, dsn)

    assert set(described["tables"]) == {"employees", "shifts"}
    assert [item["column"] for item in described["tables"]["employees"]] == ["id", "name", "unit"]


def test_a_source_name_and_variable_are_checked():
    DataSource(name="hr", kind="sqlite", dsn_env="TEST_HR_DSN").validate()
    with pytest.raises(ValueError):
        DataSource(name="ЛОМ", kind="sqlite", dsn_env="TEST_HR_DSN").validate()
    with pytest.raises(ValueError):
        DataSource(name="hr", kind="oracle", dsn_env="TEST_HR_DSN").validate()
    with pytest.raises(ValueError):
        # Строку подключения на месте имени переменной не примем: именно так
        # пароль и попал бы в базу.
        DataSource(name="hr", kind="sqlite", dsn_env="postgres://user:pass@host/db").validate()


def test_the_registry_keeps_the_variable_name_and_not_the_secret(storage):
    """В базу ложится ИМЯ переменной. Секрета там нет и быть не должно."""

    storage.ensure_user("alice")
    storage.register_data_source(
        "alice", name="hr", kind="sqlite", dsn_env="TEST_HR_DSN", description="кадры"
    )

    stored = storage.get_data_source("alice", "hr")
    assert stored is not None
    assert stored["dsn_env"] == "TEST_HR_DSN"
    dumped = " ".join(str(value) for value in stored.values())
    assert "pass" not in dumped and "://" not in dumped
    assert [row["name"] for row in storage.list_data_sources("alice")] == ["hr"]
    assert storage.forget_data_source("alice", "hr") is True
    assert storage.get_data_source("alice", "hr") is None
