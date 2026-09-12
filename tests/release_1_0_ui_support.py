"""Private Chromium/HTTP fixture for the 1.0 admin UI oracles.

Missing Playwright or Chromium is ENVIRONMENT/BLOCKED, never a green skip.
The loopback socket stays bound through startup. Acquisition and teardown share
one finally; the original failure is preserved next to any cleanup failure.
"""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import InboxItem, KnowledgeObject, RawObject, new_id

TOKEN = "U" * 48
ALICE = "local:r10-ui-alice"
BORIS = "local:r10-ui-boris"
EMPTY = "local:r10-ui-empty"
ALICE_NAME = "Алиса-17"
BORIS_NAME = "Борис-17"
EMPTY_NAME = "Катя-пусто"
KNOWLEDGE_TITLE = "СМЕТА-СКЛАД-17"
KNOWLEDGE_BODY = "Поверка весов склада номер семнадцать"
FOREIGN_KNOWLEDGE = "ЧУЖОЕ-ЗНАНИЕ-БОРИСА"
INBOX_BODY = "ПОВЕРКА-ВЕСОВ-INBOX"
FOREIGN_INBOX = "ЧУЖОЙ-INBOX-БОРИСА"
FILE_NAME = "смета-весов-17.pdf"
FOREIGN_FILE = "секрет-бориса.pdf"
CONV_TITLE = "Разговор про поверку"
CONV_MESSAGE = "Скинул смету на поверку весов"
PROMOTE_TITLE = "ПОВЕРКА-ВЕСОВ-ЗНАНИЕ"
AUDIT_USER = "local:r10-ui-audit"
AUDIT_NAME = "Аудит-17"
AUDIT_NEG_USER = "local:r10-ui-audit-neg"
AUDIT_NEG_NAME = "Аудит-негатив-17"
NEW_USER = "local:r10-ui-new"
NEW_NAME = "Новая-17"
STARTUP_SEC = 10
CASE_MS = 15_000
VIEWS = (
    ("dashboard", "Обзор"),
    ("chats", "Переписка"),
    ("inbox", "Inbox"),
    ("knowledge", "Знания"),
    ("timeline", "Хроника"),
    ("graph", "Граф"),
    ("quality", "Качество"),
    ("cleanup", "Ревизия"),
    ("users", "Пользователи"),
    ("activity", "Активность"),
    ("conversations", "Диалоги"),
    ("files", "Файлы"),
    ("sources", "Источники"),
    ("backups", "Резервирование"),
    ("audit", "Аудит"),
    ("compacts", "Сводки"),
    ("diagnostics", "Диагностика"),
)


def environment_blocked(reason: str) -> None:
    pytest.fail(f"ENVIRONMENT/BLOCKED: {reason}")


def _raw(user_id: str, body: str, *, content_type: str, filename: str | None = None) -> RawObject:
    meta: dict[str, Any] = {}
    if filename:
        meta = {"filename": filename, "mime_type": "application/pdf", "size_bytes": len(body.encode("utf-8"))}
    return RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload" if content_type == "file" else "test",
        source_ref=filename or new_id("src"),
        raw_content=body,
        content_type=content_type,
        metadata_json=meta,
    )


