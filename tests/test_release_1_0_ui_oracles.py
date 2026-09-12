"""Chromium behavior oracles for 12 of the 17 required admin UI tabs.

Tab title / static JS parse / API-only 200 is not enough: each case navigates
or clicks in a real browser, asserts a literal user-visible string, and checks
the matching private SQLite row when the action writes. Product failures stay
plain. Sidebar presence gives no behavioral credit to the other five tabs.
Missing browser is ENVIRONMENT/BLOCKED via the support fixture.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from friday.permissions import LEGACY_OWNER_USER_ID
from tests.release_1_0_ui_support import (
    ALICE,
    ALICE_NAME,
    AUDIT_NAME,
    AUDIT_NEG_NAME,
    AUDIT_NEG_USER,
    AUDIT_USER,
    BORIS,
    CONV_MESSAGE,
    CONV_TITLE,
    EMPTY,
    FILE_NAME,
    FOREIGN_FILE,
    FOREIGN_INBOX,
    FOREIGN_KNOWLEDGE,
    INBOX_BODY,
    KNOWLEDGE_BODY,
    KNOWLEDGE_TITLE,
    NEW_NAME,
    NEW_USER,
    PROMOTE_TITLE,
    TOKEN,
    VIEWS,
    app_text,
    assert_dashboard_counts,
    click_expect_response,
    dashboard_stat_map,
    expected_overview_counts,
    folded_text,
    open_tab,
    owned_backup_sqlite_files,
    select_person,
    sha256_file,
    start_ui_stack,
    toast_text,
    wait_app_has,
    wait_ready,
    wait_toast,
)

pytest_plugins = ["tests.release_1_0_ui_support"]


def _equal(actual, expected, code: str) -> None:
    assert actual == expected, (code, actual, expected)


def _as_int(value) -> int:
    if value in (True, False):
        return int(value)
    return int(value)


def _count(storage, sql: str, params: tuple = ()) -> int:
    row = storage.execute(sql, params).fetchone()
    return int(row["count"] if row is not None else 0)


def _expect_assertion(code: str, fn):
    with pytest.raises(AssertionError) as caught:
        fn()
    text = str(caught.value)
    assert code in text, (code, "fault_did_not_red_named_assertion", text)


def _row_for_user_id(page, user_id: str):
    row = page.locator("tr", has_text=user_id)
    if user_id == AUDIT_USER:
        return row.filter(has_not_text=AUDIT_NEG_USER)
    return row


def test_ui_dashboard_shows_literal_counts_and_honest_empty_backups(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "dashboard", "Обзор")
    expected = expected_overview_counts(ui_owner.storage)
    assert_dashboard_counts(page, expected)
    folded = folded_text(page)
    assert "знаний" in folded, "ui_dashboard_knowledge_label"
    assert "пользователей" in folded, "ui_dashboard_users_label"
    assert "резервных копий пока нет" in folded, "ui_dashboard_empty_backups"
    assert FOREIGN_KNOWLEDGE not in app_text(page), "ui_dashboard_no_foreign_title"
    _equal(page.locator("#nav button").count(), 17, "ui_dashboard_tab_count")


def test_ui_inbox_review_promotes_seeded_material_to_knowledge(ui_owner):
    page, base, storage, ids = ui_owner.page, ui_owner.base, ui_owner.storage, ui_owner.ids
    before_alice_ko = _count(
        storage, "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=?", (ALICE,)
    )
    before_boris_ko = _count(
        storage, "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=?", (BORIS,)
    )
    open_tab(page, base, "inbox", "Inbox")
    assert INBOX_BODY in app_text(page), "ui_inbox_seed_visible"
    page.locator("button", has_text="Разобрать").first.click()
    page.locator("#inboxTitle").wait_for()
    page.fill("#inboxTitle", PROMOTE_TITLE)
    click_expect_response(
        page,
        page.locator("#modalFoot button", has_text="Продвинуть в знания"),
        lambda resp: "/classify" in resp.url and resp.request.method == "POST",
        "ui_inbox_promote_handler",
    )
    wait_toast(page, "сохранён")
    wait_ready(page, "Inbox")
    promoted = list(
        storage.execute(
            "SELECT id, title, user_id FROM knowledge_objects WHERE title=?",
            (PROMOTE_TITLE,),
        ).fetchall()
    )
    _equal(len(promoted), 1, "ui_inbox_promote_cardinality")
    _equal(str(promoted[0]["user_id"]), ALICE, "ui_inbox_promote_owner")
    inbox = storage.get_inbox_item(ids["alice_inbox"], ALICE)
    assert inbox is not None, "ui_inbox_row_missing"
    _equal(str(inbox["status"]), "classified", "ui_inbox_status_after_promote")
    assert inbox.get("knowledge_object_id"), "ui_inbox_promote_linked"
    _equal(
        _count(storage, "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=?", (ALICE,)),
        before_alice_ko + 1,
        "ui_inbox_alice_ko_delta",
    )
    _equal(
        _count(storage, "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=?", (BORIS,)),
        before_boris_ko,
        "ui_inbox_boris_ko_unrelated",
    )
    boris_inbox = storage.get_inbox_item(ids["boris_inbox"], BORIS)
    _equal(str(boris_inbox["status"]), "pending", "ui_inbox_boris_status_unrelated")
    empty_pending = _count(
        storage,
        "SELECT COUNT(*) AS count FROM inbox WHERE user_id=? AND status='pending'",
        (EMPTY,),
    )
    _equal(empty_pending, 0, "ui_inbox_empty_unrelated")


def test_ui_inbox_hides_foreign_membership(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "inbox", "Inbox")
    body = app_text(page)
    assert INBOX_BODY in body, "ui_inbox_own_visible"
    assert FOREIGN_INBOX not in body, "ui_inbox_foreign_hidden"
    assert FOREIGN_INBOX not in page.content(), "ui_inbox_foreign_not_in_markup"


def test_ui_knowledge_search_opens_inspection_of_seeded_document(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "knowledge", "Знания")
    page.fill("#knowledgeSearch", "СМЕТА-СКЛАД")
    click_expect_response(
        page,
        page.locator("button", has_text="Найти").first,
        lambda resp: "/api/admin/knowledge" in resp.url and "q=" in resp.url,
        "ui_knowledge_search_handler",
    )
    wait_ready(page, "Знания")
    body = app_text(page)
    assert KNOWLEDGE_TITLE in body, "ui_knowledge_search_hit"
    ko_id = ui_owner.ids["alice_ko"]
    resp = click_expect_response(
        page,
        page.locator("button", has_text="Инспекция").first,
        lambda r, wanted=ko_id: (
            r.request.method == "GET" and r.url.split("?")[0].endswith(f"/api/admin/knowledge/{wanted}")
        ),
        "ui_knowledge_inspect_handler",
    )
    page.locator("#modalTitle").wait_for()
    modal = page.locator("#modal").inner_text()
    assert KNOWLEDGE_TITLE in modal, "ui_knowledge_inspect_title"
    assert KNOWLEDGE_BODY in modal, "ui_knowledge_inspect_body"
    assert resp.status == 200, ("ui_knowledge_inspect_http", resp.status)


def test_ui_knowledge_search_hides_foreign_document(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "knowledge", "Знания")
    body = app_text(page)
    assert KNOWLEDGE_TITLE in body, "ui_knowledge_own_visible"
    assert FOREIGN_KNOWLEDGE not in body, "ui_knowledge_foreign_hidden"


def test_ui_knowledge_soft_deleted_inspection_keeps_own_history_and_leaves_the_list(ui_owner):
    """Soft deletion removes list membership; exact admin history stays available."""
    page, base, storage = ui_owner.page, ui_owner.base, ui_owner.storage
    open_tab(page, base, "knowledge", "Знания")
    assert KNOWLEDGE_TITLE in app_text(page), "ui_knowledge_before_delete"
    ko_id = ui_owner.ids["alice_ko"]
    foreign_before = storage.get_knowledge_object(ui_owner.ids["boris_ko"], BORIS)
    assert storage.soft_delete_knowledge_object(ko_id, ALICE)
    deleted = storage.get_knowledge_object(ko_id, ALICE)["deleted_at"]
    resp = click_expect_response(
        page,
        page.locator("button", has_text="Инспекция").first,
        lambda r: r.request.method == "GET" and r.url.split("?")[0].endswith(f"/api/admin/knowledge/{ko_id}"),
        "ui_knowledge_deleted_inspect_handler",
    )
    _equal(resp.status, 200, "ui_knowledge_deleted_inspect_http")
    payload = resp.json()
    item = payload["item"]
    _equal(
        (item["id"], item["user_id"], item["title"], item["content"], item["deleted_at"], item["version"]),
        (ko_id, ALICE, KNOWLEDGE_TITLE, KNOWLEDGE_BODY, deleted, 2),
        "ui_knowledge_deleted_item",
    )
    versions = payload["versions"]
    _equal(
        [(v["version"], v["knowledge_object_id"], v["user_id"]) for v in versions],
        [(2, ko_id, ALICE), (1, ko_id, ALICE)],
        "ui_knowledge_deleted_history",
    )
    for v in versions:
        snapshot = json.loads(v["snapshot_json"])
        _equal(
            (snapshot["title"], snapshot["content"]),
            (KNOWLEDGE_TITLE, KNOWLEDGE_BODY),
            "ui_knowledge_deleted_history_content",
        )
    page.wait_for_function(
        "wanted => document.getElementById('modalTitle').textContent === 'Инспекция: ' + wanted",
        arg=KNOWLEDGE_TITLE,
    )
    page.locator("#modal").wait_for(state="visible")
    modal = page.locator("#modal").inner_text()
    assert KNOWLEDGE_BODY in modal and "История версий" in modal, "ui_knowledge_deleted_modal"
    assert FOREIGN_KNOWLEDGE not in modal, "ui_knowledge_deleted_not_foreign"
    _equal(
        storage.get_knowledge_object(ui_owner.ids["boris_ko"], BORIS),
        foreign_before,
        "ui_knowledge_deleted_foreign_preserved",
    )
    page.locator("#modalFoot button", has_text="Закрыть").click()
    page.reload(wait_until="domcontentloaded")
    wait_ready(page, "Знания")
    # Reload restores the tab; the fixture's selected person must be restored explicitly.
    select_person(page, ALICE)
    wait_ready(page, "Знания")
    _equal(page.locator("#userSelect").input_value(), ALICE, "ui_knowledge_deleted_list_person")
    assert KNOWLEDGE_TITLE not in app_text(page) and FOREIGN_KNOWLEDGE not in app_text(page), (
        "ui_knowledge_deleted_hidden_from_list"
    )
    select_person(page, BORIS)
    wait_ready(page, "Знания")
    _equal(page.locator("#userSelect").input_value(), BORIS, "ui_knowledge_foreign_list_person")
    assert FOREIGN_KNOWLEDGE in app_text(page) and KNOWLEDGE_TITLE not in app_text(page), (
        "ui_knowledge_other_person_keeps_own_live_item"
    )


def test_ui_conversations_open_messages_and_archive_keeps_them(ui_owner):
    page, base, storage, ids = ui_owner.page, ui_owner.base, ui_owner.storage, ui_owner.ids
    conv_id = ids["alice_conversation"]
    boris_id = ids["boris_conversation"]
    open_tab(page, base, "conversations", "Диалоги")
    body = app_text(page)
    assert CONV_TITLE in body, "ui_conversations_title"
    assert "Чужой диалог Бориса" not in body, "ui_conversations_foreign_hidden"
    page.locator("button", has_text="Сообщения").first.click()
    page.locator("#modalTitle").wait_for()
    assert CONV_MESSAGE in page.locator("#modal").inner_text(), "ui_conversations_open_message"
    page.locator("#modalFoot button", has_text="Закрыть").click()
    before_alice = _as_int(
        storage.execute("SELECT is_archived FROM conversations WHERE id=?", (conv_id,)).fetchone()[
            "is_archived"
        ]
    )
    _equal(before_alice, 0, "ui_conversations_not_archived_before")
    click_expect_response(
        page,
        page.locator("button", has_text="Архивировать").first,
        lambda resp: "/archive" in resp.url and resp.request.method == "POST",
        "ui_conversations_archive_handler",
    )
    wait_toast(page, "архивирован")
    wait_ready(page, "Диалоги")
    row = storage.execute("SELECT is_archived FROM conversations WHERE id=?", (conv_id,)).fetchone()
    assert row is not None, "ui_conversations_row"
    _equal(_as_int(row["is_archived"]), 1, "ui_conversations_is_archived")
    boris_row = storage.execute("SELECT is_archived FROM conversations WHERE id=?", (boris_id,)).fetchone()
    _equal(_as_int(boris_row["is_archived"]), 0, "ui_conversations_boris_unrelated")
    messages = storage.execute("SELECT content FROM messages WHERE conversation_id=?", (conv_id,)).fetchall()
    bodies = [str(item["content"]) for item in messages]
    _equal(len(bodies), int(ids["alice_message_count"]), "ui_conversations_message_cardinality")
    assert CONV_MESSAGE in bodies, "ui_conversations_archive_keeps_text"
    assert "ЧУЖОЕ-СООБЩЕНИЕ-БОРИСА" not in bodies, "ui_conversations_no_foreign_side_effect"


def test_ui_files_list_owned_hides_foreign(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "files", "Файлы")
    body = app_text(page)
    assert FILE_NAME in body, "ui_files_owned_visible"
    assert FOREIGN_FILE not in body, "ui_files_foreign_hidden"


def test_ui_files_download_of_missing_bytes_errors_not_foreign(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    raw_id = ui_owner.ids["alice_file"]
    open_tab(page, base, "files", "Файлы")
    resp = click_expect_response(
        page,
        page.locator("button", has_text="Скачать").first,
        lambda r, wanted=raw_id: "/download" in r.url and wanted in r.url,
        "ui_files_download_handler",
    )
    toast = toast_text(page)
    assert FOREIGN_FILE not in toast and FOREIGN_FILE not in app_text(page), "ui_files_missing_not_foreign"
    assert resp.status == 404, ("ui_files_missing_artifact", resp.status, toast)
    folded = toast.casefold()
    assert "не найден" in folded or "HTTP 404" in toast or "Файл не найден" in toast, (
        "ui_files_missing_artifact_toast",
        toast,
    )


def test_ui_users_create_persists_literal_account(ui_owner):
    page, base, storage = ui_owner.page, ui_owner.base, ui_owner.storage
    before = _count(storage, "SELECT COUNT(*) AS count FROM users")
    open_tab(page, base, "users", "Пользователи")
    assert ALICE_NAME in app_text(page), "ui_users_alice_listed"
    page.locator("button", has_text="Добавить").click()
    page.locator("#newUserId").wait_for()
    page.fill("#newUserId", NEW_USER)
    page.fill("#newUserName", NEW_NAME)
    click_expect_response(
        page,
        page.locator("#modalFoot button", has_text="Создать"),
        lambda resp: "/api/admin/users" in resp.url and resp.request.method in {"POST", "PUT", "PATCH"},
        "ui_users_create_handler",
    )
    wait_toast(page, "создан")
    wait_app_has(page, NEW_NAME)
    assert NEW_NAME in app_text(page), "ui_users_created_visible"
    row = storage.execute("SELECT id, display_name FROM users WHERE id=?", (NEW_USER,)).fetchone()
    assert row is not None, "ui_users_created_persisted"
    _equal(str(row["display_name"]), NEW_NAME, "ui_users_created_name")
    _equal(_count(storage, "SELECT COUNT(*) AS count FROM users"), before + 1, "ui_users_create_cardinality")
    _equal(
        str(
            storage.execute("SELECT display_name FROM users WHERE id=?", (ALICE,)).fetchone()["display_name"]
        ),
        ALICE_NAME,
        "ui_users_alice_unrelated",
    )


def test_ui_users_disable_does_not_change_sibling(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "users", "Пользователи")
    before = {
        str(row["id"]): str(row["status"])
        for row in ui_owner.storage.execute("SELECT id, status FROM users").fetchall()
    }
    target = page.locator("tr", has_text=EMPTY)
    assert target.count() == 1, "ui_users_empty_row"
    click_expect_response(
        page,
        target.locator("button", has_text="Отключить"),
        lambda resp: resp.request.method in {"POST", "PATCH", "PUT"} and "/users" in resp.url,
        "ui_users_disable_handler",
    )
    wait_toast(page, "Статус изменён")
    wait_ready(page, "Пользователи")
    after = {
        str(row["id"]): str(row["status"])
        for row in ui_owner.storage.execute("SELECT id, status FROM users").fetchall()
    }
    _equal(after.get(EMPTY), "disabled", "ui_users_empty_disabled")
    _equal(after.get(ALICE), before.get(ALICE), "ui_users_alice_unchanged")
    _equal(after.get(BORIS), before.get(BORIS), "ui_users_boris_unchanged")
    _equal(after.get(LEGACY_OWNER_USER_ID), before.get(LEGACY_OWNER_USER_ID), "ui_users_owner_unchanged")


def _create_user_via_ui(page, base, user_id: str, name: str) -> None:
    open_tab(page, base, "users", "Пользователи")
    page.locator("button", has_text="Добавить").click()
    page.locator("#newUserId").wait_for()
    page.fill("#newUserId", user_id)
    page.fill("#newUserName", name)
    click_expect_response(
        page,
        page.locator("#modalFoot button", has_text="Создать"),
        lambda resp: "/api/admin/users" in resp.url and resp.request.method in {"POST", "PUT", "PATCH"},
        "ui_audit_user_create_handler",
    )
    wait_toast(page, "создан")
    wait_ready(page, "Пользователи")


def test_ui_audit_lists_real_action_and_opens_matching_details(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    _create_user_via_ui(page, base, AUDIT_USER, AUDIT_NAME)
    open_tab(page, base, "audit", "Аудит")
    row = _row_for_user_id(page, AUDIT_USER)
    assert row.count() >= 1, "ui_audit_target_row"
    row_text = row.first.inner_text()
    assert "admin.user.upsert" in row_text, ("ui_audit_action_visible", row_text)
    assert LEGACY_OWNER_USER_ID in row_text, ("ui_audit_actor_visible", row_text)
    row.first.locator("button", has_text="Детали").click()
    page.locator("#modalTitle").wait_for()
    details = page.locator("#modal").inner_text()
    assert "admin.user.upsert" in details, ("ui_audit_details_action", details[:400])
    assert AUDIT_USER in details, ("ui_audit_details_target", details[:400])
    assert LEGACY_OWNER_USER_ID in details, ("ui_audit_details_actor", details[:400])
    assert "Запись не найдена" not in details, "ui_audit_details_missing"


def test_ui_audit_details_do_not_show_a_different_row(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    _create_user_via_ui(page, base, AUDIT_USER, AUDIT_NAME)
    _create_user_via_ui(page, base, AUDIT_NEG_USER, AUDIT_NEG_NAME)
    open_tab(page, base, "audit", "Аудит")
    row = _row_for_user_id(page, AUDIT_NEG_USER)
    assert row.count() >= 1, "ui_audit_neg_row"
    row.first.locator("button", has_text="Детали").click()
    page.locator("#modalTitle").wait_for()
    details = page.locator("#modal").inner_text()
    assert AUDIT_NEG_USER in details, "ui_audit_details_selected_target"
    assert f'"target_id": "{AUDIT_NEG_USER}"' in details or f'target_id": "{AUDIT_NEG_USER}"' in details, (
        "ui_audit_details_target_id",
        details[:500],
    )
    assert f'"target_id": "{AUDIT_USER}"' not in details, ("ui_audit_details_not_other_target", details[:500])
    assert FOREIGN_KNOWLEDGE not in details, "ui_audit_details_not_foreign"
    assert "ЧУЖОЕ-СООБЩЕНИЕ-БОРИСА" not in details, "ui_audit_details_not_foreign_message"
    assert "admin.user.upsert" in details, "ui_audit_details_action_bound"


def test_ui_backups_create_now_lists_sha_and_persists(ui_owner):
    page, base, storage = ui_owner.page, ui_owner.base, ui_owner.storage
    open_tab(page, base, "backups", "Резервирование")
    before = owned_backup_sqlite_files(storage)
    _equal(len(before), 0, "ui_backups_none_before")
    resp = click_expect_response(
        page,
        page.locator("button", has_text="Создать сейчас"),
        lambda r: r.url.rstrip("/").endswith("/api/admin/backups") and r.request.method == "POST",
        "ui_backups_create_handler",
    )
    assert resp.status == 200, ("ui_backups_create_http", resp.status, resp.text())
    payload = resp.json()
    backup = payload.get("backup") or {}
    name = str(backup.get("database") or "")
    listed_sha = str(backup.get("sha256") or "")
    assert name.endswith(".sqlite3"), ("ui_backups_create_name", name)
    assert len(listed_sha) == 64, ("ui_backups_create_sha", listed_sha)
    wait_toast(page, "Создано")
    wait_ready(page, "Резервирование")
    body = app_text(page)
    assert "Копий ещё нет" not in body, "ui_backups_created_listed"
    files = owned_backup_sqlite_files(storage)
    _equal(len(files), 1, "ui_backups_artifact_cardinality")
    artifact = files[0]
    _equal(artifact.name, name, "ui_backups_artifact_name")
    independent = sha256_file(artifact)
    _equal(independent, listed_sha, "ui_backups_artifact_hash")
    assert listed_sha in body, "ui_backups_sha_visible"
    manifest = artifact.with_suffix(".manifest.json")
    assert manifest.is_file(), "ui_backups_manifest_missing"


def test_ui_backups_verify_reaches_handler(ui_owner):
    page, base, storage = ui_owner.page, ui_owner.base, ui_owner.storage
    open_tab(page, base, "backups", "Резервирование")
    create = click_expect_response(
        page,
        page.locator("button", has_text="Создать сейчас"),
        lambda r: r.url.rstrip("/").endswith("/api/admin/backups") and r.request.method == "POST",
        "ui_backups_verify_create_handler",
    )
    assert create.status == 200, ("ui_backups_verify_create_http", create.status)
    wait_toast(page, "Создано")
    wait_ready(page, "Резервирование")
    files = owned_backup_sqlite_files(storage)
    _equal(len(files), 1, "ui_backups_verify_artifact")
    resp = click_expect_response(
        page,
        page.locator("button", has_text="Проверить").first,
        lambda r: "/verify" in r.url and r.request.method == "POST",
        "ui_backups_verify_handler",
    )
    assert resp.status == 200, ("ui_backups_verify_http", resp.status, resp.text())
    verification = (resp.json() or {}).get("verification") or {}
    assert verification.get("ok") is True, ("ui_backups_verify_ok", verification)
    wait_toast(page, "исправна")
    toast = toast_text(page).casefold()
    assert "исправна" in toast, ("ui_backups_verify_toast", toast_text(page))
    assert "не пройдена" not in toast, ("ui_backups_verify_not_failure", toast_text(page))


def _assert_compact_values(page):
    _equal(
        dashboard_stat_map(page),
        {
            "ходов всего": "0",
            "ответила структура": "—",
            "ответила модель": "—",
            "принято поправок": "—",
            "отказов в правах": "—",
            "не выполнено поручений": "0",
        },
        "ui_compacts_literal_counters",
    )
    assert "Собрано по 0 ходам" in app_text(page), "ui_compacts_source_turns"
    assert "За эти сутки ничего не отмечено" in app_text(page), "ui_compacts_no_incidents"


def test_ui_compacts_run_and_open_literal_counters(ui_owner):
    page, base, storage = ui_owner.page, ui_owner.base, ui_owner.storage
    _equal(_count(storage, "SELECT COUNT(*) AS count FROM day_compacts"), 0, "ui_compacts_empty_before")
    open_tab(page, base, "compacts", "Сводки")
    assert "Прогонов ещё не было" in app_text(page), "ui_compacts_empty_view"
    page.fill("#compactDate", "2026-08-03")
    resp = click_expect_response(
        page,
        page.locator("button", has_text="Собрать"),
        lambda r: r.url.split("?")[0].endswith("/api/compacts/run") and r.request.method == "POST",
        "ui_compacts_run_handler",
    )
    _equal(resp.status, 200, "ui_compacts_run_http")
    made = resp.json()
    expected_counters = {"total_turns": 0, "ignored_orders": 0, "measured_turns": 0}
    _equal(
        (
            made["principal"],
            made["local_date"],
            made["status"],
            made["source_turns"],
            made["counters"],
            made["incidents"],
            made["patterns"],
        ),
        (ALICE, "2026-08-03", "done", 0, expected_counters, [], []),
        "ui_compacts_actual_result",
    )
    rows = storage.execute("SELECT * FROM day_compacts").fetchall()
    _equal(len(rows), 1, "ui_compacts_persisted_cardinality")
    row = rows[0]
    _equal(
        (
            row["id"],
            row["principal"],
            row["local_date"],
            row["status"],
            row["source_turns"],
            json.loads(row["counters_json"]),
        ),
        (made["id"], ALICE, "2026-08-03", "done", 0, expected_counters),
        "ui_compacts_persisted_row",
    )
    wait_app_has(page, "2026-08-03")
    wait_ready(page, "Сводки")
    page.locator("button", has_text="2026-08-03").first.click()
    page.wait_for_function("() => document.querySelectorAll('#app .card.stat').length === 6")
    _assert_compact_values(page)
    assert FOREIGN_KNOWLEDGE not in app_text(page), "ui_compacts_no_foreign_text"


def test_ui_diagnostics_shows_live_llm_disabled_not_config_lie(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "diagnostics", "Диагностика")
    body = app_text(page)
    folded = folded_text(page)
    assert "общее состояние" in folded, "ui_diagnostics_state"
    assert "llm" in folded, "ui_diagnostics_llm_row"
    assert "выключен" in folded, "ui_diagnostics_llm_disabled"
    llm_tail = folded.split("llm", 1)[-1][:80]
    assert "включён" not in llm_tail, "ui_diagnostics_llm_not_config_lie"
    assert FOREIGN_KNOWLEDGE not in body, "ui_diagnostics_no_foreign"


def test_ui_quality_explain_finds_seeded_title(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "quality", "Качество")
    body = app_text(page)
    assert "Оценка качества поиска" in body, "ui_quality_eval_card"
    assert "Трейс ретривера" in body, "ui_quality_explain_card"
    page.fill("#explainQuery", "СМЕТА-СКЛАД")
    resp = click_expect_response(
        page,
        page.locator("button", has_text="Объяснить"),
        lambda r: "/retrieval/explain" in r.url,
        "ui_quality_explain_handler",
    )
    page.wait_for_function(
        """() => {
            const el = document.getElementById('explainResults');
            if (!el) return false;
            const text = el.innerText || '';
            return text && !text.includes('Введите запрос');
        }""",
        timeout=15000,
    )
    explained = page.locator("#explainResults").inner_text()
    assert FOREIGN_KNOWLEDGE not in explained, "ui_quality_explain_not_foreign"
    if "Кандидатов не найдено" in explained:
        payload = resp.json() if resp.status == 200 else {}
        raise AssertionError(
            (
                "ui_quality_explain_hit",
                "seeded title produced empty retriever trace; explain is documented model-free, not a disabled-model empty card",
                payload.get("returned"),
                payload.get("candidates"),
                explained[:300],
            )
        )
    assert KNOWLEDGE_TITLE in explained, ("ui_quality_explain_title", explained[:400])
    assert "СМЕТА-СКЛАД" in explained, ("ui_quality_explain_query_hit", explained[:400])


def test_ui_cleanup_empty_does_not_list_foreign(ui_owner):
    page, base, storage = ui_owner.page, ui_owner.base, ui_owner.storage
    before = {
        table: [dict(row) for row in storage.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
        for table in ("raw_objects", "knowledge_objects", "knowledge_object_versions", "inbox")
    }
    open_tab(page, base, "cleanup", "Ревизия")
    with page.expect_response(
        lambda r: "/api/admin/cleanup/legacy?" in r.url and "user_id=" + EMPTY.replace(":", "%3A") in r.url
    ) as pending:
        select_person(page, EMPTY)
    resp = pending.value
    _equal(resp.status, 200, "ui_cleanup_empty_http")
    data = resp.json()
    _equal(
        (data["user_id"], data["items"], data["count"], data["total"], data["offset"]),
        (EMPTY, [], 0, 0, 0),
        "ui_cleanup_empty_result",
    )
    wait_app_has(page, "Подозрительных объектов не найдено")
    _equal(page.locator("#app .cleanup-check").count(), 0, "ui_cleanup_empty_rows")
    assert FOREIGN_KNOWLEDGE not in app_text(page) and KNOWLEDGE_TITLE not in app_text(page), (
        "ui_cleanup_foreign_hidden"
    )
    after = {
        table: [dict(row) for row in storage.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
        for table in before
    }
    _equal(after, before, "ui_cleanup_no_writes")


def test_ui_empty_person_inbox_is_empty_not_alices(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "inbox", "Inbox")
    select_person(page, EMPTY)
    body = app_text(page)
    assert INBOX_BODY not in body, "ui_inbox_empty_not_alice"
    assert FOREIGN_INBOX not in body, "ui_inbox_empty_not_boris"
    assert "Очередь пуста" in body or "пока нет" in body.casefold() or "пуста" in body.casefold(), (
        "ui_inbox_empty_honest"
    )


def test_ui_missing_token_is_auth_dialog_not_empty_pass(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    page.evaluate("() => sessionStorage.removeItem('jericho_api_token')")
    page.goto(f"{base}/admin/#inbox", wait_until="domcontentloaded")
    page.reload(wait_until="domcontentloaded")
    page.locator("#modalTitle").wait_for()
    title = page.locator("#modalTitle").inner_text()
    assert "Admin API" in title or "ключ" in title.casefold() or "Доступ" in title, "ui_auth_dialog"
    assert INBOX_BODY not in app_text(page), "ui_auth_no_seed_leak"


def test_ui_wrong_token_does_not_leak_seeded_titles(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", "X" * 48)
    page.goto(f"{base}/admin/#knowledge", wait_until="domcontentloaded")
    page.reload(wait_until="domcontentloaded")
    page.locator("#modalTitle").wait_for()
    body = app_text(page) + page.locator("#modal").inner_text()
    assert KNOWLEDGE_TITLE not in body, "ui_wrong_token_no_knowledge"
    assert FOREIGN_KNOWLEDGE not in body, "ui_wrong_token_no_foreign"
    assert FILE_NAME not in body, "ui_wrong_token_no_file"


def test_ui_nav_click_opens_diagnostics_title(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "dashboard", "Обзор")
    page.locator("#nav button", has_text="Диагностика").click()
    wait_ready(page, "Диагностика")
    _equal(page.locator("#pageTitle").inner_text(), "Диагностика", "ui_nav_click_title")
    assert "LLM" in app_text(page), "ui_nav_click_body"


def test_ui_all_registered_tabs_are_in_the_sidebar(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "dashboard", "Обзор")
    labels = [label for _view, label in VIEWS]
    for label in labels:
        assert page.locator("#nav button", has_text=label).count() == 1, ("ui_nav_missing", label)
    assert TOKEN  # fixture authenticated


def _process_rows():
    rows = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / "stat").read_text().rpartition(") ")[2].split()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        rows[int(directory.name)] = (int(fields[1]), fields[19], fields[0])
    return rows


class _Owned(dict):
    """Observe real owned descendants, including their PID birth identity."""

    def __init__(self):
        super().__init__()
        self.processes = set()
        self.stopped = False

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        rows = _process_rows()
        parents = {os.getpid()}
        while True:
            children = {pid for pid, (parent, _, _) in rows.items() if parent in parents}
            expanded = parents | children
            if expanded == parents:
                break
            parents = expanded
        self.processes.update((pid, rows[pid][1]) for pid in parents if pid != os.getpid())

    def assert_closed(self):
        assert self["socket"].fileno() == -1, "ui_cleanup_socket_closed"
        assert not self["thread"].is_alive(), "ui_cleanup_thread_closed"
        assert self["server"].should_exit, "ui_cleanup_server_exit"
        assert not [t for t in threading.enumerate() if t.is_alive() and t.name.startswith("ui-oracle-")], (
            "ui_cleanup_no_server_thread"
        )
        if "browser" in self:
            assert not self["browser"].is_connected(), "ui_cleanup_browser_disconnected"
            assert self["page"].is_closed(), "ui_cleanup_page_closed"
        if "playwright" in self:
            assert self.stopped and self.processes, "ui_cleanup_driver_stop_observed"
        active = []
        for pid, birth in self.processes:
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            # An unreadable owned PID is uncertainty, not proof of cleanup.
            # Exited zombies cannot execute or retain a browser/socket; PID
            # reuse is distinguished by the process birth identity.
            if fields[19] == birth and fields[0] != "Z":
                active.append((pid, birth))
        assert not active, ("ui_cleanup_owned_processes_active", active)


def _watch_stop(monkeypatch, owned, *, fail=False):
    from playwright.sync_api import sync_playwright

    manager = type(sync_playwright())
    original_start = manager.start

    def start(context):
        play = original_start(context)
        original_stop = play.stop

        def stop():
            original_stop()
            owned.stopped = True
            if fail:
                raise RuntimeError("injected-cleanup-stop")

        play.stop = stop
        return play

    monkeypatch.setattr(manager, "start", start)


@pytest.mark.parametrize("stage", ("seed", "launch", "goto"))
def test_ui_stack_cleans_resources_when_an_actual_setup_call_fails(settings, monkeypatch, stage):
    from playwright.sync_api import BrowserType, Page

    import tests.release_1_0_ui_support as support

    owned = _Owned()
    injected = []

    def fail(*args, **kwargs):
        injected.append(stage)
        raise RuntimeError("injected-actual-" + stage)

    _watch_stop(monkeypatch, owned)
    if stage == "seed":
        monkeypatch.setattr(support, "seed_ui_corpus", fail)
    elif stage == "launch":
        monkeypatch.setattr(BrowserType, "launch", fail)
    else:
        monkeypatch.setattr(Page, "goto", fail)
    expected = pytest.fail.Exception if stage == "launch" else RuntimeError
    with pytest.raises(expected, match="injected-actual-" + stage), start_ui_stack(settings, owned=owned):
        raise AssertionError("ui_setup_fault_did_not_fire")
    _equal(injected, [stage], "ui_setup_fault_exercised")
    owned.assert_closed()


def test_ui_stack_preserves_environment_failure_with_cleanup_failure(settings, monkeypatch):
    from playwright.sync_api import Page

    owned = _Owned()
    injected = []

    def fail(*args, **kwargs):
        injected.append("goto")
        pytest.fail("ENVIRONMENT/BLOCKED: injected-actual-goto")

    monkeypatch.setattr(Page, "goto", fail)
    _watch_stop(monkeypatch, owned, fail=True)
    with pytest.raises(BaseExceptionGroup) as failure, start_ui_stack(settings, owned=owned):
        raise AssertionError("ui_setup_fault_did_not_fire")
    _equal(injected, ["goto"], "ui_setup_fault_exercised")
    errors = failure.value.exceptions
    assert len(errors) == 2 and isinstance(errors[0], pytest.fail.Exception), "ui_cleanup_original_preserved"
    assert str(errors[0]) == "ENVIRONMENT/BLOCKED: injected-actual-goto", "ui_cleanup_original_detail"
    assert isinstance(errors[1], RuntimeError) and str(errors[1]) == "injected-cleanup-stop", (
        "ui_cleanup_failure_preserved"
    )
    owned.assert_closed()


def test_ui_stack_normal_exit_closes_its_observed_browser_driver_and_socket(settings, monkeypatch):
    owned = _Owned()
    _watch_stop(monkeypatch, owned)
    with start_ui_stack(settings, owned=owned) as ui:
        assert ui.browser.is_connected() and owned["socket"].fileno() >= 0, "ui_cleanup_live_precondition"
    owned.assert_closed()


def test_fault_dashboard_wrong_user_count_reds(ui_owner):
    page, base, storage = ui_owner.page, ui_owner.base, ui_owner.storage
    frozen = expected_overview_counts(storage)
    storage.ensure_user("local:r10-ui-ghost", source="local", display_name="Призрак-счёт", preset_key="user")
    storage.commit()
    open_tab(page, base, "dashboard", "Обзор")
    page.reload(wait_until="domcontentloaded")
    wait_ready(page, "Обзор")
    _expect_assertion("ui_dashboard_пользователей_count", lambda: assert_dashboard_counts(page, frozen))


def test_fault_dropped_archive_write_reds(ui_owner):
    page, base, storage, ids = ui_owner.page, ui_owner.base, ui_owner.storage, ui_owner.ids
    conv_id = ids["alice_conversation"]
    open_tab(page, base, "conversations", "Диалоги")
    page.locator("button", has_text="Сообщения").first.click()
    page.locator("#modalTitle").wait_for()
    page.locator("#modalFoot button", has_text="Закрыть").click()
    click_expect_response(
        page,
        page.locator("button", has_text="Архивировать").first,
        lambda resp: "/archive" in resp.url and resp.request.method == "POST",
        "ui_fault_archive_handler",
    )
    wait_toast(page, "архивирован")
    storage.execute("UPDATE conversations SET is_archived=0 WHERE id=?", (conv_id,))
    storage.commit()

    def _check() -> None:
        row = storage.execute("SELECT is_archived FROM conversations WHERE id=?", (conv_id,)).fetchone()
        _equal(_as_int(row["is_archived"]), 1, "ui_conversations_is_archived")

    _expect_assertion("ui_conversations_is_archived", _check)


def test_fault_wrong_audit_record_reds(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    _create_user_via_ui(page, base, AUDIT_USER, AUDIT_NAME)
    _create_user_via_ui(page, base, AUDIT_NEG_USER, AUDIT_NEG_NAME)
    open_tab(page, base, "audit", "Аудит")
    wrong = _row_for_user_id(page, AUDIT_USER)
    wrong.first.locator("button", has_text="Детали").click()
    page.locator("#modalTitle").wait_for()

    def _check() -> None:
        details = page.locator("#modal").inner_text()
        assert AUDIT_NEG_USER in details, "ui_audit_details_selected_target"

    _expect_assertion("ui_audit_details_selected_target", _check)


def test_fault_backup_missing_artifact_reds_verify(ui_owner):
    page, base, storage = ui_owner.page, ui_owner.base, ui_owner.storage
    open_tab(page, base, "backups", "Резервирование")
    click_expect_response(
        page,
        page.locator("button", has_text="Создать сейчас"),
        lambda r: r.url.rstrip("/").endswith("/api/admin/backups") and r.request.method == "POST",
        "ui_fault_backup_create_handler",
    )
    wait_toast(page, "Создано")
    wait_ready(page, "Резервирование")
    files = owned_backup_sqlite_files(storage)
    _equal(len(files), 1, "ui_fault_backup_exists")
    files[0].unlink()
    assert not files[0].is_file(), "ui_fault_backup_unlinked"

    def _check() -> None:
        resp = click_expect_response(
            page,
            page.locator("button", has_text="Проверить").first,
            lambda r: "/verify" in r.url and r.request.method == "POST",
            "ui_backups_verify_handler",
        )
        verification = (resp.json() if resp.ok else {}).get("verification") or {}
        assert resp.status == 200, ("ui_backups_verify_http", resp.status, resp.text())
        assert verification.get("ok") is True, ("ui_backups_verify_ok", verification)

    _expect_assertion("ui_backups_verify", _check)


def test_fault_wrong_search_hides_seeded_title_reds(ui_owner):
    page, base, storage, ids = ui_owner.page, ui_owner.base, ui_owner.storage, ui_owner.ids
    storage.execute(
        "UPDATE knowledge_objects SET title='ДРУГОЕ-ИМЯ-17' WHERE id=?",
        (ids["alice_ko"],),
    )
    storage.commit()
    open_tab(page, base, "knowledge", "Знания")
    page.fill("#knowledgeSearch", "СМЕТА-СКЛАД")
    click_expect_response(
        page,
        page.locator("button", has_text="Найти").first,
        lambda resp: "/api/admin/knowledge" in resp.url,
        "ui_fault_search_handler",
    )
    wait_ready(page, "Знания")

    def _check() -> None:
        assert KNOWLEDGE_TITLE in app_text(page), "ui_knowledge_search_hit"

    _expect_assertion("ui_knowledge_search_hit", _check)


def test_fault_suppressed_inspect_handler_reds(ui_owner):
    page, base = ui_owner.page, ui_owner.base
    open_tab(page, base, "knowledge", "Знания")
    page.evaluate("() => { actions.inspectKnowledge = undefined; }")
    ko_id = ui_owner.ids["alice_ko"]

    def _check() -> None:
        click_expect_response(
            page,
            page.locator("button", has_text="Инспекция").first,
            lambda r, wanted=ko_id: (
                r.request.method == "GET" and r.url.split("?")[0].endswith(f"/api/admin/knowledge/{wanted}")
            ),
            "ui_knowledge_inspect_handler",
        )

    _expect_assertion("ui_knowledge_inspect_handler", _check)


def test_fault_compact_visible_counter_reds_the_actual_result_oracle(ui_owner):
    test_ui_compacts_run_and_open_literal_counters(ui_owner)
    ui_owner.page.locator("#app .card.stat .value").first.evaluate("element => element.textContent = '99'")
    _expect_assertion("ui_compacts_literal_counters", lambda: _assert_compact_values(ui_owner.page))


def test_fault_foreign_cleanup_http_member_reds_empty_person_oracle(ui_owner):
    injected = []

    def foreign_response(route):
        response = route.fetch()
        data = response.json()
        if data["user_id"] == EMPTY:
            data["items"] = [
                {
                    "knowledge_object": {"id": ui_owner.ids["alice_ko"], "title": KNOWLEDGE_TITLE},
                    "risk_score": 1,
                    "reasons": [],
                    "assessment": {},
                }
            ]
            data["count"] = data["total"] = 1
            injected.append(True)
        route.fulfill(response=response, json=data)

    ui_owner.page.route("**/api/admin/cleanup/legacy?**", foreign_response)
    _expect_assertion(
        "ui_cleanup_empty_result", lambda: test_ui_cleanup_empty_does_not_list_foreign(ui_owner)
    )
    assert injected, "ui_cleanup_fault_exercised"
