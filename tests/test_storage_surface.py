"""`FridayStorage` is the single data-access surface; splitting it must not move it.

The class carries 181 methods over 35 tables and is reached from roughly 140 call
sites. Unlike the HTTP layer it publishes no schema, so a refactor has no external
contract to diff against — this file is that contract: every attribute name the class
exposes, with its signature.

It also guards the failure mode a name list alone would miss. Once the class is
assembled from mixins, two of them defining the same method silently shadow one
another by MRO, and the surface would still look complete while the wrong
implementation runs.

Update EXPECTED only when deliberately adding or removing a method.
"""

from __future__ import annotations

import inspect
import re

from friday.storage import FridayStorage


def _surface() -> dict[str, str]:
    surface: dict[str, str] = {}
    for name, member in inspect.getmembers(FridayStorage):
        if name.startswith("__"):
            continue
        if not (inspect.isfunction(member) or inspect.ismethod(member) or isinstance(member, property)):
            continue
        if isinstance(member, property):
            surface[name] = "property"
            continue
        surface[name] = str(inspect.signature(member))
    return surface


def test_storage_exposes_the_same_surface() -> None:
    surface = _surface()
    assert len(surface) == EXPECTED_MEMBER_COUNT, (
        f"FridayStorage exposes {len(surface)} members, expected {EXPECTED_MEMBER_COUNT}. "
        "Update EXPECTED_MEMBER_COUNT only when a method was added or removed on purpose."
    )
    missing = sorted(set(EXPECTED_SIGNATURES) - set(surface))
    assert not missing, f"members disappeared: {missing}"
    changed = sorted(
        name
        for name, signature in EXPECTED_SIGNATURES.items()
        if name in surface and surface[name] != signature
    )
    assert not changed, f"signatures changed: {changed}"


def test_no_method_is_defined_twice_across_the_class_hierarchy() -> None:
    """A split into mixins makes silent shadowing possible: two bases defining the
    same name resolve by MRO and the loser never runs, while the surface still looks
    complete. Nothing else in the suite would notice."""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for base in FridayStorage.__mro__:
        if base is object:
            continue
        for name, member in vars(base).items():
            if name.startswith("__") or not callable(member):
                continue
            if name in seen and seen[name] != base.__name__:
                duplicates.append(f"{name}: {seen[name]} and {base.__name__}")
            seen.setdefault(name, base.__name__)
    assert not duplicates, f"method defined in more than one base: {duplicates}"


