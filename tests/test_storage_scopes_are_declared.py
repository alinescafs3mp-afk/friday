"""Каждый метод хранилища, берущий `user_id`, обязан знать, ЧЕЙ это идентификатор.

В общем архиве (`FRIDAY_SHARED_ARCHIVE`) у актора три разные личности, и все три —
строки. Неправильная строка проходит без единого предупреждения:

    tenant    — общий корпус: знания, граф, поиск, входящие. Один на всех,
                иначе люди не видели бы материал друг друга;
    principal — человек: переписка, заявки, напоминания, авторство решений,
                личные указания и поправки, лимиты;
    credential — способ входа: отзыв токена, события сессии. НЕ автор.

Класс дефекта подтверждён трижды за одни сутки 2026-08-03:

    заявка на подтверждение опасного действия была видна и решаема ЛЮБЫМ
    участником — scoped по арендатору (разбор Codex §12.2, проверено на живом
    коде, починено);
    указание «отвечай мне кратко» ложилось в учётку арендатора и стало бы
    правилом для всех (разбор Сола, починено);
    личные разрешения, лимиты обращений и идемпотентность ответов ДО СИХ ПОР
    считаются по арендатору (§12.1, 12.3, 12.4 — открыто).

Полное лечение — типы вместо строк — документ Codex сам не советует начинать с
переименования по репозиторию: велик риск красиво назвать ту же путаницу. Здесь
дешёвая и работающая половина: перечень личных методов назван поимённо, а общее
их число закреплено. Новый метод с `user_id` роняет тест, и автор обязан решить,
чей это идентификатор, — ровно как с `EXPECTED_MEMBER_COUNT` в соседнем файле.

Это НЕ доказательство правильности вызовов: метод из перечня можно позвать с
арендатором, и тест этого не увидит. Он делает класс видимым, а не невозможным.
Так и задумано: доказательство стоит недель, видимость — часа.
"""

from __future__ import annotations

import inspect

from friday.storage import FridayStorage

#: Методы, работающие с ЛИЧНЫМИ данными. Их `user_id` — человек (`actor.own_id`),
#: а не арендатор. Список закрыт: всё остальное считается общим корпусом.
PERSON_SCOPED = {
    # Переписка человека личная даже при общем архиве: общими просьба владельца
    # делала документы и записи, а не чужие разговоры.
    "archive_conversation",
    "clear_channel_conversation",
    "count_conversations",
    "count_messages",
    "create_conversation",
    "delete_conversation",
    "get_channel_conversation",
    "get_conversation",
    "get_conversation_messages",
    "get_message",
    "list_conversations",
    "set_conversation_archived",
    "set_conversation_mode",
    "store_message",
    # Заявка — просьба КОНКРЕТНОГО человека разрешить действие от его имени.
    # Была общей: участник видел чужую и мог подтвердить.
    "_approval_row",
    "claim_action_approval",
    "count_action_approvals",
    "create_action_approval",
    "decide_action_approval",
    "finish_action_approval",
    "get_action_approval",
    "list_action_approvals",
    "mark_action_approval_uncertain",
    # Личные настройки и память о человеке.
    "remember_correction",
    "remember_standing_rule",
    "_remember_personal_line",
    # Уведомления приходят человеку в его чат.
    "dismiss_notification",
    "enqueue_notification",
    # Ключи входа принадлежат человеку, а не корпусу.
    "create_api_token",
    "list_api_tokens",
}

#: Сколько всего методов хранилища принимают `user_id`.
#:
#: Обновлять ОСОЗНАННО и вместе с решением: новый метод — это новый ответ на
#: вопрос «арендатор или человек?». Если ответ «человек», имя идёт в
#: `PERSON_SCOPED` выше; если «арендатор» — просто число растёт, и это тоже
#: решение, принятое явно.
EXPECTED_USER_ID_METHODS = 225


def _methods_taking_user_id() -> set[str]:
    found: set[str] = set()
    for name in dir(FridayStorage):
        if name.startswith("__"):
            continue
        member = getattr(FridayStorage, name, None)
        if not callable(member):
            continue
        try:
            signature = inspect.signature(member)
        except (TypeError, ValueError):
            continue
        if "user_id" in signature.parameters:
            found.add(name)
    return found


def test_every_personal_method_still_exists() -> None:
    """Перечень описывает настоящую поверхность, а не воспоминание о ней."""
    surface = _methods_taking_user_id()
    missing = sorted(PERSON_SCOPED - surface)

    assert not missing, f"в перечне личных методов есть несуществующие: {missing}"


def test_a_new_method_forces_a_decision() -> None:
    """Мутация: добавить метод с `user_id` и не решить, чей он, — тест краснеет.

    Молча расширенная поверхность и есть тот способ, которым сюда попали все три
    подтверждённых дефекта: механизм различения существовал, но новое место о нём
    не знало.
    """
    surface = _methods_taking_user_id()

    assert len(surface) == EXPECTED_USER_ID_METHODS, (
        f"методов с user_id стало {len(surface)}, ожидалось {EXPECTED_USER_ID_METHODS}. "
        "Решите, чей это идентификатор: человек — в PERSON_SCOPED, арендатор — просто "
        "обновите число."
    )


def test_the_personal_surface_is_not_empty() -> None:
    """Пустой перечень означал бы, что различение потеряно целиком."""
    assert len(PERSON_SCOPED) >= 25


def test_the_known_holes_are_named_in_the_docstring() -> None:
    """Открытые дыры перечислены здесь же, чтобы не потеряться между сессиями.

    Личные разрешения, лимиты и идемпотентность до сих пор считаются по
    арендатору. Пока это не починено, файл обязан об этом говорить.
    """
    import sys

    doc = sys.modules[__name__].__doc__ or ""
    assert "12.1" in doc and "12.3" in doc and "12.4" in doc
    assert "открыто" in doc


def test_it_does_not_pretend_to_prove_call_sites() -> None:
    """Честность о собственных пределах — часть работы прибора.

    Метод из перечня можно позвать с арендатором, и этот тест не увидит. Он
    делает класс ВИДИМЫМ, а не невозможным, и docstring обязан это признавать —
    иначе следующий человек примет видимость за доказательство.
    """
    import sys

    doc = sys.modules[__name__].__doc__ or ""
    assert "НЕ доказательство правильности вызовов" in doc
