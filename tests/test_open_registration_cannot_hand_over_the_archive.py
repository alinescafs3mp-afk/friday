"""Впустить незнакомца и отдать ему всё — сочетание, которое не должно подняться.

`FRIDAY_TELEGRAM_OPEN_REGISTRATION=1` впускает в личной переписке любого, кто
напишет боту: человека, которого владелец не называл ни по имени, ни по номеру
чата. Что этот человек получит, решают две соседние настройки, и оба сочетания
отдают ему архив целиком — административный пресет или общий арендатор, где
право читать знания есть даже у самого узкого пресета.

Код это ОПИСЫВАЛ (цена расписана прямым текстом в докстринге `new_account_preset`),
но не запрещал. Описание живёт в исходнике, а сочетание переключается одной
строкой в `.env`.

Вторая половина проб не менее важна первой: сегодняшняя живая конфигурация —
`FRIDAY_NEW_ACCOUNT_PRESET=owner` и `FRIDAY_SHARED_ARCHIVE=1` при ВЫКЛЮЧЕННОЙ
открытой регистрации — это решение владельца о том, кто свой, и оно должно
проходить чисто. Запрет касается ровно сочетания «впускаем незнакомца» + «даём
ему всё».
"""

from __future__ import annotations

from dataclasses import replace

from friday.config import validate_settings


def _errors(settings) -> list[str]:
    return [item for item in validate_settings(settings) if not item.startswith("warning:")]


def _warnings(settings) -> list[str]:
    return [item for item in validate_settings(settings) if item.startswith("warning:")]


def test_open_registration_with_an_owner_preset_refuses_to_start(settings):
    broken = replace(settings, telegram_open_registration=True, new_account_preset="owner")
    named = [
        item
        for item in _errors(broken)
        if "FRIDAY_TELEGRAM_OPEN_REGISTRATION" in item and "FRIDAY_NEW_ACCOUNT_PRESET" in item
    ]
    assert named, f"незнакомец получал бы права владельца, а конфигурация проходила: {_errors(broken)}"


def test_open_registration_with_an_admin_preset_refuses_to_start(settings):
    """`admin` — обычная делегируемая роль, но у неё есть `admin.export`.

    То есть весь архив одним запросом. Отличие от `owner` тут не в объёме.
    """
    broken = replace(settings, telegram_open_registration=True, new_account_preset="admin")
    assert [item for item in _errors(broken) if "FRIDAY_NEW_ACCOUNT_PRESET" in item], _errors(broken)


def test_open_registration_with_a_shared_archive_refuses_to_start(settings):
    """Административных прав не нужно вовсе: общий архив — один арендатор на всех."""
    broken = replace(
        settings,
        telegram_open_registration=True,
        shared_archive=True,
        new_account_preset="",
    )
    named = [
        item
        for item in _errors(broken)
        if "FRIDAY_TELEGRAM_OPEN_REGISTRATION" in item and "FRIDAY_SHARED_ARCHIVE" in item
    ]
    assert named, f"общий архив открывался незнакомцу, а конфигурация проходила: {_errors(broken)}"


def test_the_owners_own_setup_still_validates_cleanly(settings):
    """Живая конфигурация владельца: широкий пресет, общий архив, ЗАКРЫТЫЙ вход.

    Кого впускать, решает список разрешённых чатов — то есть сам владелец. Это
    его решение от 2026-08-02, и запрет не имеет к нему отношения.
    """
    live = replace(
        settings,
        telegram_open_registration=False,
        new_account_preset="owner",
        shared_archive=True,
    )
    assert not [
        item for item in _errors(live) if "OPEN_REGISTRATION" in item or "NEW_ACCOUNT_PRESET" in item
    ], _errors(live)


def test_open_registration_alone_stays_legal(settings):
    """Самозапись с узким `newcomer` в собственном арендаторе — рабочая возможность."""
    narrow = replace(
        settings,
        telegram_open_registration=True,
        new_account_preset="",
        shared_archive=False,
    )
    assert not [
        item for item in _errors(narrow) if "OPEN_REGISTRATION" in item or "NEW_ACCOUNT_PRESET" in item
    ], _errors(narrow)


def test_another_preset_is_named_out_loud_but_not_refused(settings):
    """Состав произвольного пресета известен базе, а не коду.

    Запретить его вслепую значило бы решать за владельца; промолчать — не сказать,
    кому он достаётся. Поэтому предупреждение.
    """
    other = replace(
        settings,
        telegram_open_registration=True,
        new_account_preset="user",
        shared_archive=False,
    )
    assert not [item for item in _errors(other) if "NEW_ACCOUNT_PRESET" in item], _errors(other)
    assert [item for item in _warnings(other) if "FRIDAY_NEW_ACCOUNT_PRESET" in item], _warnings(other)


def test_the_owner_can_sign_under_the_combination(settings):
    """Подпись владельца снимает отказ — но не молчание.

    Живой экземпляр работает в этом режиме с 2026-08-02 по прямой просьбе
    владельца. Валидатор, роняющий чужую работающую систему из-за несогласия с её
    хозяином, — не защита, а поломка; это выяснилось не в рассуждении, а на живом
    экземпляре, который лёг от первой же редакции этой проверки.
    """
    signed = replace(
        settings,
        telegram_open_registration=True,
        new_account_preset="owner",
        shared_archive=True,
        open_registration_grants_full_access=True,
    )
    assert not _errors(signed), f"подписанное сочетание всё ещё роняет запуск: {_errors(signed)}"
    spoken = [item for item in _warnings(signed) if "OPEN_REGISTRATION" in item]
    assert len(spoken) == 2, f"подписанное сочетание замолчало вместо того, чтобы предупредить: {spoken}"


def test_the_signature_alone_grants_nothing(settings):
    """Подпись без самого сочетания не должна ничего говорить."""
    quiet = replace(
        settings,
        telegram_open_registration=False,
        new_account_preset="owner",
        shared_archive=True,
        open_registration_grants_full_access=True,
    )
    assert not [item for item in _warnings(quiet) if "OPEN_REGISTRATION" in item], _warnings(quiet)


def test_without_the_signature_it_is_still_a_refusal(settings):
    """Главное свойство: случайным переключением одного флага сюда не попасть."""
    unsigned = replace(
        settings,
        telegram_open_registration=True,
        new_account_preset="owner",
        shared_archive=True,
        open_registration_grants_full_access=False,
    )
    assert len([item for item in _errors(unsigned) if "OPEN_REGISTRATION" in item]) == 2