# 244 → 245: dense_vector_signature — дешёвая подпись векторов для резидентного кэша.
# 245 → 247: knowledge_missing_document_date + set_document_date — разовый проход,
# достающий собственную дату документа из провенанса уже загруженных файлов.
# 247 → 248: knowledge_ids_in_window — жёсткий предфильтр по периоду для поиска,
# построенный ТЕМ ЖЕ предикатом, что список и его счётчик.
# 248 → 249: list_documents_by_own_date — лента по СОБСТВЕННОЙ дате документа;
# list_knowledge_objects сортирует по важности и свежести записи, для хроники
# нужен порядок по дате самого документа.
# 249 → 251: knowledge_date_histogram + count_knowledge_without_own_date — плотность
# корпуса по годам/месяцам/дням для экрана хроники и число тех, кто в неё не попадёт.
# 251 → 253: knowledge_bodies_after + decided_entity_links — разовый проход правилом
# ФИО по архиву, загруженному до появления правила. Первый метод отдаёт тела страницами
# по КУРСОРУ `rowid`, а не через LIMIT/OFFSET: идентификаторы здесь uuid4, и сортировка
# по ним случайна. Второй существует ровно затем, чтобы проход не спорил с человеком —
# `link_knowledge_entity` перезаписывает статус по ON CONFLICT, и без этой проверки
# отклонённая владельцем связь молча вернулась бы в accepted.
# 253 → 255: find_entities_by_normalized_names + iter_entities снимают потолок
# полного списка для точного сопоставления и token-overlap обхода.
# 255 → 256: list_entities_knowledge_refs пакетирует проекции текущего фронта BFS.
# 258 → 259: search_messages — FTS по истории переписки (G16 / schema 20).
# 259 → 260: set_conversation_title — self-service rename (G18c).
# 264 → 266: entity_knowledge_summary + get_entity_knowledge_cards — сводка карточки
# объекта считается по ВСЕМ документам сущности (а не по показанной странице), а сам
# список показывается без тел документов.
# 266 → 267: restore_entity_version — правка сущности стала обратимой (спека v3 §2);
# у знаний обратный ход был давно, у сущностей снимки писались «в никуда».
# 267 → 269: count_documents_by_own_date + общий _own_date_window. Чат печатал
# «показаны первые 10 из 11» на периоде с сотнями документов: длина собственной
# страницы подавалась как число документов периода.
# 269 → 271: merge_version_floor + undelete_entity. Первый закрывает откат через
# версию, порождённую слиянием (иначе «отменить последнюю правку» стирало
# алиас-мост, а слитая сущность оставалась надгробием); второй даёт удалению
# обратный ход, без которого «мягкое удаление» было мягким только на словах.
# 271 → 272: knowledge_impact — вторая половина lineage, «что затронет изменение»:
# на живом корпусе 1168 сущностей из 4448 держатся на ЕДИНСТВЕННОМ документе.
# 272 → 278: мониторы — сохранённый вопрос, за которым система следит сама
# (create/get/list/iter_active/stop/mark_checked). Граница «что уже видели» —
# курсор по rowid, а не время: `utc_now()` секундной точности, и документ,
# пришедший в ту же секунду, при сравнении по времени терялся бы навсегда.
# 278 → 289: подтверждение опасного действия (спека v3 §5) — create/get/list/count/
# decide/claim/finish/mark_uncertain/expire/reconcile_stale_claims/approval_is_terminal/
# _approval_row.
# Отдельный слой, а не поле в таблице миссий: все три его свойства — атомарность
# заявления, привязка к отпечатку аргументов и различимость исходов (в том числе
# `uncertain`, который НЕЛЬЗЯ повторять) — это свойства именно SQL.
#: +1 к прежним 302: добавлен `user_messages` — что человек ПИСАЛ. Надзор до
#: этого читал только загруженные материалы, и на «что писал JBL?» отвечал
#: «сообщений 42, но записи не загрузились»: у того, кто просто переписывается,
#: загрузок ноль.
#: +2 к 303: `list_files_received_on` и `count_files_received_on` — файлы,
#: пришедшие в названные дни, и их полное число. Материал для сборки архива:
#: «собери документы за 10, 13 и 25 число». Список и счёт разведены намеренно —
#: длина страницы не должна выдавать себя за размер дня.
#: +2 к 305: `settle_uncertain_approval` и `list_uncertain_approvals` — сверка
#: неизвестного исхода наблюдением (спека v3 §5). Заявка, чьё исполнение
#: оборвалось, уходила в `uncertain` и висела там навсегда: сверка для шагов
#: миссии существовала, а для заявок — нет.
#: +1 к 307: `find_file_by_extracted_text` — тот же ДОКУМЕНТ, пришедший другим
#: файлом. Дедупликация сравнивала байты, а Word при пересохранении их меняет:
#: 56 пар из 200 в очереди разбора имели побайтово одинаковый текст и ни одна не
#: совпадала по хешу файла.
#: +1 к 308: `chat_feed_cursor` — отпечаток ленты переписки. Панель спрашивает
#: его раз в несколько секунд вместо самой ленты: 2 мс против 34 мс.
#: +1 к 309: `remember_standing_rule` — указание человека о том, как Пятнице себя
#: вести, сказанное в разговоре. Чтение и запись метаданных идут одной
#: транзакцией, в отличие от `/api/me/instructions`: там правит человек руками и
#: изредка, здесь пишет сам ход разговора, и два хода подряд при том же приёме
#: потеряли бы одну из правок вместе со ВСЕМИ остальными ключами метаданных.
#: +2 к 310: `remember_correction` и общее для него с правилами тело
#: `_remember_personal_line`. Поправка человека («День морской пехоты 27 ноября, а
#: не 27 июля») — не указание о СТИЛЕ, а сведение о том, ЧТО правда, и списки
#: разные. Механика у них одна, поэтому тело вынесено: держать две копии значило
#: бы починить гонку в одном месте и забыть в другом.
#: +8 к 312: слой ночных сводок (`begin_day_compact`, `finish_day_compact`,
#: `abandon_day_compact`, `get_day_compact`, `list_day_compacts`,
#: `count_day_compacts`, `days_needing_a_compact` и разбор строки `_compact_row`).
#: Заказ владельца 2026-08-04. Сводка хранит коды инцидентов и счётчики; текста
#: из переписки в ней нет по построению, поэтому отдельного «очистителя» в этом
#: списке не появилось — очищать нечего.
# 342 → 344: relation valid-time boundaries have their own bounded list and exact
# count; the unified KG timeline must not infer a total from the returned page.
# 344 → 345: relation_history_status validates the immutable completeness floor
# and mutable-identity boundary once for a transaction-time graph snapshot.
# 345 → 346: _validate_relation_history_schema fail-closes a schema-31 marker
# before idempotent DDL can conceal missing capture/protection mechanisms.
# 346 → 347: _observe_relation_history_boundary persists a historical read as
# a logical-clock promise before any mutable graph projection is returned.
# 347 → 348: count_feedback_state counts the same privacy-filtered personal
# feedback rows as get_feedback_state without turning a bounded page into a total.
# 348 → 349: count_visible_raw_objects exposes exact tenant-wide raw/file
# aggregates through the same privacy boundary as visible knowledge.
# 349 → 351: conversation attachment selection added an authorized-id FTS
# search and a body-free descriptor batch read.
# 351 → 352: cross-conversation selection added the uploader-owned file
# catalog used by exact names and indirect clues.
# 352 → 354: bounded exact-filename and direct content lookup keep uploader,
# privacy and lifecycle filters ahead of their ambiguity/completeness sentinels.
# 354 → 355: dense/reranked file-source candidates are re-authorized against
# immutable Raw bytes and the current Inbox verdict before an excerpt is projected.
# 356 → 357: every successful transport identity of a content-deduplicated file
# receives an immutable alias to the canonical Raw Object.
# 357 → 358: one exact-uploader corpus selector keeps received/document time
# roles distinct and returns totals separately from its bounded page.
# 359 → 360: the admin messenger reads one bounded chronological tail per
# person instead of issuing one request for each of five conversations.
# 364 → 388: schema 35 adds the owner-scoped Obsidian onboarding, operation,
# delivery, conflict, vault-alias and atomic-finalization storage surface.
# 388 → 405: schema 36 adds stable bindings, revision index/link snapshots and
# expiring candidate/Active Frame CRUD without exposing a cross-owner reader.
# 405 → 408: owner-scoped operation status plus explicit conflict lookup/resolution.
# 408 → 409: bounded owner-scoped legacy-marker migration candidates.
EXPECTED_MEMBER_COUNT = 409
EXPECTED_SIGNATURES: dict[str, str] = {
    "bind_owned_file_source_ref_alias": "(self, user_id: 'str', uploaded_by: 'str', source_ref: 'str', raw_object_id: 'str', supplied_filename: 'str' = '') -> 'bool'",
    "find_owned_files_by_filename": "(self, user_id: 'str', uploaded_by: 'str', filename: 'str') -> 'list[dict[str, Any]]'",
    "get_raw_object_descriptors": "(self, raw_ids: 'list[str]', user_id: 'str', *, limit: 'int' = 1000) -> 'list[dict[str, Any]]'",
    "get_searchable_file_sources": "(self, user_id: 'str', raw_ids: 'list[str]', *, uploaded_by: 'str | None' = None, limit: 'int' = 100, include_content: 'bool' = False) -> 'list[dict[str, Any]]'",
    "list_owned_file_catalog": "(self, user_id: 'str', uploaded_by: 'str', *, limit: 'int' = 5000) -> 'list[dict[str, Any]]'",
    "list_obsidian_legacy_marker_candidates": "(self, user_id: 'str', *, limit: 'int' = 5000) -> 'list[dict[str, Any]]'",
    "prepare_obsidian_operation": "(self, user_id: 'str', *, operation_id: 'str', vault_id: 'str', method: 'str', arguments_digest: 'str', expected_revision: 'str | None' = None, work_item_id: 'str | None' = None, prepared_result: 'Mapping[str, Any] | None' = None) -> 'tuple[dict[str, Any], bool]'",
    "select_owned_file_corpus": "(self, user_id: 'str', uploaded_by: 'str', *, received_since: 'str | None' = None, received_until: 'str | None' = None, document_since: 'str | None' = None, document_until: 'str | None' = None, limit: 'int' = 13, offset: 'int' = 0) -> 'dict[str, Any]'",
    "search_owned_file_content": "(self, user_id: 'str', uploaded_by: 'str', query: 'str', *, limit: 'int' = 64) -> 'dict[str, Any]'",
    "search_owned_files_by_term": "(self, user_id: 'str', uploaded_by: 'str', query: 'str', *, limit: 'int' = 64) -> 'dict[str, Any]'",
    "search_raw_objects_in_set": "(self, user_id: 'str', query: 'str', raw_ids: 'list[str]', *, limit: 'int' = 64) -> 'list[dict[str, Any]]'",
    "list_documents_with_entity_suggestions": "(self, user_id: 'str', *, limit: 'int' = 50, offset: 'int' = 0) -> 'tuple[list[dict[str, Any]], int]'",
    "restore_knowledge_version": "(self, ko_id: 'str', user_id: 'str', version: 'int', *, reviewed_by: 'str | None' = None) -> 'dict[str, Any] | None'",
    "relativize_stored_paths": "(self, files_root: 'str') -> 'dict[str, int]'",
    "arrivals_without_an_author": "(self, user_id: 'str', since: 'str | None' = None, until: 'str | None' = None, *, files_only: 'bool' = False) -> 'int'",
    "backfill_entity_mentions": "(self, user_id: 'str', *, max_documents: 'int' = 200, max_seconds: 'float' = 15.0, max_links: 'int' = 50) -> 'dict[str, Any]'",
    "get_knowledge_conflict_by_pair": "(self, user_id: 'str', pair_key: 'str', conflict_type: 'str') -> 'dict[str, Any]'",
    "_inbox_group_key": "(row: 'dict[str, Any]', by: 'str') -> 'str'",
    "group_pending_inbox": "(self, user_id: 'str', *, by: 'str' = 'extension', limit_ids: 'int' = 200, max_groups: 'int' = 100) -> 'dict[str, Any]'",
    "count_events": "(self) -> 'int'",
    "list_events": "(self, *, event_type: 'str | None' = None, since: 'str | None' = None, limit: 'int' = 100) -> 'list[dict[str, Any]]'",
    "record_event": "(self, event_type: 'str', payload: 'dict[str, Any] | None' = None) -> 'str'",
    "_ensure_schema": "(self, conn: 'sqlite3.Connection') -> 'None'",
    "_execute_statements": "(conn: 'sqlite3.Connection', script: 'str') -> 'None'",
    "_is_sqlite_busy": "(exc: 'sqlite3.OperationalError') -> 'bool'",
    "_ko_snapshot": "(obj: 'KnowledgeObject | dict[str, Any]') -> 'dict[str, Any]'",
    "_migrate_legacy_schema": "(self, conn: 'sqlite3.Connection') -> 'None'",
    "_observe_relation_history_boundary": "(self, boundary: 'str') -> 'None'",
    "_retire_outdated_indexes": "(self, conn: 'sqlite3.Connection') -> 'None'",
    "_migrate_schema": "(self, conn: 'sqlite3.Connection') -> 'None'",
    "_validate_relation_history_schema": "(conn: 'sqlite3.Connection', schema_version: 'int') -> 'None'",
    "_validate_file_source_alias_schema": "(conn: 'sqlite3.Connection') -> 'None'",
    "_open": "(self) -> 'sqlite3.Connection'",
    "_open_once": "(self) -> 'sqlite3.Connection'",
    "_raw_from_row": "(self, row: 'sqlite3.Row | dict[str, Any]') -> 'RawObject'",
    "_resolution_from_row": "(row: 'sqlite3.Row | dict[str, Any]') -> 'EntityResolutionCandidate'",
    "_soft_delete_entity_locked": "(self, entity_id: 'str', user_id: 'str | None') -> 'bool'",
    "_store_entity_version": "(self, conn: 'sqlite3.Connection', row: 'dict[str, Any]') -> 'None'",
    "_store_ko_version": "(self, conn: 'sqlite3.Connection', row: 'dict[str, Any]') -> 'None'",
    "_table_columns": "(conn: 'sqlite3.Connection', table: 'str') -> 'set[str]'",
    "_verify_backup_conn": "(self, backup_conn: 'sqlite3.Connection') -> 'tuple[str, list[Any], int]'",
    "add_eval_case": "(self, user_id: 'str', query: 'str', expected_ids: 'Sequence[str]', *, note: 'str' = '', source: 'str' = 'manual') -> 'dict[str, Any]'",
    "archive_conversation": "(self, conversation_id: 'str', user_id: 'str') -> 'bool'",
    "claim_bridge_nonce": "(self, nonce: 'str') -> 'bool'",
    "claim_inbox_promotion": "(self, inbox_id: 'str', user_id: 'str', knowledge_object_id: 'str') -> 'bool'",
    "clear_channel_conversation": "(self, user_id: 'str', channel: 'str', channel_id: 'str') -> 'bool'",
    "close": "(self, *, final: 'bool' = False) -> 'None'",
    "commit": "(self) -> 'None'",
    "conflict_pair_key": "(knowledge_a_id: 'str', knowledge_b_id: 'str') -> 'str'",
    "conn": "property",
    "count_chunked_knowledge_objects": "(self, user_id: 'str | None' = None) -> 'int'",
    "count_knowledge_chunk_embeddings": "(self, user_id: 'str | None' = None) -> 'int'",
    "count_knowledge_embeddings": "(self, user_id: 'str | None' = None) -> 'int'",
    "count_chat_feed": "(self) -> 'int'",
    "count_conversations": "(self, user_id: 'str', *, include_archived: 'bool' = False) -> 'int'",
    "count_events_in_range": "(self, user_id: 'str', *, start: 'str | None' = None, end: 'str | None' = None, mine: 'str' = '') -> 'int'",
    "count_relation_changes_in_range": "(self, user_id: 'str', *, start: 'str | None' = None, end: 'str | None' = None) -> 'int'",
    "count_entities_by_type": "(self, user_id: 'str', *, include_merged: 'bool' = False) -> 'dict[str, int]'",
    "count_entity_knowledge": "(self, user_id: 'str', entity_id: 'str') -> 'int'",
    "count_entity_relations": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'int'",
    "count_feedback_state": "(self, user_id: 'str', *, target_type: 'str | None' = None, target_id: 'str | None' = None, feedback_type: 'str | None' = None, negative_only: 'bool' = False) -> 'int'",
    "count_knowledge_objects": "(self, user_id: 'str', *, uploaded_by: 'str | None' = None) -> 'int'",
    "count_visible_raw_objects": "(self, user_id: 'str', *, files_only: 'bool' = False) -> 'int'",
    "count_missions": "(self, user_id: 'str', *, statuses: 'Sequence[str] | None' = None) -> 'int'",
    "count_recent_audit": "(self, action: 'str', since: 'str', *, limit: 'int | None' = None) -> 'int'",
    "count_user_vectors": "(self, user_id: 'str', model: 'str', *, before: 'tuple[str, str] | None' = None) -> 'int'",
    "create_api_token": "(self, user_id: 'str', token_sha256: 'str', *, label: 'str' = '', created_by: 'str' = '', ttl_seconds: 'int | None' = None) -> 'dict[str, Any]'",
    "create_backup": "(self, *, label: 'str' = 'manual') -> 'dict[str, Any]'",
    "create_conversation": "(self, user_id: 'str', title: 'str' = '', *, mode: 'str' = 'dialogue') -> 'dict[str, Any]'",
    "create_entity": "(self, entity: 'Entity') -> 'Entity'",
    "create_mission": "(self, mission: 'Mission') -> 'dict[str, Any]'",
    "create_mission_unless_twin": "(self, mission: 'Mission', *, statuses: 'Sequence[str]', since: 'str') -> 'tuple[dict[str, Any], bool]'",
    "create_relation": "(self, relation: 'Relation') -> 'Relation'",
    "invalidate_relation": (
        "(self, user_id: 'str', relation_id: 'str', *, valid_to: 'str' = '', "
        "superseded_by: 'str' = '', reason: 'str' = '') -> 'dict[str, Any] | None'"
    ),
    "delete_conversation": "(self, conversation_id: 'str', user_id: 'str') -> 'dict[str, Any]'",
    "delete_entity_time": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'bool'",
    "delete_eval_case": "(self, user_id: 'str', case_id: 'str') -> 'bool'",
    "delete_knowledge_embedding": "(self, knowledge_object_id: 'str') -> 'None'",
    "diagnostics": "(self) -> 'dict[str, Any]'",
    "discard_notifications": "(self, ids: 'Sequence[str]', *, reason: 'str') -> 'int'",
    "diff_knowledge_versions": "(self, ko_id: 'str', user_id: 'str', *, from_version: 'int | None' = None, to_version: 'int | None' = None) -> 'dict[str, Any] | None'",
    "enqueue_notification": "(self, user_id: 'str', chat_id: 'str', body: 'str', *, kind: 'str' = '', dedup_key: 'str' = '') -> 'bool'",
    "ensure_user": "(self, user_id: 'str', *, source: 'str' = 'local', external_id: 'str' = '', display_name: 'str' = '', username: 'str' = '', preset_key: 'str' = 'user', metadata: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "eval_case_health": "(self, user_id: 'str', *, cases: 'list[dict[str, Any]] | None' = None) -> 'dict[str, Any]'",
    "execute": "(self, sql: 'str', params: 'tuple | dict | None' = None) -> 'sqlite3.Cursor'",
    "export_user": "(self, user_id: 'str') -> 'dict[str, Any]'",
    "files_without_an_author": "(self) -> 'int'",
    "find_api_token": "(self, token_sha256: 'str') -> 'dict[str, Any] | None'",
    "find_duplicate_candidates": "(self, user_id: 'str', *, min_confidence: 'float' = 0.5) -> 'list[EntityResolutionCandidate]'",
    "find_entities_by_normalized_names": "(self, user_id: 'str', names: 'Sequence[str]', *, include_aliases: 'bool' = True, limit: 'int' = 800) -> 'list[dict[str, Any]]'",
    "find_entity_by_alias": "(self, user_id: 'str', alias: 'str', *, limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "find_entity_by_name": "(self, user_id: 'str', name: 'str') -> 'dict[str, Any] | None'",
    "iter_entities": "(self, user_id: 'str', entity_type: 'EntityType | None' = None, *, page_size: 'int' = 1000, include_merged: 'bool' = False) -> 'Iterator[dict[str, Any]]'",
    "find_inbox_by_raw": "(self, raw_object_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "find_raw_by_source_ref": "(self, user_id: 'str', source: 'str', source_ref: 'str') -> 'dict[str, Any] | None'",
    "find_fresh_agent_candidate": "(self, user_id: 'str', source: 'str', candidate_type: 'str', content_hash: 'str', *, requested_by: 'str' = '', since: 'str' = '') -> 'dict[str, Any] | None'",
    "get_api_token": "(self, token_id: 'str') -> 'dict[str, Any] | None'",
    "get_channel_conversation": "(self, user_id: 'str', channel: 'str', channel_id: 'str') -> 'str | None'",
    "get_channel_session": "(self, user_id: 'str', channel: 'str', channel_id: 'str') -> 'dict[str, Any] | None'",
    "get_chunk_spans": "(self, user_id: 'str', model: 'str', keys: 'Sequence[tuple[str, int]]', *, uploaded_by: 'str | None' = None) -> 'dict[tuple[str, int], tuple[int, int]]'",
    "get_conflict_pair_statuses": "(self, user_id: 'str', conflict_type: 'str') -> 'dict[str, str]'",
    "get_conversation": "(self, conversation_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_conversation_messages": "(self, conversation_id: 'str', *, user_id: 'str', limit: 'int' = 50, offset: 'int | None' = None) -> 'list[dict[str, Any]]'",
    "get_current_feedback_stats": "(self, user_id: 'str', target_type: 'str | None' = None) -> 'dict[str, Any]'",
    "get_custom_preset": "(self, preset_key: 'str') -> 'dict[str, Any] | None'",
    "get_entity": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'dict[str, Any] | None'",
    # `include_cooccurrence` добавлен в 0.170.0 и объявлен здесь осознанно: у
    # окрестности узла появился второй род рёбер — совместная встречаемость, та же,
    # что рисует общая картина. Умолчание `False` держит агента и публичный
    # маршрут на прежнем ответе; соседство в концентраторе замерено как не-улика.
    "get_entity_graph": "(self, user_id: 'str', entity_id: 'str', depth: 'int' = 2, *, as_of: 'str' = '', entity_types: 'Sequence[str]' = (), relation_types: 'Sequence[str]' = (), min_weight: 'float' = 0.0, min_confidence: 'float' = 0.0, known_at: 'str' = '', include_cooccurrence: 'bool' = False) -> 'dict[str, Any]'",
    "get_entity_knowledge": "(self, user_id: 'str', entity_id: 'str', *, limit: 'int' = 50) -> 'list[dict[str, Any]]'",
    "get_entity_relations": (
        "(self, entity_id: 'str', user_id: 'str | None' = None, *, "
        "include_invalidated: 'bool' = False, as_of: 'str' = '', known_at: 'str' = '') -> 'list[dict[str, Any]]'"
    ),
    "graph_overview": "(self, user_id: 'str', *, limit: 'int' = 120, entity_types: 'Sequence[str] | None' = None, relation_types: 'Sequence[str] | None' = None, only_relations: 'bool' = False, min_weight: 'int' = 1, min_confidence: 'float' = 0.0, as_of: 'str' = '', known_at: 'str' = '', search: 'str' = '', hide_isolates: 'bool' = False) -> 'dict[str, Any]'",
    "relation_history_status": "(self, user_id: 'str', known_at: 'str' = '') -> 'dict[str, Any]'",
    "get_entity_time": "(self, entity_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_feedback_for_target": "(self, user_id: 'str', target_type: 'str', target_id: 'str') -> 'list[dict[str, Any]]'",
    "get_feedback_state": "(self, user_id: 'str', *, target_type: 'str | None' = None, target_id: 'str | None' = None, feedback_type: 'str | None' = None, limit: 'int' = 1000) -> 'list[dict[str, Any]]'",
    "get_feedback_stats": "(self, user_id: 'str', target_type: 'str | None' = None) -> 'dict[str, Any]'",
    "get_inbox_by_raw": "(self, raw_object_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_inbox_item": "(self, inbox_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_knowledge_by_raw": "(self, raw_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_knowledge_conflict": "(self, user_id: 'str', conflict_id: 'str') -> 'dict[str, Any] | None'",
    "get_knowledge_object": "(self, ko_id: 'str', user_id: 'str | None' = None, *, uploaded_by: 'str | None' = None) -> 'dict[str, Any] | None'",
    "get_knowledge_usage": "(self, user_id: 'str', knowledge_object_ids: 'list[str]') -> 'dict[str, dict[str, Any]]'",
    "get_lifecycle_stats": "(self, user_id: 'str') -> 'dict[str, int]'",
    "get_message": "(self, message_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_mission": "(self, mission_id: 'str', user_id: 'str | None' = None, *, created_by: 'str | None' = None) -> 'dict[str, Any] | None'",
    "get_mission_tasks": "(self, mission_id: 'str', user_id: 'str | None' = None) -> 'list[dict[str, Any]]'",
    "get_permission_overrides": "(self, user_id: 'str') -> 'dict[str, str]'",
    "get_raw_object": "(self, raw_id: 'str', user_id: 'str | None' = None) -> 'dict[str, Any] | None'",
    "get_relation_candidate": "(self, user_id: 'str', candidate_id: 'str') -> 'dict[str, Any] | None'",
    "get_resolution_candidate": "(self, candidate_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_vectors_by_content_hash": "(self, content_hashes: 'Sequence[str]', model: 'str') -> 'dict[str, bytes]'",
    "get_reusable_vectors": "(self, knowledge_object_ids: 'Sequence[str]', model: 'str') -> 'dict[str, dict[str, bytes]]'",
    "get_user": "(self, user_id: 'str') -> 'dict[str, Any] | None'",
    "get_user_chunk_embeddings": "(self, user_id: 'str', model: 'str', dim: 'int', *, object_limit: 'int | None' = None, row_limit: 'int | None' = None, uploaded_by: 'str | None' = None) -> 'list[tuple[str, bytes]]'",
    "get_user_embeddings": "(self, user_id: 'str', model: 'str', dim: 'int', *, limit: 'int | None' = None, uploaded_by: 'str | None' = None) -> 'list[tuple[str, bytes]]'",
    "idempotency_claim": "(self, user_id: 'str', request_key: 'str', *, request_hash: 'str' = '', lease_seconds: 'int' = 300) -> 'dict[str, Any]'",
    "idempotency_complete": "(self, user_id: 'str', request_key: 'str', lease_token: 'str', response: 'dict[str, Any]') -> 'bool'",
    "idempotency_get": "(self, user_id: 'str', request_key: 'str', *, request_hash: 'str' = '') -> 'dict[str, Any] | None'",
    "idempotency_mark_effect_possible": "(self, user_id: 'str', request_key: 'str', lease_token: 'str', response: 'dict[str, Any]') -> 'bool'",
    "idempotency_prune": "(self, *, days: 'int' = 30) -> 'int'",
    "idempotency_release": "(self, user_id: 'str', request_key: 'str', lease_token: 'str') -> 'bool'",
    "idempotency_renew": "(self, user_id: 'str', request_key: 'str', lease_token: 'str') -> 'bool'",
    "idempotency_store": "(self, user_id: 'str', request_key: 'str', response: 'dict[str, Any]', *, request_hash: 'str' = '') -> 'None'",
    "kv_delete": "(self, key: 'str') -> 'None'",
    "kv_get": "(self, key: 'str') -> 'str | None'",
    "kv_list_prefix": "(self, prefix: 'str') -> 'list[dict[str, Any]]'",
    "kv_set": "(self, key: 'str', value: 'str') -> 'None'",
    "known_vocabulary": "(self, terms: 'Sequence[str]') -> 'set[str]'",
    "link_knowledge_entity": "(self, user_id: 'str', knowledge_object_id: 'str', entity_id: 'str', *, status: 'str' = 'accepted', confidence: 'float' = 1.0, evidence: 'dict[str, Any] | None' = None, reviewed_by: 'str | None' = None) -> 'dict[str, Any]'",
    "list_api_tokens": "(self, user_id: 'str | None' = None, *, include_revoked: 'bool' = False) -> 'list[dict[str, Any]]'",
    "list_audit_log": "(self, user_id: 'str | None' = None, *, limit: 'int' = 100, offset: 'int' = 0, before: 'str | None' = None) -> 'list[dict[str, Any]]'",
    "list_backups": "(self, *, limit: 'int | None' = None) -> 'list[dict[str, Any]]'",
    "list_chat_thread": "(self, user_id: 'str', *, limit: 'int' = 500) -> 'dict[str, Any]'",
    "list_container_entities": "(self, user_id: 'str', types: 'tuple[str, ...]') -> 'list[dict[str, Any]]'",
    "list_conversations": "(self, user_id: 'str', *, include_archived: 'bool' = False, limit: 'int' = 200, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_custom_presets": "(self) -> 'list[dict[str, Any]]'",
    "list_entities": "(self, user_id: 'str', entity_type: 'EntityType | None' = None, *, limit: 'int' = 100, offset: 'int' = 0, include_merged: 'bool' = False) -> 'list[dict[str, Any]]'",
    "list_entities_by_activity": "(self, user_id: 'str', *, types: 'tuple[str, ...] | None' = None, limit: 'int' = 5) -> 'list[dict[str, Any]]'",
    "list_entities_knowledge_refs": "(self, user_id: 'str', entity_ids: 'Sequence[str]', *, limit: 'int' = 50) -> 'dict[str, list[dict[str, Any]]]'",
    "list_entity_versions": "(self, entity_id: 'str', user_id: 'str') -> 'list[dict[str, Any]]'",
    "list_eval_cases": "(self, user_id: 'str', *, limit: 'int' = 1000) -> 'list[dict[str, Any]]'",
    "list_events_in_range": "(self, user_id: 'str', *, start: 'str | None' = None, end: 'str | None' = None, limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_relation_changes_in_range": "(self, user_id: 'str', *, start: 'str | None' = None, end: 'str | None' = None, limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_inbox": "(self, user_id: 'str', status: 'InboxStatus | None' = None, *, limit: 'int' = 50, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_inbox_detailed": "(self, user_id: 'str', status: 'InboxStatus | None' = None, *, limit: 'int' = 50, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_knowledge_conflicts": "(self, user_id: 'str', *, status: 'str | None' = 'suggested', limit: 'int' = 200, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_knowledge_entity_links_for": "(self, knowledge_ids: 'Sequence[str]') -> 'dict[str, list[str]]'",
    "list_knowledge_entity_links": "(self, user_id: 'str', *, entity_id: 'str | None' = None, knowledge_object_id: 'str | None' = None, status: 'str | None' = 'accepted', limit: 'int' = 100) -> 'list[dict[str, Any]]'",
    "list_knowledge_missing_embedding": "(self, model: 'str', *, limit: 'int' = 64, chunk_scheme: 'str' = '', chunk_threshold: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_live_knowledge_ids": "(self, user_id: 'str') -> 'set[str]'",
    "list_knowledge_objects": "(self, user_id: 'str', *, limit: 'int' = 100, offset: 'int' = 0, lifecycle_stage: 'str | None' = None, tag: 'str | None' = None, entity_id: 'str | None' = None, query: 'str | None' = None, since: 'str | None' = None, until: 'str | None' = None, uploaded_by: 'str | None' = None) -> 'list[dict[str, Any]]'",
    # +`utc_offset_minutes`: годовщина считается в сутках ЧЕЛОВЕКА. День приходил
    # из местного времени, а `created_at` лежит в UTC — две разные шкалы.
    "list_knowledge_on_this_day": "(self, user_id: 'str', *, month_day: 'str', before_iso: 'str', limit: 'int' = 10, utc_offset_minutes: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_knowledge_tags": "(self, user_id: 'str', *, limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_knowledge_versions": "(self, ko_id: 'str', user_id: 'str') -> 'list[dict[str, Any]]'",
    "list_lifecycle_candidates": "(self, user_id: 'str', *, days_threshold: 'int' = 90, limit: 'int' = 500, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "get_merge_history": "(self, merge_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "list_merge_history": "(self, user_id: 'str', *, limit: 'int' = 100) -> 'list[dict[str, Any]]'",
    "list_missions": "(self, user_id: 'str | None' = None, *, status: 'MissionStatus | str | None' = None, statuses: 'Sequence[str] | None' = None, limit: 'int' = 50, offset: 'int' = 0, created_by: 'str | None' = None) -> 'list[dict[str, Any]]'",
    "list_part_of_relations": "(self, user_id: 'str') -> 'list[dict[str, Any]]'",
    "list_pending_notifications": "(self, *, limit: 'int' = 20, max_attempts: 'int' = 5) -> 'list[dict[str, Any]]'",
    "list_purgeable_knowledge": "(self, user_id: 'str | None' = None, *, older_than_days: 'int' = 30, limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_recent_knowledge": "(self, user_id: 'str', *, since_iso: 'str', limit: 'int' = 10) -> 'list[dict[str, Any]]'",
    "list_relation_candidates": "(self, user_id: 'str', *, status: 'str | None' = 'suggested', limit: 'int' = 200, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_resolution_candidates": "(self, user_id: 'str', status: 'ResolutionStatus | None' = None, *, limit: 'int' = 500, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_user_ids": "(self, *, active_only: 'bool' = True) -> 'list[str]'",
    "list_user_vectors_page": "(self, user_id: 'str', model: 'str', *, after: 'tuple[str, str] | None' = None, before: 'tuple[str, str] | None' = None, max_updated_at: 'str | None' = None, descending: 'bool' = False, limit: 'int' = 2048) -> 'list[tuple[str, str, bytes]]'",
    "list_users": "(self, *, limit: 'int' = 500, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "log_audit": "(self, entry: 'AuditEntry') -> 'AuditEntry'",
    "mark_notifications": "(self, sent_ids: 'Sequence[str]' = (), failed_ids: 'Sequence[str]' = (), *, max_attempts: 'int' = 5) -> 'None'",
    "merge_entities": "(self, user_id: 'str', source_id: 'str', target_id: 'str', *, merged_by: 'str | None' = None) -> 'dict[str, Any]'",
    "unmerge_entities": "(self, user_id: 'str', merge_id: 'str', *, undone_by: 'str | None' = None) -> 'dict[str, Any]'",
    "optimize": "(self) -> 'None'",
    "people_whose_name_starts_with": "(self, user_id: 'str', stems: 'Sequence[str]', *, limit: 'int' = 5) -> 'list[str]'",
    "prune_bridge_nonces": "(self, *, max_age_sec: 'int') -> 'int'",
    "prune_eval_cases": "(self, user_id: 'str', *, cap: 'int' = 200) -> 'dict[str, int]'",
    "prune_backups": "(self, *, keep: 'int') -> 'dict[str, Any]'",
    "purge_knowledge_object": "(self, ko_id: 'str', user_id: 'str | None' = None, *, require_soft_deleted: 'bool' = True) -> 'dict[str, Any]'",
    "record_knowledge_usage": "(self, user_id: 'str', knowledge_object_ids: 'list[str]', *, retrieved: 'bool' = False, used_in_answer: 'bool' = False) -> 'int'",
    "resolve_candidate": "(self, candidate_id: 'str', status: 'ResolutionStatus', resolved_by: 'str | None' = None, *, user_id: 'str | None' = None) -> 'bool'",
    "resolve_conflict": "(self, user_id: 'str', conflict_id: 'str', winner_id: 'str', *, reviewed_by: 'str', resolution_note: 'str' = '') -> 'dict[str, Any] | None'",
    "restore_backup": "(self, filename: 'str', *, safety_label: 'str' = 'pre-restore') -> 'dict[str, Any]'",
    "review_knowledge_conflict": "(self, user_id: 'str', conflict_id: 'str', status: 'str', *, reviewed_by: 'str', resolution_note: 'str' = '') -> 'dict[str, Any] | None'",
    "resolve_owned_file_source_ref": "(self, user_id: 'str', uploaded_by: 'str', source_ref: 'str') -> 'str | None'",
    "review_relation_candidate": "(self, user_id: 'str', candidate_id: 'str', status: 'str', *, reviewed_by: 'str') -> 'dict[str, Any] | None'",
    "revoke_api_token": "(self, token_id: 'str', *, user_id: 'str | None' = None) -> 'bool'",
    "search_raw_objects": "(self, user_id: 'str', query: 'str', *, limit: 'int' = 20, include_content: 'bool' = False, uploaded_by: 'str | None' = None) -> 'list[dict[str, Any]]'",
    "search_knowledge": "(self, user_id: 'str', query: 'str', *, limit: 'int' = 20, uploaded_by: 'str | None' = None) -> 'list[dict[str, Any]]'",
    "search_messages": "(self, user_id: 'str', query: 'str', *, limit: 'int' = 20, conversation_id: 'str | None' = None, role: 'str | None' = None, before_message_id: 'str | None' = None, match_all_terms: 'bool' = False) -> 'list[dict[str, Any]]'",
    "list_messages_window": "(self, user_id: 'str', since: 'str', until: 'str', *, role: 'str | None' = None, conversation_id: 'str | None' = None, before_message_id: 'str | None' = None, limit: 'int' = 50, offset: 'int' = 0) -> 'dict[str, Any]'",
    "set_channel_conversation": "(self, user_id: 'str', channel: 'str', channel_id: 'str', conversation_id: 'str', *, mode: 'str | None' = None) -> 'None'",
    "set_channel_mode": "(self, user_id: 'str', channel: 'str', channel_id: 'str', mode: 'str') -> 'dict[str, Any] | None'",
    "set_conversation_archived": "(self, conversation_id: 'str', user_id: 'str', archived: 'bool') -> 'dict[str, Any] | None'",
    "set_conversation_mode": "(self, conversation_id: 'str', user_id: 'str', mode: 'str') -> 'dict[str, Any] | None'",
    "set_conversation_title": "(self, conversation_id: 'str', user_id: 'str', title: 'str') -> 'dict[str, Any] | None'",
    "set_entity_time": "(self, entity_id: 'str', user_id: 'str', occurred_at: 'str', *, occurred_end: 'str | None' = None, precision: 'str' = 'day', source: 'str' = '') -> 'dict[str, Any]'",
    "set_knowledge_entity_link_status": "(self, link_id: 'str', user_id: 'str', status: 'str', *, reviewed_by: 'str') -> 'dict[str, Any] | None'",
    "set_mission_plan": "(self, mission_id: 'str', user_id: 'str', tasks: 'list[MissionTask]', *, plan_summary: 'str', status: 'MissionStatus | str') -> 'dict[str, Any] | None'",
    "set_permission_override": "(self, user_id: 'str', security_id: 'str', effect: 'str | None') -> 'None'",
    "soft_delete_entity": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'bool'",
    "soft_delete_knowledge_object": "(self, ko_id: 'str', user_id: 'str | None' = None) -> 'bool'",
    "store_feedback": "(self, feedback: 'FeedbackItem') -> 'FeedbackItem'",
    "store_inbox_item": "(self, item: 'InboxItem') -> 'InboxItem'",
    "store_knowledge_conflict": "(self, user_id: 'str', knowledge_a_id: 'str', knowledge_b_id: 'str', *, conflict_type: 'str' = 'potential_contradiction', confidence: 'float', evidence: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "store_knowledge_object": "(self, obj: 'KnowledgeObject') -> 'KnowledgeObject'",
    "store_message": "(self, conversation_id: 'str', user_id: 'str', role: 'str', content: 'str', metadata: 'dict[str, Any] | None' = None, reply_to: 'str | None' = None) -> 'dict[str, Any]'",
    "store_raw_object": "(self, obj: 'RawObject') -> 'RawObject'",
    "store_relation_candidate": "(self, user_id: 'str', source_entity_id: 'str', target_entity_id: 'str', relation_type: 'str', *, confidence: 'float', evidence: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "store_resolution_candidate": "(self, candidate: 'EntityResolutionCandidate') -> 'EntityResolutionCandidate'",
    "touch_api_token": "(self, token_id: 'str') -> 'None'",
    "transaction": "(self) -> 'Iterator[sqlite3.Connection]'",
    "update_entity": "(self, entity: 'Entity') -> 'Entity'",
    "update_inbox_status": "(self, inbox_id: 'str', status: 'InboxStatus', reviewed_by: 'str | None' = None, *, user_id: 'str | None' = None, suggested_entity_id: 'str | None' = None, suggested_tags: 'list[str] | None' = None, suggestions: 'dict[str, Any] | None' = None, suggested_action: 'str | None' = None, knowledge_object_id: 'str | None' = None, clear_knowledge_object_id: 'bool' = False, promotion_score: 'float | None' = None, quality_score: 'float | None' = None, notes: 'str | None' = None) -> 'bool'",
    "update_inbox_suggestions": "(self, inbox_id: 'str', user_id: 'str', *, suggestions: 'dict[str, Any]', suggested_tags: 'list[str] | None' = None, suggested_action: 'str | None' = None, promotion_score: 'float | None' = None, quality_score: 'float | None' = None, classification_notes: 'str | None' = None) -> 'bool'",
    "update_knowledge_fields": "(self, ko_id: 'str', user_id: 'str', **fields: 'Any') -> 'dict[str, Any] | None'",
    "update_knowledge_object": "(self, obj: 'KnowledgeObject') -> 'KnowledgeObject'",
    "update_mission_fields": "(self, mission_id: 'str', user_id: 'str', **fields: 'Any') -> 'bool'",
    "update_mission_task_fields": "(self, task_id: 'str', user_id: 'str', **fields: 'Any') -> 'bool'",
    "update_user": "(self, user_id: 'str', **fields: 'Any') -> 'dict[str, Any] | None'",
    "upsert_custom_preset": "(self, preset_key: 'str', name: 'str', capabilities: 'set[str]', *, description: 'str' = '', created_by: 'str') -> 'dict[str, Any]'",
    "upsert_feedback_eval_case": "(self, user_id: 'str', query: 'str', expected_ids: 'Sequence[str]') -> 'bool'",
    "upsert_knowledge_embeddings": "(self, items: 'Sequence[dict[str, Any]]') -> 'int'",
    "upsert_knowledge_vectors": "(self, items: 'Sequence[dict[str, Any]]', chunks: 'Mapping[str, Sequence[dict[str, Any]]] | None' = None) -> 'dict[str, int]'",
    "vocabulary_terms": "(self, prefixes: 'Sequence[str]', *, limit: 'int' = 400) -> 'list[str]'",
    "verify_backup": "(self, filename: 'str') -> 'dict[str, Any]'",
}


def _plan(storage, sql: str, params: tuple) -> list[tuple[int, str]]:
    return [
        (int(row["parent"]), str(row["detail"]))
        for row in storage.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    ]


def _index_names(plan: list[tuple[int, str]]) -> set[str]:
    """Every index the plan actually names, so a prefix cannot pass for the whole."""
    return set(re.findall(r"USING (?:COVERING )?INDEX (\w+)", " ".join(row[1] for row in plan)))


def _captured_sql(storage, call) -> tuple[str, tuple]:
    """Run a storage method and return the last SQL it executed, verbatim.

    Lets a plan test pin what the code runs instead of a copy that silently
    stops matching it.
    """
    seen: list[tuple[str, tuple]] = []
    original = storage.execute

    def recording(sql: str, params: tuple | dict | None = None):
        seen.append((sql, tuple(params or ())))
        return original(sql, params)

    storage.execute = recording  # type: ignore[method-assign]
    try:
        call()
    finally:
        del storage.execute
    selects = [pair for pair in seen if pair[0].lstrip().upper().startswith("SELECT")]
    assert selects, "метод не выполнил ни одного SELECT — нечего закреплять"
    return selects[-1]


def test_the_hot_read_paths_are_index_ordered(storage):
    """A sort the index cannot serve is a temp b-tree over the whole tenant.

    Both of these are per-search. Measured on a synthetic 10k-object corpus before
    the indexes existed: the recall pool page took **90.9 ms** and the dense-vector
    window **469 ms**, each building a temp b-tree first. After: 1.6 ms and 125 ms,
    and `count_knowledge_objects` — which the capped-pool signal now calls — went
    66.2 ms to 0.2 ms because the partial index covers it.

    `idx_knowledge_user_quality` was already there and starts with `user_id`, which
    is exactly why this hid: SQLite used it to FIND the rows and then sorted every
    one of them, because `importance` is its fourth column and orders nothing here.
    """
    from friday.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("owner")
    for index in range(200):
        raw = RawObject(
            id=new_id("raw"),
            user_id="owner",
            source="test",
            source_ref=new_id("src"),
            raw_content=f"заметка {index}",
            content_type="text",
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="owner",
                raw_object_id=raw.id,
                content=raw.raw_content,
                title=f"Заметка {index}",
                summary=raw.raw_content,
                importance=index / 200,
            )
        )
    # Deliberately no ANALYZE: with statistics for a 200-row table SQLite decides a
    # temp sort is cheaper than a second index, which is true at 200 rows and false
    # at the scale this test exists for. The plan under default assumptions is the
    # one that matters.

    # The SQL is taken FROM the method, not retyped here. A hardcoded copy keeps
    # passing after the real query changes — it would have gone on pinning a plan
    # for a query nobody runs when `id DESC` was added to the ORDER BY.
    sql, params = _captured_sql(storage, lambda: storage.list_knowledge_objects("owner", limit=400))
    pool = _plan(storage, sql, params)
    # Privacy predicates contain bounded DISTINCT identity-token subqueries. Their
    # local de-duplication is intentionally a temp b-tree and says nothing about
    # whether the outer tenant page was sorted.  This regression protects the
    # expensive top-level ORDER BY only.
    assert not [
        detail for parent, detail in pool if parent == 0 and "TEMP B-TREE" in detail and "ORDER BY" in detail
    ], pool
    # Exact name, not a substring: `idx_knowledge_user_importance` is a prefix of
    # every index that could replace it, so `in line` passes vacuously on the very
    # change this assertion exists to notice.
    assert "idx_knowledge_pool_order" in _index_names(pool), pool

    sql, params = _captured_sql(storage, lambda: storage.get_user_embeddings("owner", "m", 4, limit=100))
    vectors = _plan(storage, sql, params)
    assert "INDEXED BY idx_knowledge_chunk_scan_order" in sql
    assert "ORDER BY window_k.created_at DESC, window_k.id ASC" in sql
    assert not [
        detail
        for parent, detail in vectors
        if parent == 0 and "TEMP B-TREE" in detail and "ORDER BY" in detail
    ], vectors
    assert any("idx_knowledge_chunk_scan_order" in detail for _, detail in vectors), vectors