def seed_ui_corpus(storage: Any) -> dict[str, str]:
    """Owned private fixture rows. Foreign membership must never paint on Alice."""

    storage.ensure_user(ALICE, source="local", display_name=ALICE_NAME, preset_key="user")
    storage.update_user(ALICE, display_name=ALICE_NAME, preset_key="user")
    storage.ensure_user(BORIS, source="local", display_name=BORIS_NAME, preset_key="user")
    storage.update_user(BORIS, display_name=BORIS_NAME, preset_key="user")
    storage.ensure_user(EMPTY, source="local", display_name=EMPTY_NAME, preset_key="user")
    storage.update_user(EMPTY, display_name=EMPTY_NAME, preset_key="user")

    alice_raw = _raw(ALICE, KNOWLEDGE_BODY, content_type="text")
    storage.store_raw_object(alice_raw)
    alice_ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=ALICE,
        raw_object_id=alice_raw.id,
        content=KNOWLEDGE_BODY,
        title=KNOWLEDGE_TITLE,
        summary=KNOWLEDGE_BODY,
        tags_json=["склад", "смета"],
        metadata_json={"document_date": "2024-03-17"},
        knowledge_kind="document",
    )
    storage.store_knowledge_object(alice_ko)

    foreign_raw = _raw(BORIS, FOREIGN_KNOWLEDGE, content_type="text")
    storage.store_raw_object(foreign_raw)
    boris_ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=BORIS,
        raw_object_id=foreign_raw.id,
        content=FOREIGN_KNOWLEDGE,
        title=FOREIGN_KNOWLEDGE,
        summary=FOREIGN_KNOWLEDGE,
    )
    storage.store_knowledge_object(boris_ko)

    inbox_raw = _raw(ALICE, INBOX_BODY, content_type="text")
    storage.store_raw_object(inbox_raw)
    inbox = InboxItem(
        id=new_id("inb"),
        user_id=ALICE,
        raw_object_id=inbox_raw.id,
        status="pending",
        suggestions_json={"title": INBOX_BODY, "summary": INBOX_BODY, "knowledge_kind": "note"},
        classification_notes=INBOX_BODY,
        quality_score=0.91,
    )
    storage.store_inbox_item(inbox)

    foreign_inbox_raw = _raw(BORIS, FOREIGN_INBOX, content_type="text")
    storage.store_raw_object(foreign_inbox_raw)
    boris_inbox = InboxItem(
        id=new_id("inb"),
        user_id=BORIS,
        raw_object_id=foreign_inbox_raw.id,
        status="pending",
        suggestions_json={"title": FOREIGN_INBOX},
        classification_notes=FOREIGN_INBOX,
    )
    storage.store_inbox_item(boris_inbox)

    file_raw = _raw(ALICE, "PDFBYTES17", content_type="file", filename=FILE_NAME)
    storage.store_raw_object(file_raw)
    foreign_file = _raw(BORIS, "SECRETBYTES", content_type="file", filename=FOREIGN_FILE)
    storage.store_raw_object(foreign_file)

    conversation = storage.create_conversation(ALICE, title=CONV_TITLE)
    storage.store_message(conversation["id"], ALICE, "user", CONV_MESSAGE)
    storage.store_message(conversation["id"], ALICE, "assistant", "Приняла смету в архив")
    other = storage.create_conversation(BORIS, title="Чужой диалог Бориса")
    storage.store_message(other["id"], BORIS, "user", "ЧУЖОЕ-СООБЩЕНИЕ-БОРИСА")
    storage.commit()
    return {
        "alice_ko": alice_ko.id,
        "boris_ko": boris_ko.id,
        "alice_inbox": inbox.id,
        "boris_inbox": boris_inbox.id,
        "alice_file": file_raw.id,
        "alice_conversation": conversation["id"],
        "boris_conversation": other["id"],
        "boris_file": foreign_file.id,
        "alice_message_count": 2,
        "boris_message_count": 1,
    }


