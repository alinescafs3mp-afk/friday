"""Обновление страницы не должно возвращать на «Обзор».

Замечание владельца 2026-08-02: он работал во вкладке, обновлял страницу — и
каждый раз начинал заново с главной.

Проверяется настоящим браузером: вкладка хранится в адресе и в сессии, и обе
дороги ведут обратно туда, где человек был. `node --check` разбирает файл, но
ничего не говорит о том, что после F5 откроется тот же экран.
"""

from __future__ import annotations

import threading
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

PORT = 8797
TOKEN = "K" * 48


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
    try:
        yield f"http://127.0.0.1:{PORT}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _open(play, base):
    try:
        browser = play.chromium.launch()
    except Exception as exc:  # noqa: BLE001 — отсутствующий браузер это не провал продукта
        pytest.skip(f"no chromium available: {exc}")
    page = browser.new_page()
    page.goto(f"{base}/admin/", wait_until="networkidle")
    page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
    page.reload(wait_until="networkidle")
    return browser, page


def test_a_reload_keeps_the_open_tab(live_admin):
    """Мутация: убрать `startingView()` из старта — тест краснеет."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page = _open(play, live_admin)
        page.locator("#nav button", has_text="Аудит").click()
        page.wait_for_timeout(700)
        assert page.locator("#pageTitle").inner_text() == "Аудит"
        assert page.evaluate("location.hash") == "#audit", "вкладка не попала в адрес"

        page.reload(wait_until="networkidle")
        page.wait_for_timeout(900)
        assert page.locator("#pageTitle").inner_text() == "Аудит", "после обновления вернуло на главную"
        active = page.locator("#nav button.active").inner_text()
        assert "Аудит" in active, "в меню подсвечена не та вкладка"
        browser.close()


def test_the_address_opens_the_tab_directly(live_admin):
    """Адрес — это ссылка: по ней должен открываться тот же экран."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{live_admin}/admin/", wait_until="networkidle")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.goto(f"{live_admin}/admin/#chats", wait_until="networkidle")
        page.wait_for_timeout(900)
        assert page.locator("#pageTitle").inner_text() == "Переписка"
        browser.close()


def test_an_unknown_address_falls_back_to_the_overview(live_admin):
    """Опечатка в адресе не должна оставлять пустой экран."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{live_admin}/admin/", wait_until="networkidle")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.goto(f"{live_admin}/admin/#такой-вкладки-нет", wait_until="networkidle")
        page.wait_for_timeout(800)
        assert page.locator("#pageTitle").inner_text() == "Обзор"
        browser.close()