def test_the_capped_vector_window_still_returns_the_newest_object(storage):
    """The rewrite must keep 'newest N', not just 'N'.

    Choosing the window in a subquery is what lets the LIMIT short-circuit; it also
    moves the ORDER BY off the outer result, so the property that actually matters
    is pinned here rather than implied by a plan.
    """
    from friday.retrieval import pack_vector
    from friday.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("owner")
    ids = []
    for index in range(5):
        raw = RawObject(
            id=new_id("raw"),
            user_id="owner",
            source="test",
            source_ref=new_id("src"),
            raw_content=f"вектор {index}",
            content_type="text",
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            id=new_id("ko"),
            user_id="owner",
            raw_object_id=raw.id,
            content=raw.raw_content,
            title=f"K{index}",
            summary=raw.raw_content,
            created_at=f"2026-0{index + 1}-01T00:00:00Z",
        )
        storage.store_knowledge_object(ko)
        storage.upsert_knowledge_embeddings(
            [
                {
                    "knowledge_object_id": ko.id,
                    "user_id": "owner",
                    "model": "m",
                    "dim": 4,
                    "source_version": 1,
                    "content_hash": ko.id,
                    "vector": pack_vector([float(index), 0.0, 0.0, 0.0]),
                }
            ]
        )
        ids.append(ko.id)

    assert [row[0] for row in storage.get_user_embeddings("owner", "m", 4, limit=1)] == [ids[-1]]
    assert len(storage.get_user_embeddings("owner", "m", 4, limit=3)) == 3
    assert len(storage.get_user_embeddings("owner", "m", 4)) == 5


