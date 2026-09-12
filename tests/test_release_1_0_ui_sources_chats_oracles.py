"""Owned Chromium paths for Sources and Chats; no external DB or Telegram delivery."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest

from tests.release_1_0_ui_support import (
    ALICE,
    ALICE_NAME,
    BORIS,
    BORIS_NAME,
    CONV_MESSAGE,
    app_text,
    click_expect_response,
    wait_toast,
)

pytest_plugins = ["tests.release_1_0_ui_support"]

SECRET = "postgresql://fixture_user:r10-secret-only-in-env@192.0.2.9/hr"
ENV = "R10_UI_SOURCE_DSN"
ABSENT_ENV = "R10_UI_SOURCE_ABSENT_DSN"
REPLY = "Жду отчёт по смете в пятницу"
CHAT_ID = "synthetic-ui-alice-chat"


def _equal(actual, expected, code):
    assert actual == expected, (code, actual, expected)


def _rows(storage, table):
    assert table in {"data_sources", "outbound_notifications", "messages", "conversations"}
    return [dict(r) for r in storage.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]


def _response(ui, locator, path, method="GET", person=None):
    def matches(response):
        url = urlsplit(response.url)
        return (
            unquote(url.path) == path
            and response.request.method == method
            and (person is None or parse_qs(url.query).get("user_id") == [person])
        )

    return click_expect_response(ui.page, locator, matches, "ui_actual_handler")


def _no_secret(ui, body):
    observed = json.dumps(body, ensure_ascii=False) + ui.page.content()
    observed += json.dumps(_rows(ui.storage, "data_sources"), ensure_ascii=False)
    for token in (SECRET, "r10-secret-only-in-env", "192.0.2.9", "postgresql://fixture_user"):
        assert token not in observed, "ui_source_secret"


@pytest.fixture
def source_ui(ui_owner, monkeypatch):
    monkeypatch.setenv(ENV, SECRET)
    monkeypatch.delenv(ABSENT_ENV, raising=False)
    for person, name, env in ((ALICE, "hr", ENV), (ALICE, "warehouse", ABSENT_ENV), (BORIS, "billing", ENV)):
        ui_owner.storage.register_data_source(
            person,
            name=name,
            kind="postgres",
            dsn_env=env,
            description=f"{person}:{name}",
            created_by=person,
        )
    return ui_owner


def _source_list(ui):
    from playwright.sync_api import expect

    before = _rows(ui.storage, "data_sources")
    response = _response(
        ui, ui.page.locator("#nav button", has_text="Источники"), "/api/admin/data-sources", person=ALICE
    )
    _equal(response.status, 200, "ui_source_list_status")
    body = response.json()
    _equal(body["user_id"], ALICE, "ui_source_person")
    _equal([s["name"] for s in body["sources"]], ["hr", "warehouse"], "ui_source_members")
    _equal(
        [(s["kind"], s["dsn_env"], s["secret_present"]) for s in body["sources"]],
        [("postgres", ENV, True), ("postgres", ABSENT_ENV, False)],
        "ui_source_connection_state",
    )
    expect(ui.page.locator("#app tbody tr")).to_have_count(2)
    for name, env, state in (
        ("hr", ENV, "переменная задана"),
        ("warehouse", ABSENT_ENV, "переменная не задана"),
    ):
        row = ui.page.locator("#app tbody tr").filter(has=ui.page.locator("td b", has_text=name))
        expect(row).to_contain_text(env)
        expect(row).to_contain_text(state)
    assert "billing" not in app_text(ui.page), "ui_source_foreign"
    _no_secret(ui, body)
    _equal(_rows(ui.storage, "data_sources"), before, "ui_source_read_preserves_all")
    return body


def test_ui_sources_lists_exact_person_and_never_carries_connection_secret(source_ui):
    _source_list(source_ui)


def _source_create_forget(ui):
    from playwright.sync_api import expect

    _source_list(ui)
    before = _rows(ui.storage, "data_sources")
    ui.page.locator("#app button", has_text="Объявить источник").click()
    ui.page.fill("#srcName", "billing")
    ui.page.select_option("#srcKind", "sqlite")
    ui.page.fill("#srcEnv", ABSENT_ENV)
    ui.page.fill("#srcDesc", "Биллинг склада")
    response = _response(
        ui, ui.page.locator("#modalFoot button", has_text="Объявить"), "/api/admin/data-sources", "POST"
    )
    _equal(response.status, 200, "ui_source_create_http")
    _equal(
        response.request.post_data_json,
        {
            "user_id": ALICE,
            "name": "billing",
            "kind": "sqlite",
            "dsn_env": ABSENT_ENV,
            "description": "Биллинг склада",
        },
        "ui_source_create_input",
    )
    stored = ui.storage.get_data_source(ALICE, "billing")
    assert stored is not None, "ui_source_create_persisted"
    _equal(
        (stored["user_id"], stored["name"], stored["kind"], stored["dsn_env"], stored["description"]),
        (ALICE, "billing", "sqlite", ABSENT_ENV, "Биллинг склада"),
        "ui_source_create_tuple",
    )
    _equal(_rows(ui.storage, "data_sources"), before + [stored], "ui_source_create_preserves_siblings")
    _no_secret(ui, response.json())
    row = ui.page.locator("#app tbody tr").filter(has=ui.page.locator("td b", has_text="billing"))
    expect(row).to_have_count(1)
    response = _response(
        ui, row.locator("button", has_text="Забыть"), "/api/admin/data-sources/billing", "DELETE", ALICE
    )
    _equal(response.status, 200, "ui_source_forget_http")
    _equal(response.json(), {"status": "forgotten", "name": "billing"}, "ui_source_forget_output")
    _equal(_rows(ui.storage, "data_sources"), before, "ui_source_forget_exact_effect")
    expect(row).to_have_count(0)


def test_ui_source_create_and_forget_preserve_same_named_foreign_source(source_ui):
    _source_create_forget(source_ui)


def test_ui_source_missing_environment_schema_is_an_explicit_error(source_ui):
    ui = source_ui
    _source_list(ui)
    before = _rows(ui.storage, "data_sources")
    row = ui.page.locator("#app tbody tr", has_text="warehouse")
    response = _response(
        ui, row.locator("button", has_text="Схема"), "/api/admin/data-sources/warehouse/schema", person=ALICE
    )
    _equal(response.status, 409, "ui_source_missing_env_http")
    wait_toast(ui.page, f"Переменная {ABSENT_ENV} не задана")
    _no_secret(ui, response.json())
    _equal(_rows(ui.storage, "data_sources"), before, "ui_source_missing_env_no_effect")


@pytest.mark.parametrize(
    "fault,code",
    [
        ("secret", "ui_source_secret"),
        ("foreign", "ui_source_members"),
        ("missing_create", "ui_source_create_persisted"),
        ("undeleted", "ui_source_forget_exact_effect"),
    ],
)
def test_ui_sources_oracle_rejects_real_http_and_persistence_faults(source_ui, fault, code):
    ui = source_ui
    injected = []

    def corrupt(route):
        method = route.request.method
        wanted = {"secret": "GET", "foreign": "GET", "missing_create": "POST", "undeleted": "DELETE"}[fault]
        if method != wanted:
            route.continue_()
            return
        before = ui.storage.get_data_source(ALICE, "billing") if fault == "undeleted" else None
        response = route.fetch()
        body = response.json()
        if fault == "secret":
            body["sources"][0]["connection_string"] = SECRET
        elif fault == "foreign":
            body["sources"].append(
                {"name": "billing", "kind": "postgres", "dsn_env": ENV, "secret_present": True}
            )
        elif fault == "missing_create":
            ui.storage.forget_data_source(ALICE, "billing")
        else:
            assert before is not None
            columns = ",".join(before)
            ui.storage.execute(
                f"INSERT INTO data_sources ({columns}) VALUES ({','.join('?' for _ in before)})",
                tuple(before.values()),
            )
            ui.storage.commit()
        injected.append(method)
        route.fulfill(response=response, json=body)

    ui.page.route("**/api/admin/data-sources**", corrupt)
    with pytest.raises(AssertionError, match=code):
        (_source_list if fault in {"secret", "foreign"} else _source_create_forget)(ui)
    assert injected, "ui_source_fault_not_injected"


@pytest.fixture
def chat_ui(ui_owner):
    ui_owner.storage.update_user(ALICE, metadata_json={"chat_id": CHAT_ID})
    return ui_owner


def _chat_open(ui, person=ALICE):
    from playwright.sync_api import expect

    response = _response(ui, ui.page.locator("#nav button", has_text="Переписка"), "/api/admin/chats")
    _equal(response.status, 200, "ui_chat_feed_status")
    _equal({r["user_id"] for r in response.json()["items"]}, {ALICE, BORIS}, "ui_chat_feed_members")
    expect(ui.page.locator(".chat-row")).to_have_count(2)
    row = ui.page.locator(f'.chat-row[data-chat-person="{person}"]')
    expect(row).to_contain_text(ALICE_NAME if person == ALICE else BORIS_NAME)
    response = _response(ui, row, f"/api/admin/chats/{person}/messages")
    _equal(response.status, 200, "ui_chat_thread_status")
    expected = [CONV_MESSAGE, "Приняла смету в архив"] if person == ALICE else ["ЧУЖОЕ-СООБЩЕНИЕ-БОРИСА"]
    _equal([m["content"] for m in response.json()["items"]], expected, "ui_chat_thread_members")
    for content in expected:
        expect(ui.page.locator(".thread")).to_contain_text(content)
    assert ("ЧУЖОЕ-СООБЩЕНИЕ-БОРИСА" if person == ALICE else CONV_MESSAGE) not in ui.page.locator(
        ".thread"
    ).inner_text(), "ui_chat_thread_foreign"


def _chat_reply(ui):
    from playwright.sync_api import expect

    _chat_open(ui)
    before = {table: _rows(ui.storage, table) for table in ("messages", "conversations")}
    _equal(_rows(ui.storage, "outbound_notifications"), [], "ui_chat_queue_initial")
    for count in (1, 2):
        ui.page.locator("#replyText").fill(REPLY)
        response = _response(
            ui, ui.page.locator(".reply-box button"), f"/api/admin/chats/{ALICE}/reply", "POST"
        )
        _equal(response.status, 200, "ui_chat_reply_status")
        _equal(response.json(), {"queued": True, "user_id": ALICE, "chat_id": CHAT_ID}, "ui_chat_reply_http")
        rows = _rows(ui.storage, "outbound_notifications")
        _equal(len(rows), count, "ui_chat_reply_count")
        for row in rows:
            _equal(
                tuple(row[k] for k in ("user_id", "chat_id", "kind", "body", "status", "attempts")),
                (ALICE, CHAT_ID, "owner_reply", f"💬 Ответ от владельца:\n\n{REPLY}", "pending", 0),
                "ui_chat_reply_target_and_body",
            )
        _equal(len({r["dedup_key"] for r in rows}), count, "ui_chat_repeat_is_independent")
        wait_toast(ui.page, "Ответ поставлен в очередь")
        expect(ui.page.locator("#replyText")).to_have_value("")
    _equal({table: _rows(ui.storage, table) for table in before}, before, "ui_chat_reply_preserves_history")


def test_ui_chat_reply_reaches_exact_person_and_two_clicks_queue_two_labelled_messages(chat_ui):
    _chat_reply(chat_ui)


def test_ui_chat_without_transport_has_no_reply_box_or_effect(chat_ui):
    from playwright.sync_api import expect

    _chat_open(chat_ui, BORIS)
    expect(chat_ui.page.locator(".chat-thread")).to_contain_text("ответить некуда")
    expect(chat_ui.page.locator("#replyText")).to_have_count(0)
    _equal(_rows(chat_ui.storage, "outbound_notifications"), [], "ui_chat_without_transport_no_effect")


def test_ui_chat_auto_refresh_shows_new_message_and_preserves_unsent_draft(chat_ui):
    from playwright.sync_api import expect

    ui = chat_ui
    _chat_open(ui)
    ui.page.locator("#replyText").fill("Черновик — не отправлять")
    ui.storage.store_message(ui.ids["alice_conversation"], ALICE, "user", "ПРИБОРЫ-ПРИВЕЗЛИ-040")
    ui.storage.commit()
    expect(ui.page.locator(".thread")).to_contain_text("ПРИБОРЫ-ПРИВЕЗЛИ-040", timeout=15_000)
    expect(ui.page.locator("#replyText")).to_have_value("Черновик — не отправлять")
    _equal(_rows(ui.storage, "outbound_notifications"), [], "ui_chat_draft_no_effect")


@pytest.mark.parametrize(
    "fault,code",
    [
        ("wrong_thread", "ui_chat_thread_members"),
        ("missing_reply", "ui_chat_reply_count"),
        ("wrong_recipient", "ui_chat_reply_target_and_body"),
    ],
)
def test_ui_chat_oracle_rejects_real_thread_and_delivery_queue_faults(chat_ui, fault, code):
    ui = chat_ui
    injected = []

    def corrupt(route):
        response = route.fetch()
        body = response.json()
        if fault == "wrong_thread":
            body["items"][0]["content"] = "ЧУЖОЕ-СООБЩЕНИЕ-БОРИСА"
        elif fault == "missing_reply":
            ui.storage.execute("DELETE FROM outbound_notifications WHERE kind='owner_reply'")
            ui.storage.commit()
        else:
            ui.storage.execute(
                "UPDATE outbound_notifications SET user_id=?, chat_id=? WHERE kind='owner_reply'",
                (BORIS, "synthetic-wrong-person"),
            )
            ui.storage.commit()
        injected.append(True)
        route.fulfill(response=response, json=body)

    endpoint = "messages**" if fault == "wrong_thread" else "reply"
    ui.page.route(f"**/api/admin/chats/{quote(ALICE, safe='')}/{endpoint}", corrupt)
    with pytest.raises(AssertionError, match=code):
        (_chat_open if fault == "wrong_thread" else _chat_reply)(ui)
    assert injected, "ui_chat_fault_not_injected"
