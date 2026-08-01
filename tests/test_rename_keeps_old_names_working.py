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

from pathlib import Path

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
        "настройка читается в обход `config.env`, прежнее имя там работать не будет: "
        + ", ".join(offenders)
    )