def _seed_chunk_scan(
    storage, *, user_id: str = "owner", objects: int = 8, chunks_per_object: int = 4
) -> list[str]:
    from friday.retrieval import pack_vector
    from friday.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user(user_id)
    ids: list[str] = []
    vectors = []
    chunks = {}
    for object_index in range(objects):
        raw = RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="test",
            source_ref=new_id("src"),
            raw_content=f"чанковый объект {object_index}",
            content_type="text",
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            # Reverse lexical vs insertion order so equal-timestamp tests detect a
            # regression from the declared id tie-break back to incidental rowid.
            id=f"ko_scan_{user_id}_{objects - object_index:04d}",
            user_id=user_id,
            raw_object_id=raw.id,
            content=raw.raw_content,
            title=f"K{object_index}",
            summary=raw.raw_content,
            created_at=f"2026-01-{object_index + 1:02d}T00:00:00Z",
        )
        storage.store_knowledge_object(ko)
        ids.append(ko.id)
        vectors.append(
            {
                "knowledge_object_id": ko.id,
                "user_id": user_id,
                "model": "m",
                "dim": 4,
                "source_version": 1,
                "content_hash": ko.id,
                "vector": pack_vector([float(object_index + 1), 0.0, 0.0, 0.0]),
            }
        )
        chunks[ko.id] = [
            {
                "chunk_index": chunk_index,
                "user_id": user_id,
                "model": "m",
                "dim": 4,
                "source_version": 1,
                "content_hash": f"{ko.id}:{chunk_index}",
                "vector": pack_vector([float(object_index + 1), float(chunk_index), 0.0, 0.0]),
            }
            for chunk_index in range(chunks_per_object)
        ]
    storage.upsert_knowledge_vectors(vectors, chunks)
    return ids


