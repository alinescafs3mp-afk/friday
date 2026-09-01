"""Каждый метод хранилища, берущий `user_id`, обязан знать, ЧЕЙ это идентификатор.

В общем архиве (`FRIDAY_SHARED_ARCHIVE`) у актора три разные личности, и все три —
строки. Неправильная строка проходит без единого предупреждения:

    tenant    — общий корпус: знания, граф, поиск, входящие. Один на всех,
                иначе люди не видели бы материал друг друга;
    principal — человек: переписка, заявки, напоминания, авторство решений,
                личные указания и поправки, лимиты;
    credential — способ входа: отзыв токена, события сессии. НЕ автор.

Feedback добавляет четвёртый, честно смешанный домен: его `user_id` — раздел
субъекта. Для ответа это principal человека, для классификации входящего —
tenant корпуса. Угадать один из них по имени метода невозможно, поэтому эта
поверхность перечислена отдельно, а не ложно названа личной или общей.

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
    "backfill_conversation_passages",
    "clear_channel_conversation",
    "count_conversations",
    "count_messages",
    "create_conversation",
    "delete_conversation",
    "get_channel_conversation",
    "get_conversation",
    "get_conversation_messages",
    "get_message",
    "idempotency_mark_effect_possible",
    "list_chat_thread",
    "list_conversations",
    "list_messages_window",
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
    # Every Obsidian profile, vault, device, onboarding row and operation belongs
    # to the individual Android owner, never to a shared archive tenant.
    "bind_obsidian_android_device",
    "create_obsidian_bundle",
    "finalize_obsidian_onboarding",
    "create_obsidian_candidate_set",
    "get_obsidian_active_frame",
    "get_obsidian_candidate_set",
    "get_obsidian_conflict",
    "get_obsidian_device",
    "get_obsidian_note_binding",
    "get_obsidian_note_index",
    "get_obsidian_onboarding",
    "get_obsidian_operation",
    "get_obsidian_profile",
    "get_obsidian_vault",
    "list_obsidian_conflicts",
    "list_obsidian_legacy_marker_candidates",
    "list_obsidian_operations",
    "list_obsidian_note_bindings",
    "list_obsidian_note_index",
    "list_obsidian_note_links",
    "list_obsidian_pairing_candidates",
    "prepare_obsidian_operation",
    "replace_obsidian_note_links",
    "resolve_obsidian_conflict",
    "record_obsidian_conflict",
    "record_obsidian_pairing_candidates",
    "rotate_obsidian_setup_token",
    "select_obsidian_pairing_candidate",
    "select_obsidian_candidate",
    "invalidate_obsidian_active_frame",
    "invalidate_obsidian_candidate_set",
    "invalidate_obsidian_note_index",
    "tombstone_obsidian_note_binding",
    "transition_obsidian_onboarding",
    "transition_obsidian_operation",
    "update_obsidian_device",
    "update_obsidian_profile",
    "update_obsidian_vault",
    "update_obsidian_vault_alias",
    "upsert_obsidian_active_frame",
    "upsert_obsidian_note_binding",
    "upsert_obsidian_note_index",
}

#: Feedback хранится в разделе субъекта, который зависит от `target_type`.
#:
#: - ответ (`answer`) оценивает конкретный человек: `actor.own_id`;
#: - классификация (`classification`) обучает общий корпус: `actor.user_id`.
#:
#: Поэтому `user_id` здесь нельзя безусловно заменить ни на principal, ни на
#: tenant. `store_feedback` несёт тот же домен внутри `FeedbackItem.user_id` и
#: не попадает в этот signature-based перечень.
FEEDBACK_PARTITION_SCOPED = {
    "_feedback_state_filter",
    "count_feedback_state",
    "get_current_feedback_stats",
    "get_feedback_for_target",
    "get_feedback_state",
    "get_feedback_stats",
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
# 237 → 239: обе relation-timeline операции принимают tenant общего графа;
# личность человека к этой ленте не относится.
# 239 → 240: relation_history_status проверяет completeness общего tenant-графа;
# это не личная переписка и не credential identity.
# 240 → 241: count_feedback_state — новый reader feedback-раздела; для answer
# разделом служит person, для classification — tenant.
# 241 → 242: count_visible_raw_objects считает приватно-доступный tenant-корпус;
# отдельной личности человека у общей базы здесь нет.
# 242 → 252: the ten owned-file catalog/alias/search readers added after the
# previous pin all take the shared archive tenant as ``user_id``; uploader or
# person authority is carried separately and rechecked by those methods.
# 252 → 253: list_chat_thread is the bounded person-level admin transcript;
# its user_id is the principal, never the shared archive tenant.
# 253 → 254: idempotency_mark_effect_possible fences one person's transport
# request before a possible side effect; sharing it at tenant scope would let
# another participant poison or replay that person's operation key.
# 254 → 255: list_messages_window pages one person's private accepted chat
# history; its user_id is the principal even when the archive is shared.
# 255 → 256: search_owned_files_by_term searches the shared archive tenant;
# exact uploader/person authority is carried and checked separately.
# 256 → 277: the Obsidian storage surface is private to one Android owner.
# 277 → 294: schema-36 bindings/index/links and expiring continuation state
# remain private to that same owner even when Friday uses a shared archive.
# 297 → 298: legacy Obsidian marker migration reads only one owner's operation
# journal; shared-tenant scope would expose private note paths across people.
# 298 → 300: purge_secondary_product_witness and
# consume_secondary_product_rollout_attestation operate on the shared archive
# tenant; uploader/person identity is bound separately by the witness proof.
# 300 → 306: the body-free DocumentCatalog read/upsert/rebuild/backfill/
# reconcile/coverage surface is partitioned by the Raw Object tenant.
# 306 → 309: claim_mission_task, cancel_mission_and_tasks and
# normalize_future_mission_task_start fence the existing shared-tenant mission
# surface; the human proposer remains separately bound by created_by.
# 309 → 310: backfill_conversation_passages advances one person's private
# accepted conversation history; shared-tenant scope would mix private chats.
# 310 → 311: list_inbox_advice_candidates pages the shared archive tenant's
# privacy-filtered Inbox; a separate person identity is neither accepted nor
# needed by this background corpus worker.
EXPECTED_USER_ID_METHODS = 311


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
    declared = PERSON_SCOPED | FEEDBACK_PARTITION_SCOPED | APPROVAL_TENANT_WITH_A_SEPARATE_PERSON
    missing = sorted(declared - surface)

    assert not missing, f"в перечнях scope-методов есть несуществующие: {missing}"


def test_feedback_partition_is_not_mislabeled_as_person_or_tenant() -> None:
    """Смешанный feedback-домен должен остаться отдельным и проверяемым."""

    assert FEEDBACK_PARTITION_SCOPED.isdisjoint(PERSON_SCOPED)
    assert FEEDBACK_PARTITION_SCOPED.isdisjoint(APPROVAL_TENANT_WITH_A_SEPARATE_PERSON)
    for name in sorted(FEEDBACK_PARTITION_SCOPED):
        signature = inspect.signature(getattr(FridayStorage, name))
        assert "user_id" in signature.parameters
        assert "target_type" in signature.parameters


def test_an_approval_names_its_person_in_a_separate_parameter() -> None:
    """Метка «арендатор + отдельная личность» — проверяемое утверждение.

    Мутация: убрать `person_id` из `get_action_approval` — тест краснеет. Без
    этого перечень остался бы просто списком имён, а он должен быть контрактом.
    """
    named_person = {
        "_approval_row",
        "count_action_approvals",
        "decide_action_approval",
        "get_action_approval",
        "list_action_approvals",
    }
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
    assert (
        len(PERSON_SCOPED) + len(FEEDBACK_PARTITION_SCOPED) + len(APPROVAL_TENANT_WITH_A_SEPARATE_PERSON)
        >= 30
    )


def test_it_does_not_pretend_to_prove_call_sites() -> None:
    """Честность о собственных пределах — часть работы прибора.

    Метод из перечня можно позвать с арендатором, и этот тест не увидит. Он
    делает класс ВИДИМЫМ, а не невозможным, и docstring обязан это признавать —
    иначе следующий человек примет видимость за доказательство.
    """
    import sys

    doc = sys.modules[__name__].__doc__ or ""
    assert "НЕ доказательство правильности вызовов" in doc
