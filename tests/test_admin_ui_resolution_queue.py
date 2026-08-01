"""Карточка «Entity Resolution», открытая в настоящем браузере.

Значок карточки печатал `rrows.length` — длину той страницы, которую вернул
маршрут. На корпусе владельца кандидатур 45 947, а маршрут отдавал 500, и оператор
видел «500» без единого признака, что это не всё. Соседние четыре карточки того же
экрана давно показывают `pager(...)` с честным «1–100 из N», эта — нет.

`node --check` доказывает, что файл разбирается, но не то, что в значке стоит
общее число и что «Вперёд →» показывает следующую сотню. Поэтому здесь настоящий
Chromium против настоящего сервера.

Пропускается, если Playwright или его браузер не установлены: отсутствующий
инструмент — это отсутствующий инструмент, а не сломанный продукт.
"""

from __future__ import annotations

import threading
import time

import pytest

from friday.storage.models import Entity, EntityResolutionCandidate, EntityType, new_id

pytest.importorskip("playwright.sync_api")

PORT = 8795
TOKEN = "R" * 48
# На единицу больше боевой страницы (PAGE=100): ровно та граница, на которой
# «сколько показано» и «сколько есть» расходятся впервые.
CANDIDATES = 101


def _seed(storage) -> None:
    storage.ensure_user("usr_ivan", source="test", display_name="Иван", preset_key="user")
    for index in range(CANDIDATES):
        left = Entity(
            id=new_id("ent"),
            user_id="usr_ivan",
            name=f"Иванов И.И. {index}",
            entity_type=EntityType.PERSON,
        )
        right = Entity(
            id=new_id("ent"),
            user_id="usr_ivan",
            name=f"Иванов Иван {index}",
            entity_type=EntityType.PERSON,
        )
        storage.create_entity(left)
        storage.create_entity(right)
        storage.store_resolution_candidate(
            EntityResolutionCandidate(
                id=new_id("res"),
                user_id="usr_ivan",
                entity_a_id=left.id,
                entity_b_id=right.id,
                confidence=0.9 - index * 0.01,
                resolution_method="name_similarity",
                evidence_json={"reason": "совпадение имени"},
            )
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


def test_the_resolution_card_shows_the_whole_queue_and_pages_through_it(live_admin):
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - отсутствующий браузер это не провал
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        console: list[str] = []
        page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))

        page.goto(f"{live_admin}/admin/", wait_until="networkidle")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.reload(wait_until="networkidle")
        page.select_option("#userSelect", "usr_ivan")
        page.wait_for_timeout(600)

        nav = page.locator("#nav button", has_text="Граф")
        assert nav.count() == 1, "пункта «Граф» нет в боковой панели"
        nav.click()
        page.wait_for_timeout(1200)

        card = page.locator("#app section.card", has=page.locator("h2", has_text="Entity Resolution"))
        if card.count() != 1:
            pytest.fail(f"карточка Entity Resolution не найдена: {card.count()}")

        badge = card.locator(".badge").first.inner_text().strip()
        if badge != str(CANDIDATES):
            problems.append(f"значок показывает {badge!r}, а в очереди {CANDIDATES} кандидатур")

        text = card.inner_text()
        if f"1–100 из {CANDIDATES}" not in text:
            problems.append(f"карточка не называет полный объём очереди: {text[:300]!r}")
        if "Вперёд" not in text or "Назад" not in text:
            problems.append("у карточки нет перелистывания, хотя у четырёх соседних оно есть")

        # Вторая страница показывает ровно то, что первая скрыла.
        card.locator("button", has_text="Вперёд").first.click()
        page.wait_for_timeout(1200)
        card = page.locator("#app section.card", has=page.locator("h2", has_text="Entity Resolution"))
        text = card.inner_text()
        if f"101–101 из {CANDIDATES}" not in text:
            problems.append(f"«Вперёд →» не показал 101-ю кандидатуру: {text[:300]!r}")
        rows = card.locator("tbody tr")
        if rows.count() != 1:
            problems.append(f"на второй странице {rows.count()} строк вместо одной")

        if console:
            problems.append(f"ошибки в консоли браузера: {console[:3]}")
        browser.close()

    assert not problems, "; ".join(problems)