def test_dense_chunk_vector_scan_uses_parent_order_and_primary_key(storage):
    _seed_chunk_scan(storage)

    sql, params = _captured_sql(
        storage,
        lambda: storage.get_user_chunk_embeddings("owner", "m", 4, object_limit=8, row_limit=33),
    )
    plan = _plan(storage, sql, params)
    details = [detail for _, detail in plan]

    assert "CROSS JOIN knowledge_chunk_embeddings" in sql
    assert "ORDER BY k.created_at DESC, k.id, c.chunk_index" in sql
    assert sql.count("INDEXED BY idx_knowledge_chunk_scan_order") == 2
    assert any("idx_knowledge_chunk_scan_order" in detail for detail in details), plan
    assert any("sqlite_autoindex_knowledge_chunk_embeddings_1" in detail for detail in details), plan
    # SQLite may sort only the final chunk_index term INSIDE one KO.  What must
    # never return is the corpus-wide sort spelled without "LAST TERM".
    assert "USE TEMP B-TREE FOR ORDER BY" not in details, plan


def test_scoped_chunk_plan_prices_the_tenant_index_it_physically_walks(storage):
    _seed_chunk_scan(storage)

    # None of these legacy synthetic rows has this uploader.  Membership is still
    # fail-closed in SQL, but plan selection must price the tenant-wide KO index
    # that the parent-first branch would physically walk.  Replacing live_objects
    # with the scoped author count (zero here) incorrectly selects the sparse plan.
    sql, _ = _captured_sql(
        storage,
        lambda: storage.get_user_chunk_embeddings("owner", "m", 4, uploaded_by="missing-author"),
    )

    assert "CROSS JOIN knowledge_chunk_embeddings" in sql


