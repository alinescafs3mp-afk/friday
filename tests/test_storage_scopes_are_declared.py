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
    личные разрешения, лимиты обращений и идемпотентность ответов считались по
    арендатору — §12.1 (`AuthorizationService.authorize` берёт `actor.own_id`),
    §12.4 (ключи лимитов там же по человеку) и §12.3 (`idempotency_claim`);
    закрыто ночью 2026-08-04, сторожа — `test_personal_permissions_work_in_a_
    shared_archive`, `test_a_noisy_participant_does_not_starve_the_others`,
    `test_an_answer_is_not_replayed_to_another_person`.

Прежняя редакция этой шапки говорила «до сих пор открыто» ещё сутки после
починки, и отдельный тест ТРЕБОВАЛ слова «открыто»: тот, кто написал бы правду,
получил бы красный набор и, скорее всего, вернул бы ложь, чтобы позеленить.
Указано внешним разбором (Сол, 2026-08-04) и проверено по коммитам. Тест,
охраняющий неверное утверждение, хуже отсутствия теста — поэтому он снят, а не
вывернут наизнанку: прибивать текст к тексту не следовало с самого начала.

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

#: Заявки: `user_id` — АРЕНДАТОР, человек называется отдельным параметром.
#:
#: Здесь двойной контракт, и прежняя редакция перечня врала о нём. Заявка
#: действительно личная — но личность несут `requested_by` (при создании) и
#: `person_id` (при чтении и решении), а `user_id` остаётся общим корпусом:
#: `create_action_approval(actor.user_id, …, requested_by=actor.own_id)`.
#:
#: Цена ошибки в этой метке — не в правильности перечня, а в будущем вызове по
#: нему: заявка, созданная под `own_id`, ляжет туда, где её никто не ищет.
#: Человек увидит «нужно ваше решение» в чате, а список окажется пуст и кнопка
#: ответит «заявка не найдена». Указано внешним разбором (Сол, 2026-08-04).
#:
#: У трёх последних личности нет вовсе: их зовут по арендатору из исполнителя,
#: когда решение уже принято, — и это верно, там нет человека, только заявка.
APPROVAL_TENANT_WITH_A_SEPARATE_PERSON = {
    "_approval_row",
    "count_action_approvals",
    "create_action_approval",
    "decide_action_approval",
    "get_action_approval",
    "list_action_approvals",
    # Без личности вовсе — исполнительная часть уже решённой заявки.
    "claim_action_approval",
    "finish_action_approval",
    "mark_action_approval_uncertain",
}

#: Сколько всего методов хранилища принимают `user_id`.
#:
#: Обновлять ОСОЗНАННО и вместе с решением: новый метод — это новый ответ на
#: вопрос «арендатор или человек?». Если ответ «человек», имя идёт в
#: `PERSON_SCOPED` выше; если «арендатор» — просто число растёт, и это тоже
#: решение, принятое явно.
EXPECTED_USER_ID_METHODS = 228


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
    missing = sorted((PERSON_SCOPED | APPROVAL_TENANT_WITH_A_SEPARATE_PERSON) - surface)

    assert not missing, f"в перечне личных методов есть несуществующие: {missing}"


def test_an_approval_names_its_person_in_a_separate_parameter() -> None:
    """Метка «арендатор + отдельная личность» — проверяемое утверждение.

    Мутация: убрать `person_id` из `get_action_approval` — тест краснеет. Без
    этого перечень остался бы просто списком имён, а он должен быть контрактом.
    """
    named_person = {"_approval_row", "count_action_approvals", "decide_action_approval",
                    "get_action_approval", "list_action_approvals"}
    for name in sorted(named_person):
        signature = inspect.signature(getattr(FridayStorage, name))
        assert "person_id" in signature.parameters, f"{name} потерял личность заявителя"
        assert "user_id" in signature.parameters, f"{name} перестал брать арендатора"

    creation = inspect.signature(FridayStorage.create_action_approval)
    assert "requested_by" in creation.parameters, "заявка создаётся без автора"


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
    """Пустой перечень означал бы, что различение потеряно целиком.

    Порог считается по обоим перечням: девять методов заявок переехали из
    `PERSON_SCOPED` в свой набор не потому, что перестали быть личными, а потому
    что личность у них называется отдельным параметром.
    """
    assert len(PERSON_SCOPED) + len(APPROVAL_TENANT_WITH_A_SEPARATE_PERSON) >= 25


def test_it_does_not_pretend_to_prove_call_sites() -> None:
    """Честность о собственных пределах — часть работы прибора.

    Метод из перечня можно позвать с арендатором, и этот тест не увидит. Он
    делает класс ВИДИМЫМ, а не невозможным, и docstring обязан это признавать —
    иначе следующий человек примет видимость за доказательство.
    """
    import sys

    doc = sys.modules[__name__].__doc__ or ""
    assert "НЕ доказательство правильности вызовов" in doc
