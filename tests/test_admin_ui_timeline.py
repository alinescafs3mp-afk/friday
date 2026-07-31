"""Экран «Хроника», открытый в настоящем браузере.

Экран целиком живёт в JavaScript: `node --check` доказывает, что файл разбирается, но
не то, что столбики нарисовались, что нажатие на год сузило окно до месяцев и что
бездатные объекты названы, а не спрятаны. Это и есть сам продукт, поэтому здесь
работает настоящий Chromium против настоящего сервера.

Пропускается, если Playwright или его браузер не установлены: отсутствующий
инструмент — это отсутствующий инструмент, а не сломанный продукт.
"""

from __future__ import annotations

import threading
import time

import pytest

from jericho.storage.models import KnowledgeObject, RawObject, new_id

playwright_api = pytest.importorskip("playwright.sync_api")

PORT = 8793
TOKEN = "T" * 48


def _document(storage, user_id: str, title: str, document_date: str | None) -> None:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=title,
        raw_content=title,
        content_type="text/plain",
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=user_id,
            raw_object_id=raw.id,
            content=title,
            title=title,
            metadata_json={"document_date": document_date} if document_date else {},
        )
    )


def _seed(storage) -> None:
    storage.ensure_user("usr_ivan", source="test", display_name="Иван", preset_key="user")
    # Два года и два месяца внутри одного из них: достаточно, чтобы проверить и
    # годовые столбики, и провал внутрь года.
    for date_value in ("2023-05-04", "2024-03-01", "2024-03-17", "2024-11-02"):
        _document(storage, "usr_ivan", f"документ {date_value}", date_value)
    _document(storage, "usr_ivan", "БЕЗДАТНЫЙ", None)
    storage.commit()


@pytest.fixture
def live_admin(settings):
    from dataclasses import replace

    import uvicorn

    from jericho.server import create_app

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


def test_the_timeline_screen_draws_zooms_and_admits_what_it_omits(live_admin):
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

        # Экран рисует корпус ВЫБРАННОГО аккаунта, а по умолчанию выбран первый в
        # списке — владелец, у которого документов нет. Выбираем засеянный явно, иначе
        # тест проверял бы пустой экран и проходил бы, что бы ни сломалось.
        page.select_option("#userSelect", "usr_ivan")
        page.wait_for_timeout(600)

        nav = page.locator("#nav button", has_text="Хроника")
        assert nav.count() == 1, "пункта «Хроника» нет в боковой панели"
        nav.click()
        page.wait_for_timeout(900)

        bars = page.locator("#app .tl-bar")
        if bars.count() != 2:
            problems.append(f"ожидались столбики за 2023 и 2024, нарисовано {bars.count()}")
        body = page.locator("#app").inner_text()
        # Бездатный обязан быть назван числом, а не молча пропасть.
        if "1" not in body or "собственной даты" not in body:
            problems.append("экран не сказал, сколько объектов остались без своей даты")
        if "БЕЗДАТНЫЙ" in body:
            problems.append("объект без своей даты попал в ленту, хотя его время неизвестно")

        # Провал внутрь года: 2024 раскрывается в месяцы, и в ленте остаются только его.
        page.locator("#app .tl-bar", has_text="2024").first.click()
        page.wait_for_timeout(900)
        body = page.locator("#app").inner_text()
        if "по месяцам" not in body:
            problems.append("нажатие на год не сменило крупность на месяцы")
        if "2023-05-04" in body:
            problems.append("после сужения до 2024 в ленте остался документ 2023 года")
        months = page.locator("#app .tl-bar")
        if months.count() != 2:
            problems.append(f"ожидались март и ноябрь 2024, нарисовано {months.count()}")

        # Возврат ко всему корпусу должен вернуть годовые столбики.
        page.locator("#app button", has_text="Ко всему корпусу").first.click()
        page.wait_for_timeout(900)
        if page.locator("#app .tl-bar").count() != 2 or "по годам" not in page.locator("#app").inner_text():
            problems.append("возврат ко всему корпусу не вернул годовую крупность")

        browser.close()

    noisy = [line for line in console if "error" in line.casefold()]
    assert not problems, "; ".join(problems) + (f" | консоль: {noisy}" if noisy else "")
    assert not noisy, f"ошибки в консоли браузера: {noisy}"