def test_current_schema_recreates_chunk_order_index(settings, tmp_path):
    from dataclasses import replace

    database = tmp_path / "current-schema-index.sqlite3"
    tuned = replace(settings, database_path=database)
    first = FridayStorage(tuned)
    try:
        first.execute("DROP INDEX idx_knowledge_chunk_scan_order")
        first.commit()
        assert (
            first.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_knowledge_chunk_scan_order'"
            ).fetchone()
            is None
        )
    finally:
        first.close()

    reopened = FridayStorage(tuned)
    try:
        names = {
            str(row["name"])
            for row in reopened.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name IN ('knowledge_objects', 'knowledge_chunk_embeddings')"
            ).fetchall()
        }
        assert "idx_knowledge_chunk_scan_order" in names
        assert "sqlite_autoindex_knowledge_chunk_embeddings_1" in names
    finally:
        reopened.close()


def test_chunk_vector_scan_keeps_legacy_plan_at_density_and_rollover_boundaries(storage):
    ids = _seed_chunk_scan(storage, chunks_per_object=4)

    # Exactly 75% current is intentionally NOT authority for object-first.
    placeholders = ",".join("?" for _ in ids[:2])
    storage.execute(
        f"UPDATE knowledge_chunk_embeddings SET model='retired' "  # nosec B608 - placeholders only
        f"WHERE knowledge_object_id IN ({placeholders})",
        tuple(ids[:2]),
    )
    storage.commit()
    sql, _ = _captured_sql(storage, lambda: storage.get_user_chunk_embeddings("owner", "m", 4))
    assert "FROM knowledge_chunk_embeddings c" in sql
    assert "CROSS JOIN" not in sql

    # Above 75%, with >2 active chunks per object, the indexed-order path opens.
    storage.execute(
        "UPDATE knowledge_chunk_embeddings SET model='m' WHERE knowledge_object_id=?",
        (ids[0],),
    )
    storage.commit()
    sql, _ = _captured_sql(storage, lambda: storage.get_user_chunk_embeddings("owner", "m", 4))
    assert "CROSS JOIN knowledge_chunk_embeddings" in sql

    # Density is a separate strict gate: exactly two active chunks per live object
    # remains legacy even with a 100% current model.
    storage.execute("UPDATE knowledge_chunk_embeddings SET model='m'")
    storage.execute("DELETE FROM knowledge_chunk_embeddings WHERE chunk_index >= 2")
    storage.commit()
    sql, _ = _captured_sql(storage, lambda: storage.get_user_chunk_embeddings("owner", "m", 4))
    assert "FROM knowledge_chunk_embeddings c" in sql
    assert "CROSS JOIN" not in sql


