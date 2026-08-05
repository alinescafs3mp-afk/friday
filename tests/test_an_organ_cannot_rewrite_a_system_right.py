"""Право, принесённое снаружи, не переписывает системное.

Способности складывались в словарь по имени, и последний объявивший выигрывал.
Воспроизведено на стенде до правки: орган объявляет

    CapabilityDefinition("kg.merge", "мой безобидный обход", "kg", 0, ("guest", "user"))

— и `_builtin_grants("guest")` начинает возвращать `kg.merge`. Гость получает право
сливать сущности: на корпусе владельца это 4609 узлов, среди них люди с похожими
ФИО, и ошибка слияния означает двух разных людей под одним узлом. Ни исключения, ни
строки в журнале — со стороны выглядит как «так и было задумано».

Дыра не в органах: свои пять ведут себя честно и ставят `source="organ"`. Дыра в
том, что честность держалась на соглашении, а органы собираются из каталога — один
чужой файл рядом со своими ничем от них не отличим.

Вторая половина той же дыры — умолчание `source="core"`. Объявитель, забывший поле,
представляется ядром, и в журнале потом не отличить системное право от принесённого.
Умолчание молчит именно тогда, когда о нём важнее всего знать.
"""

from __future__ import annotations

import pytest

from friday.permissions import CORE_CAPABILITIES, AuthorizationService, CapabilityDefinition


def test_an_organ_cannot_take_the_name_of_a_system_right():
    """Именно тот вызов, который отдавал гостю слияние сущностей.

    Мутация: вернуть в `register_capability` простую запись в словарь — тест краснеет
    на проверке гостя, а не только на исключении.
    """
    auth = AuthorizationService()
    assert "kg.merge" not in auth._builtin_grants("guest")  # noqa: SLF001

    with pytest.raises(ValueError, match="уже объявлена"):
        auth.register_capability(
            CapabilityDefinition(
                "kg.merge", "мой безобидный обход", "kg", 0, ("guest", "user"), source="organ"
            )
        )

    assert "kg.merge" not in auth._builtin_grants("guest"), (  # noqa: SLF001
        "гость получил право сливать сущности — принесённое объявление переписало системное"
    )
    assert auth.get_capability("kg.merge").source == "core"


def test_a_brought_right_cannot_call_itself_the_core():
    """Умолчание `source="core"` не даёт чужому праву выглядеть системным."""
    auth = AuthorizationService()
    with pytest.raises(ValueError, match="в ядровом списке её нет"):
        auth.register_capability(CapabilityDefinition("mine.thing", "чужое", "x", 0, ()))


def test_an_honest_organ_still_registers():
    """Ошибка в другую сторону не менее дорога: органы должны продолжать работать."""
    auth = AuthorizationService()
    capability = CapabilityDefinition("mine.thing", "чужое", "x", 0, ("user",), source="organ")
    auth.register_capability(capability)
    assert auth.get_capability("mine.thing") is capability
    assert "mine.thing" in auth._builtin_grants("user")  # noqa: SLF001

    # Повтор ОДНОГО И ТОГО ЖЕ объявления проходит: сборок приложения несколько
    # (сервер, CLI, тесты), и требовать от них ровно одного вызова — значит менять
    # безопасность на хрупкость.
    auth.register_capability(CapabilityDefinition("mine.thing", "чужое", "x", 0, ("user",), source="organ"))


def test_one_organ_cannot_rewrite_another_organs_right():
    """Подмена бывает не только «орган против ядра», но и «орган против органа».

    Найдено мутацией: проверка, сравнивающая только `source`, оставляла зелёными все
    тесты — потому что ни один не описывал этот случай. А случай ровно тот же по
    последствиям: второй объявитель с тем же именем расширяет пресеты, и право
    достаётся тому, кому первый его не давал. `source` у обоих «organ», и по нему их
    не различить — значит сравнивать надо определение целиком.
    """
    auth = AuthorizationService()
    auth.register_capability(
        CapabilityDefinition("shared.name", "первый орган", "x", 0, ("admin",), source="organ")
    )
    with pytest.raises(ValueError, match="уже объявлена"):
        auth.register_capability(
            CapabilityDefinition("shared.name", "второй орган", "x", 0, ("guest", "user"), source="organ")
        )
    assert "shared.name" not in auth._builtin_grants("guest")  # noqa: SLF001
    assert auth.get_capability("shared.name").description == "первый орган"


def test_every_living_organ_declares_where_it_came_from(settings):
    """Проверяется не механизм, а что настоящие органы через него проходят.

    Зелёный стенд не доказывает, что боевая сборка поднимется: правка, запрещающая
    `source="core"` чужим именам, уронила бы регистрацию органов целиком, если бы хоть
    один из них поле не заполнял.
    """
    from friday.organs import build_registry

    auth = AuthorizationService()
    registry = build_registry(settings)
    declared = list(registry.capabilities())
    assert declared, "органы не объявили ни одной способности — проверять нечего"

    for capability in declared:
        auth.register_capability(capability)
        assert capability.source != "core", f"{capability.security_id} представляется ядром"

    core_names = {item.security_id for item in CORE_CAPABILITIES}
    for name in core_names:
        assert auth.get_capability(name).source == "core", f"{name} перекрыт органом"
