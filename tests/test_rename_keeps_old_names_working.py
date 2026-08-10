"""Переименование не должно ломать уже настроенные запуски (ex codename Jericho).

Проект стал Friday (по-русски — Пятница), но у владельца прежние имена стоят в
systemd-юнитах, в `.env.local`, в скриптах и в заголовках, которыми мост
подписывает запросы. «Поменяли имя — перенастраивай всё заново» это не работа, а
перекладывание её на человека, поэтому совместимость здесь — часть контракта, а
не любезность.

Каждая проверка ниже соответствует месту, где отсутствие совместимости уже дало бы
тихую поломку: пустая система вместо архива, молчащий бот, неработающий запуск.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from friday.config import env


def test_a_setting_named_the_old_way_is_still_read(monkeypatch):
    """Мутация: убрать откат на `JERICHO_` в `config.env` — тест краснеет."""
    monkeypatch.delenv("FRIDAY_API_PORT", raising=False)
    monkeypatch.setenv("JERICHO_API_PORT", "9999")
    assert env("FRIDAY_API_PORT") == "9999"


def test_the_new_name_wins_when_both_are_set(monkeypatch):
    """Иначе переезд был бы невозможен: старое имя вечно перебивало бы новое."""
    monkeypatch.setenv("JERICHO_API_PORT", "9999")
    monkeypatch.setenv("FRIDAY_API_PORT", "8000")
    assert env("FRIDAY_API_PORT") == "8000"


def test_an_unset_setting_still_falls_back_to_its_default(monkeypatch):
    monkeypatch.delenv("FRIDAY_API_PORT", raising=False)
    monkeypatch.delenv("JERICHO_API_PORT", raising=False)
    assert env("FRIDAY_API_PORT", "1234") == "1234"


def test_the_legacy_data_directory_is_used_while_it_exists(monkeypatch, tmp_path):
    """Каталог с данными молча не переезжает.

    Там база на гигабайты, файлы-первоисточники и резервные копии. Если бы новый
    путь применялся безусловно, обновление дало бы ПУСТУЮ систему — выглядящую
    исправной, с нулём знаний и без единой ошибки в журнале.

    Мутация: возвращать новый путь всегда — тест краснеет.
    """
    from friday.config import _existing_home

    legacy = tmp_path / ".jericho"
    legacy.mkdir()
    preferred = tmp_path / ".friday"

    assert _existing_home(preferred, legacy) == legacy, "живые данные потеряны при переименовании"

    preferred.mkdir()
    assert _existing_home(preferred, legacy) == preferred, "новый каталог не используется, когда он есть"

    fresh = tmp_path / "fresh"
    assert _existing_home(fresh / ".friday", fresh / ".jericho") == fresh / ".friday", (
        "на чистой машине должен создаваться каталог с новым именем"
    )


def test_the_existing_database_file_is_not_abandoned(tmp_path):
    """Файл базы не переименовывается молча — эту цену уже заплатили.

    Массовая замена задела имя файла, и живой экземпляр создал рядом ПУСТУЮ
    `friday.sqlite3` на 618 КБ при целой `jericho.sqlite3` на 323 МБ. Данные не
    пострадали, но система выглядела исправной и пустой одновременно: диагностика
    зелёная, знаний ноль, эталонов ноль. Хуже потери данных здесь только то, что
    это молча.

    Мутация: возвращать новое имя всегда — тест краснеет.
    """
    from friday.config import _existing_database

    state = tmp_path / "state"
    state.mkdir()
    legacy = state / "jericho.sqlite3"
    legacy.write_bytes(b"")
    assert _existing_database(state) == legacy, "живая база брошена ради пустой новой"

    (state / "friday.sqlite3").write_bytes(b"")
    assert _existing_database(state) == state / "friday.sqlite3"

    legacy.write_bytes(b"legacy-data")
    (state / "friday.sqlite3").write_bytes(b"new-data")
    with pytest.raises(RuntimeError, match="FRIDAY_DATABASE_PATH"):
        _existing_database(state)

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert _existing_database(fresh) == fresh / "friday.sqlite3", (
        "на чистой машине база должна создаваться с новым именем"
    )


def test_explicit_database_path_bypasses_an_ambiguous_automatic_choice(tmp_path, monkeypatch):
    from friday.config import load_settings

    home = tmp_path / "home"
    state = home / "data" / "state"
    state.mkdir(parents=True)
    (state / "friday.sqlite3").write_bytes(b"new-data")
    legacy = state / "jericho.sqlite3"
    legacy.write_bytes(b"legacy-data")
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(legacy))

    assert load_settings().database_path == legacy.resolve()


def test_deployed_database_can_be_required_to_exist_without_breaking_fresh_scratch(tmp_path, monkeypatch):
    from friday.config import load_settings

    home = tmp_path / "home"
    database = home / "data" / "state" / "authoritative.sqlite3"
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(database))

    # Scratch/test installations retain the deliberate bootstrap path.
    assert load_settings().database_path == database.resolve()

    monkeypatch.setenv("FRIDAY_DATABASE_MUST_EXIST", "1")
    with pytest.raises(RuntimeError, match="refusing to create"):
        load_settings()

    database.parent.mkdir(parents=True)
    database.write_bytes(b"")
    with pytest.raises(RuntimeError, match="refusing to create"):
        load_settings()

    database.write_bytes(b"existing-database")
    assert load_settings().database_path == database.resolve()


def test_required_database_removed_after_configuration_is_never_recreated(tmp_path, monkeypatch):
    """The existence check and sqlite open share a no-create contract.

    Regression for the startup TOCTOU: validation saw the authoritative image,
    another recovery step removed it, and the ordinary sqlite3 open silently
    bootstrapped a replacement at the same path.
    """
    from friday.config import load_settings
    from friday.storage import FridayStorage

    home = tmp_path / "home"
    database = home / "data" / "state" / "authoritative.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE authority_marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO authority_marker(value) VALUES('present')")

    monkeypatch.setenv("FRIDAY_HOME", str(home))
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(database))
    monkeypatch.setenv("FRIDAY_DATABASE_MUST_EXIST", "1")
    loaded = load_settings()
    assert loaded.database_must_exist is True
    assert loaded.public_dict()["data"]["database_must_exist"] is True

    database.unlink()
    storage = FridayStorage(loaded)
    try:
        with pytest.raises(
            sqlite3.OperationalError,
            match="(?:unable to open database|required Friday database)",
        ):
            storage.get_user("owner")
    finally:
        storage.close(final=True)

    assert not database.exists(), "mode=rw recreated the vanished authoritative database"


def test_required_database_truncated_after_configuration_is_never_initialized(tmp_path, monkeypatch):
    """An existing zero-byte file must not be bootstrapped at the open boundary."""

    from friday.config import load_settings
    from friday.storage import FridayStorage

    home = tmp_path / "home"
    database = home / "data" / "state" / "authoritative.sqlite3"
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(database))
    monkeypatch.delenv("FRIDAY_DATABASE_MUST_EXIST", raising=False)
    bootstrap = FridayStorage(load_settings())
    bootstrap.ensure_user("owner", preset_key="owner")
    bootstrap.close(final=True)

    monkeypatch.setenv("FRIDAY_DATABASE_MUST_EXIST", "1")
    loaded = load_settings()
    database.write_bytes(b"")

    storage = FridayStorage(loaded)
    try:
        with pytest.raises(sqlite3.OperationalError, match="required Friday database"):
            storage.get_user("owner")
    finally:
        storage.close(final=True)

    assert database.stat().st_size == 0, "Friday initialized the truncated authoritative image"


def test_required_nonempty_blank_sqlite_image_is_not_migrated(tmp_path, monkeypatch):
    """MUST_EXIST means an existing Friday DB, not any nonempty SQLite file."""

    from friday.config import load_settings
    from friday.storage import FridayStorage

    home = tmp_path / "home"
    database = home / "data" / "state" / "authoritative.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    original = database.read_bytes()
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(database))
    monkeypatch.setenv("FRIDAY_DATABASE_MUST_EXIST", "1")
    loaded = load_settings()

    storage = FridayStorage(loaded)
    try:
        with pytest.raises(sqlite3.OperationalError, match="recognizable Friday schema"):
            storage.get_user("owner")
    finally:
        storage.close(final=True)

    assert database.read_bytes() == original
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


@pytest.mark.parametrize("second_table", ["messages", "raw_objects"])
def test_required_foreign_database_with_common_table_names_is_not_modified(
    tmp_path,
    monkeypatch,
    second_table,
):
    """Generic names are not a Friday authority signature."""

    from friday.config import load_settings
    from friday.storage import FridayStorage

    home = tmp_path / "home"
    database = home / "data" / "state" / "authoritative.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        connection.execute(f'CREATE TABLE "{second_table}"(id TEXT PRIMARY KEY)')  # nosec B608
        connection.execute("INSERT INTO users(id) VALUES('foreign-owner')")
    original = database.read_bytes()

    monkeypatch.setenv("FRIDAY_HOME", str(home))
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(database))
    monkeypatch.setenv("FRIDAY_DATABASE_MUST_EXIST", "1")
    loaded = load_settings()
    storage = FridayStorage(loaded)
    try:
        with pytest.raises(sqlite3.OperationalError, match="recognizable Friday schema"):
            storage.get_user("owner")
    finally:
        storage.close(final=True)

    assert database.read_bytes() == original
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_the_bridge_accepts_both_header_spellings(settings):
    """Мост и бэкенд — разные процессы и обновляются не одновременно.

    На время переезда один из них какое-то время шлёт прежние заголовки. Отвергать
    их — значит устроить себе тишину в чате ровно в момент обновления, причём
    молча: снаружи это неотличимо от «бот умер».

    Мутация: читать только `x-friday-*` — тест краснеет.
    """
    from friday.server import _bridge_header

    class _Request:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    assert _bridge_header(_Request({"x-jericho-signature": "old"}), "signature") == "old"
    assert _bridge_header(_Request({"x-friday-signature": "new"}), "signature") == "new"
    # Новый заголовок побеждает, если пришли оба.
    both = _Request({"x-jericho-signature": "old", "x-friday-signature": "new"})
    assert _bridge_header(both, "signature") == "new"
    assert _bridge_header(_Request({}), "signature") == ""


def test_both_console_commands_point_at_the_same_entry_point():
    """Прежняя команда осталась синонимом: она в юнитах и в мышечной памяти."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["friday"] == "friday.cli:main"
    assert scripts.get("jericho") == "friday.cli:main", "старая команда перестала работать"


def test_no_module_reads_a_setting_behind_the_compatibility_point():
    """Никто не читает настройку в обход единой точки.

    Шесть чтений в `cli.py`/`tui.py` шли мимо неё напрямую через `os.environ`, и
    мост из-за этого не поднялся: токен в `.env.local` назван по-старому, а
    команда искала только новое имя. Поймано ЖИВЫМ ЗАПУСКОМ, не тестом.

    Проверяется исходный код, а не поведение помощника: помощник может быть
    сколь угодно правильным и при этом не вызываться — первая редакция этого
    файла проверяла именно его и мутацию «читать os.environ напрямую» не ловила.
    """
    import re

    root = Path(__file__).resolve().parents[1] / "friday"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "__init__.py" and path.parent.name == "config":
            continue  # сама точка чтения
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r'os\.environ\.get\(\s*["\']FRIDAY_', line):
                offenders.append(f"{path.relative_to(root.parent)}:{number}")
    assert not offenders, (
        "настройка читается в обход `config.env`, прежнее имя там работать не будет: " + ", ".join(offenders)
    )