def _reference_chunk_scan(
    storage,
    user_id: str,
    model: str,
    dim: int,
    *,
    object_limit: int | None,
    row_limit: int | None,
) -> list[tuple[str, bytes]]:
    """The pre-optimization selection, with the now-explicit two-sided tenant gate."""
    from friday.storage._privacy import _not_private_knowledge_dependency

    query = (
        "SELECT c.knowledge_object_id || '#' || c.chunk_index AS id, c.vector AS vector "
        "FROM knowledge_chunk_embeddings c "
        "JOIN knowledge_objects k ON k.id = c.knowledge_object_id "
        "WHERE c.user_id = ? AND c.model = ? AND c.dim = ? "
        "AND k.user_id = ? AND k.deleted_at IS NULL "
        f"AND {_not_private_knowledge_dependency('k')}"  # nosec B608
    )
    params: list[object] = [user_id, model, int(dim), user_id]
    if object_limit is not None and object_limit > 0:
        query += (
            " AND c.knowledge_object_id IN ("
            "SELECT window_k.id FROM knowledge_objects window_k "
            "INDEXED BY idx_knowledge_chunk_scan_order "
            "WHERE window_k.user_id = ? AND window_k.deleted_at IS NULL "
            f"AND {_not_private_knowledge_dependency('window_k')} "  # nosec B608
            "ORDER BY window_k.created_at DESC, window_k.id ASC LIMIT ?)"
        )
        params.extend([user_id, int(object_limit)])
    query += " ORDER BY k.created_at DESC, c.knowledge_object_id, c.chunk_index"
    if row_limit is not None and row_limit > 0:
        query += " LIMIT ?"
        params.append(int(row_limit))
    rows = storage.execute(query, tuple(params)).fetchall()
    return [(str(row["id"]), bytes(row["vector"])) for row in rows]