def count_sql(storage: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = storage.execute(sql, params).fetchone()
    return int(row["count"] if row is not None else 0)


def expected_overview_counts(storage: Any) -> dict[str, int]:
    """Actor-scoped tiles: overview uses owner token tenant, not the selected person."""

    owner = LEGACY_OWNER_USER_ID
    return {
        "знаний": count_sql(
            storage,
            "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL",
            (owner,),
        ),
        "сущностей": count_sql(
            storage,
            "SELECT COUNT(*) AS count FROM entities WHERE user_id=? AND deleted_at IS NULL",
            (owner,),
        ),
        "inbox": count_sql(
            storage,
            "SELECT COUNT(*) AS count FROM inbox WHERE user_id=? AND status='pending'",
            (owner,),
        ),
        "пользователей": count_sql(storage, "SELECT COUNT(*) AS count FROM users"),
        "диалогов": count_sql(
            storage,
            "SELECT COUNT(*) AS count FROM conversations WHERE user_id=?",
            (owner,),
        ),
        "сообщений": count_sql(
            storage,
            "SELECT COUNT(*) AS count FROM messages WHERE user_id=?",
            (owner,),
        ),
    }


def owned_backup_sqlite_files(storage: Any) -> list[Path]:
    root = Path(storage.settings.backups_dir)
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.sqlite3") if path.is_file() and not path.is_symlink())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class UiOwner:
    base: str
    port: int
    token: str
    app: Any
    storage: Any
    page: Any
    ids: dict[str, str]
    thread: threading.Thread
    browser: Any


def wait_ready(page, title: str | None = None, timeout: int = CASE_MS) -> None:
    page.wait_for_function(
        """() => {
            const app = document.getElementById('app');
            if (!app) return false;
            const text = app.innerText || '';
            return Boolean(text) && !text.includes('Загрузка…');
        }""",
        timeout=timeout,
    )
    if title:
        page.wait_for_function(
            "(wanted) => (document.getElementById('pageTitle') || {}).textContent === wanted",
            arg=title,
            timeout=timeout,
        )


def open_tab(page, base: str, view: str, title: str) -> None:
    page.goto(f"{base}/admin/#{view}", wait_until="domcontentloaded")
    wait_ready(page, title)


def select_person(page, user_id: str) -> None:
    page.select_option("#userSelect", user_id)
    wait_ready(page)


def app_text(page) -> str:
    return page.locator("#app").inner_text()


def folded_text(page) -> str:
    """Chromium innerText reflects CSS text-transform:uppercase on .stat .label."""

    return app_text(page).casefold()


def toast_text(page) -> str:
    return page.locator("#toast").inner_text()


def wait_toast(page, snippet: str, timeout: int = CASE_MS) -> None:
    needle = snippet.casefold()
    page.wait_for_function(
        "(wanted) => ((document.getElementById('toast') || {}).textContent || '').toLowerCase().includes(wanted)",
        arg=needle,
        timeout=timeout,
    )


def wait_app_has(page, snippet: str, timeout: int = CASE_MS) -> None:
    needle = snippet.casefold()
    page.wait_for_function(
        "(wanted) => ((document.getElementById('app') || {}).innerText || '').toLowerCase().includes(wanted)",
        arg=needle,
        timeout=timeout,
    )


def accept_dialogs(page) -> None:
    page.on("dialog", lambda dialog: dialog.accept())


def click_expect_response(page, locator, predicate: Callable[[Any], bool], code: str):
    try:
        with page.expect_response(predicate, timeout=CASE_MS) as pending:
            locator.click()
        return pending.value
    except Exception as exc:  # noqa: BLE001 — missing handler must red the named oracle
        raise AssertionError((code, "handler_or_response_missing", type(exc).__name__, str(exc))) from exc


def dashboard_stat_map(page) -> dict[str, str]:
    cards = page.locator(".card.stat")
    found: dict[str, str] = {}
    for index in range(cards.count()):
        card = cards.nth(index)
        label = card.locator(".label").inner_text().strip().casefold()
        value = card.locator(".value").inner_text().strip().replace("\xa0", "").replace(" ", "")
        found[label] = value
    return found


def assert_dashboard_counts(page, expected: dict[str, int]) -> None:
    got = dashboard_stat_map(page)
    for label, number in expected.items():
        actual = got.get(label.casefold())
        assert actual == str(number), (f"ui_dashboard_{label}_count", actual, number, got)


def _close_quietly(fn: Callable[[], None], bucket: list[BaseException]) -> None:
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001
        bucket.append(exc)


@contextmanager
def start_ui_stack(settings, *, owned: dict[str, Any] | None = None):
    """One reserved loopback socket, one browser+server, teardown in finally."""

    from dataclasses import replace

    import uvicorn

    from friday.server import create_app

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        environment_blocked(f"playwright import failed: {exc}")

    assert not settings.llm_enabled and not settings.workers_enabled
    resources = {} if owned is None else owned
    sock = None
    server = None
    thread = None
    play_cm = None
    browser = None
    original: BaseException | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        resources["socket"] = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = int(sock.getsockname()[1])
        app = create_app(replace(settings, api_token=TOKEN, api_port=port))
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        resources["server"] = server
        bound = sock

        def _run() -> None:
            server.run(sockets=[bound])

        thread = threading.Thread(target=_run, name=f"ui-oracle-{port}", daemon=False)
        thread.start()
        resources["thread"] = thread
        deadline = time.monotonic() + STARTUP_SEC
        while time.monotonic() < deadline and not server.started:
            time.sleep(0.05)
        if not server.started:
            environment_blocked(f"admin server did not start on 127.0.0.1:{port} within {STARTUP_SEC}s")
        try:
            storage = app.state.storage
        except AttributeError as exc:
            environment_blocked(f"app.state.storage missing after lifespan start: {exc}")
        ids = seed_ui_corpus(storage)
        play_cm = sync_playwright().start()
        resources["playwright"] = play_cm
        try:
            browser = play_cm.chromium.launch(timeout=10_000)
            resources["browser"] = browser
        except Exception as exc:  # noqa: BLE001
            environment_blocked(f"chromium unavailable: {exc}")
        page = browser.new_page()
        resources["page"] = page
        page.set_default_timeout(CASE_MS)
        accept_dialogs(page)
        base = f"http://127.0.0.1:{port}"
        page.goto(f"{base}/admin/", wait_until="domcontentloaded")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.reload(wait_until="domcontentloaded")
        wait_ready(page)
        select_person(page, ALICE)
        yield UiOwner(
            base=base,
            port=port,
            token=TOKEN,
            app=app,
            storage=app.state.storage,
            page=page,
            ids=ids,
            thread=thread,
            browser=browser,
        )
    except BaseException as exc:
        original = exc
    finally:
        cleanup_errors: list[BaseException] = []
        if browser is not None:
            _close_quietly(browser.close, cleanup_errors)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            _close_quietly(lambda: thread.join(timeout=10), cleanup_errors)
            if thread.is_alive():
                cleanup_errors.append(
                    RuntimeError(f"ENVIRONMENT/BLOCKED: uvicorn thread leaked after join name={thread.name}")
                )
        if play_cm is not None:
            _close_quietly(play_cm.stop, cleanup_errors)
        if sock is not None:
            _close_quietly(sock.close, cleanup_errors)
        if original is not None and cleanup_errors:
            raise BaseExceptionGroup(
                "ui_owner original failure plus cleanup failure",
                [original, *cleanup_errors],
            )
        if cleanup_errors:
            if len(cleanup_errors) == 1:
                raise cleanup_errors[0]
            raise BaseExceptionGroup("ui_owner cleanup failure", cleanup_errors)
        if original is not None:
            raise original


@pytest.fixture
def ui_owner(settings):
    with start_ui_stack(settings) as harness:
        yield harness
