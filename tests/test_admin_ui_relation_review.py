"""Массовый разбор предложенных связей: спросить и не потерять.

Две асимметрии одного экрана, обе видны только в браузере.

1. «Принять выбранные» спрашивало подтверждение, «Отклонить выбранные» — нет,
   хотя отклонение тоже массовое и тоже уводит строки из очереди. То же у
   противоречий: подтверждения не было ни у одной из двух кнопок.

2. Отклонённая связь НЕ удаляется — строка остаётся со статусом `rejected`,
   маршрут `/api/admin/relation-candidates` принимает статус параметром и
   хранилище его разрешает. Но в админке не было кнопки, которая бы его
   передала, и «отклонить» выглядело безвозвратным. У соседней карточки
   «Противоречия и дубликаты» такой переключатель есть с 2026-07-30 — сюда его
   просто не перенесли.

Пропускается, если Playwright или его браузер не установлены.
"""

from __future__ import annotations

import threading
import time

import pytest

from friday.storage.models import Entity, EntityType, new_id

pytest.importorskip("playwright.sync_api")

PORT = 8796
TOKEN = "L" * 48


def _seed(storage) -> None:
    storage.ensure_user("usr_ivan", source="test", display_name="Иван", preset_key="user")
    people = []
    for name in ("Иванов Иван", "Петров Пётр", "ООО «Кровля»"):
        entity = Entity(
            id=new_id("ent"),
            user_id="usr_ivan",
            name=name,
            entity_type=EntityType.PERSON if "О" not in name[:1] else EntityType.ORGANIZATION,
        )
        storage.create_entity(entity)
        people.append(entity)
    for index in range(3):
        storage.store_relation_candidate(
            "usr_ivan",
            people[index % len(people)].id,
            people[(index + 1) % len(people)].id,
            "works_on",
            confidence=0.8 - index * 0.05,
            evidence={"reason": "совместное упоминание"},
        )
    storage.commit()


@pytest.fixture
def live_admin(settings):
    from dataclasses import replace

    import uvicorn

    from friday.server import create_app

    app = create_app(replace(settings, api_token=TOKEN, api_port=PORT))
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        pytest.skip("the admin server did not start")
    _seed(app.state.storage)
    try:
        yield f"http://127.0.0.1:{PORT}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _open_graph(page, base: str):
    page.goto(f"{base}/admin/", wait_until="networkidle")
    page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
    page.reload(wait_until="networkidle")
    page.select_option("#userSelect", "usr_ivan")
    page.wait_for_timeout(600)
    page.locator("#nav button", has_text="Граф").click()
    page.wait_for_timeout(1200)


def _relations_card(page):
    return page.locator("#app section.card", has=page.locator("h2", has_text="Предлагаемые связи"))


def test_bulk_rejection_asks_first_and_leaves_the_rows_findable(live_admin):
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - отсутствующий браузер это не провал
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        console: list[str] = []
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))

        _open_graph(page, live_admin)
        card = _relations_card(page)
        if card.count() != 1:
            pytest.fail("карточка «Предлагаемые связи» не найдена")

        # Массовое отклонение обязано спросить — и сказать, скольких оно касается.
        asked: list[str] = []

        def dismiss_dialog(dialog):
            asked.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", dismiss_dialog)
        card.locator("button", has_text="Выбрать все").click()
        card.locator("button", has_text="Отклонить выбранные").click()
        page.wait_for_timeout(500)
        if not asked:
            problems.append("массовое отклонение сработало без подтверждения")
        elif "3" not in asked[0]:
            problems.append(f"подтверждение не называет число затрагиваемых связей: {asked[0]!r}")

        # Отказ в диалоге означает, что ничего не произошло.
        page.wait_for_timeout(600)
        rows_after_dismiss = _relations_card(page).locator("tbody tr").count()
        if rows_after_dismiss != 3:
            problems.append(f"после отказа в диалоге строк {rows_after_dismiss} вместо 3")

        # Теперь соглашаемся: прежний слушатель снимается, иначе диалог получит
        # первый из двух и снова откажется.
        page.remove_listener("dialog", dismiss_dialog)
        page.on("dialog", lambda dialog: dialog.accept())
        card = _relations_card(page)
        card.locator("button", has_text="Выбрать все").click()
        card.locator("button", has_text="Отклонить выбранные").click()
        page.wait_for_timeout(1500)

        card = _relations_card(page)
        if card.locator("tbody tr").count():
            problems.append("после отклонения строки остались в очереди «предложены»")

        # И вот главное: отклонённые ВИДНЫ, а не исчезли навсегда.
        card.locator("button", has_text="отклонены").click()
        page.wait_for_timeout(1200)
        card = _relations_card(page)
        shown = card.locator("tbody tr").count()
        if shown != 3:
            problems.append(f"фильтр «отклонены» показал {shown} строк вместо 3 — отклонённое не найти")

        if console:
            problems.append(f"ошибки в консоли браузера: {console[:3]}")
        browser.close()

    assert not problems, "; ".join(problems)