def test_adaptive_chunk_plans_are_row_for_row_equivalent_and_fail_closed(storage):
    from friday.storage.models import Entity, EntityType, new_id

    owner_ids = _seed_chunk_scan(storage, objects=8, chunks_per_object=8)
    _seed_chunk_scan(storage, user_id="other", objects=3, chunks_per_object=8)

    private_name = "PRIVATE-CHUNK-DEPENDENCY-7f13"
    private_entity = Entity(new_id("ent"), "other", private_name, EntityType.EVENT)
    storage.create_entity(private_entity)
    # The carrier predates the reminder becoming private, reproducing a legacy or
    # cross-source dependency that the closure must hide after authority changes.
    storage.update_knowledge_fields(owner_ids[3], "owner", content=f"Copied private identity: {private_name}")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, 'other', '2026-08-10T09:00:00Z', 'day',
                      'reminder:other', '2026-08-06T00:00:00Z')""",
            (private_entity.id,),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'other', 'reminder', '2026-08-06T00:00:00Z')""",
            (private_entity.id,),
        )
    # Ties exercise the total id-selected object window; insertion order is the
    # deliberate reverse of lexical id order. Deleted/model/dim rows must not leak.
    placeholders = ",".join("?" for _ in owner_ids)
    storage.execute(
        f"UPDATE knowledge_objects SET created_at='2026-01-01T00:00:00Z' "  # nosec B608
        f"WHERE id IN ({placeholders})",
        tuple(owner_ids),
    )
    storage.execute(
        "UPDATE knowledge_chunk_embeddings SET model='retired' WHERE knowledge_object_id=? AND chunk_index=0",
        (owner_ids[0],),
    )
    storage.execute(
        "UPDATE knowledge_chunk_embeddings SET dim=9 WHERE knowledge_object_id=? AND chunk_index=1",
        (owner_ids[1],),
    )
    storage.commit()
    storage.soft_delete_knowledge_object(owner_ids[-1], "owner")

    private_chunk_prefix = f"{owner_ids[3]}#"
    assert not [
        key
        for key, _ in storage.get_user_chunk_embeddings("owner", "m", 4)
        if key.startswith(private_chunk_prefix)
    ]

    for object_limit in (None, 5):
        for row_limit in (None, 17):
            assert storage.get_user_chunk_embeddings(
                "owner", "m", 4, object_limit=object_limit, row_limit=row_limit
            ) == _reference_chunk_scan(
                storage,
                "owner",
                "m",
                4,
                object_limit=object_limit,
                row_limit=row_limit,
            )

    eligible = sorted(set(owner_ids) - {owner_ids[3], owner_ids[-1]})[:5]
    selected_rows = storage.get_user_chunk_embeddings("owner", "m", 4, object_limit=5)
    selected_objects = list(dict.fromkeys(key.rpartition("#")[0] for key, _ in selected_rows))
    assert selected_objects == eligible
    assert {key for key, _ in storage.get_user_embeddings("owner", "m", 4, limit=5)} == set(eligible)

    # The chunk-side user_id is denormalised and can be malformed.  It grants no
    # authority: neither tenant may read a row whose parent and chunk owners differ.
    mismatch_key = f"{owner_ids[2]}#0"
    storage.execute(
        "UPDATE knowledge_chunk_embeddings SET user_id='other' WHERE knowledge_object_id=? AND chunk_index=0",
        (owner_ids[2],),
    )
    storage.commit()
    assert mismatch_key not in {key for key, _ in storage.get_user_chunk_embeddings("owner", "m", 4)}
    assert mismatch_key not in {key for key, _ in storage.get_user_chunk_embeddings("other", "m", 4)}

    # Rolling most rows to an old model selects the sparse branch; it must have the
    # same ordered semantics as the dense branch, not merely the same set.
    retired_ids = [value for index, value in enumerate(owner_ids) if index != 3][:6]
    storage.execute(
        f"UPDATE knowledge_chunk_embeddings SET model='retired' "  # nosec B608
        f"WHERE knowledge_object_id IN ({','.join('?' for _ in retired_ids)})",
        tuple(retired_ids),
    )
    storage.commit()
    sparse_rows = storage.get_user_chunk_embeddings("owner", "m", 4, object_limit=5, row_limit=17)
    assert sparse_rows == _reference_chunk_scan(storage, "owner", "m", 4, object_limit=5, row_limit=17)
    assert not [key for key, _ in sparse_rows if key.startswith(private_chunk_prefix)]


def test_denormalized_vector_owner_never_overrides_parent_tenant(storage):
    owner_ids = _seed_chunk_scan(storage, objects=2, chunks_per_object=4)
    other_ids = _seed_chunk_scan(storage, user_id="other", objects=2, chunks_per_object=4)
    mismatched = owner_ids[0]
    reverse_mismatched = other_ids[0]
    storage.execute(
        "UPDATE knowledge_embeddings SET user_id='other' WHERE knowledge_object_id=?",
        (mismatched,),
    )
    storage.execute(
        "UPDATE knowledge_embeddings SET user_id='owner' WHERE knowledge_object_id=?",
        (reverse_mismatched,),
    )
    storage.execute(
        "UPDATE knowledge_chunk_embeddings SET user_id='other' WHERE knowledge_object_id=?",
        (mismatched,),
    )
    storage.execute(
        "UPDATE knowledge_chunk_embeddings SET user_id='owner' WHERE knowledge_object_id=?",
        (reverse_mismatched,),
    )
    # Keep both callers on the sparse/rolling branch.  Each has one active malformed
    # row group whose chunk owner matches the caller but whose parent belongs to the
    # other tenant; removing the parent-side tenant gate must now fail.
    storage.execute(
        "UPDATE knowledge_chunk_embeddings SET model='retired' WHERE knowledge_object_id IN (?, ?)",
        (owner_ids[1], other_ids[1]),
    )
    storage.commit()

    missing = {str(row["id"]) for row in storage.list_knowledge_missing_embedding("m", limit=100)}
    assert missing == {mismatched, reverse_mismatched}
    assert storage.count_knowledge_missing_embedding("m") == 2

    for tenant in ("owner", "other"):
        sql, _ = _captured_sql(
            storage,
            lambda tenant=tenant: storage.get_user_chunk_embeddings(tenant, "m", 4),
        )
        assert "CROSS JOIN" not in sql
        vector_page = storage.list_user_vectors_page(tenant, "m")
        vector_page_ids = {key for key, _, _ in vector_page}
        assert storage.count_user_vectors(tenant, "m") == len(vector_page_ids) == 1
        for foreign_parent in (mismatched, reverse_mismatched):
            assert foreign_parent not in {key for key, _ in storage.get_user_embeddings(tenant, "m", 4)}
            assert foreign_parent not in {
                key for key, _ in storage.get_user_embeddings(tenant, "m", 4, limit=10)
            }
            assert foreign_parent not in vector_page_ids
            assert not [
                key
                for key, _ in storage.get_user_chunk_embeddings(tenant, "m", 4)
                if key.startswith(f"{foreign_parent}#")
            ]
