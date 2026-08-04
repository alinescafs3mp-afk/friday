"""Вкладка «Источники» проверяется настоящим браузером, а не разбором файла.

`node --check` разбирает app.js и ничего не говорит о том, доедет ли нажатие до
обработчика: кнопка рисуется, клик уходит в диспетчер и молча теряется, если
действие не зарегистрировано. Тот же класс уже ловили на ленте переписки.

Второе свойство здесь важнее косметики: панель показывает ИМЯ переменной
окружения и признак «задана / не задана». Строки подключения на экране нет и
быть не может — иначе пароль от чужой боевой базы жил бы в разметке страницы.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

TOKEN = "S" * 48
SECRET = "postgresql://user:s3cret@10.0.0.9/hr"


@pytest.fixture
def live_admin(settings, monkeypatch):
    from dataclasses import replace

    import uvicorn

    from friday.server import create_app

    monkeypatch.setenv("UI_HR_DSN", SECRET)
    # Порт занимает ОС, а не константа в файле: гейт идёт двенадцатью рабочими,
    # и на зашитом номере соседний тест того же файла не поднимался — а тест,
    # который молча пропустился, ничего не охраняет.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    app = create_app(replace(settings, api_token=TOKEN, api_port=port))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        pytest.skip("the admin server did not start")
    storage = app.state.storage
    # Панель показывает источники ВЫБРАННОГО человека, а выбирает она первого из
    # `/api/admin/users`. Засев «какому-нибудь» пользователю дал бы пустой экран
    # при исправном коде — тест обязан сеять тому же, кого выберет панель.
    listing = httpx.get(
        f"http://127.0.0.1:{port}/api/admin/users",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    shown = (listing.json().get("items") or [{}])[0].get("id")
    if not shown:
        pytest.skip("в базе нет ни одного пользователя — панели нечего показывать")
    storage.register_data_source(
        shown,
        name="hr",
        kind="postgres",
        dsn_env="UI_HR_DSN",
        description="кадровая база склада",
        created_by=shown,
    )
    storage.register_data_source(
        shown,
        name="warehouse",
        kind="mysql",
        dsn_env="UI_WAREHOUSE_DSN",
        created_by=shown,
    )
    try:
        yield f"http://127.0.0.1:{port}"
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


def test_the_tab_opens_and_lists_the_sources(live_admin):
    """Мутация: убрать `renderers.sources` — вкладка откроется пустой, тест краснеет."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page = _open(play, live_admin)
        page.locator("#nav button", has_text="Источники").click()
        page.wait_for_timeout(900)
        assert page.locator("#pageTitle").inner_text() == "Источники"

        body = page.locator("#app").inner_text()
        assert "hr" in body and "warehouse" in body, "объявленные источники не показаны"
        assert "UI_HR_DSN" in body, "не названа переменная, из которой берётся подключение"
        # Задана переменная или нет — это разные состояния, и человек их видит.
        assert "переменная задана" in body
        assert "переменная не задана" in body
        browser.close()


def test_the_page_never_carries_the_connection_string(live_admin):
    """Секрет не должен попасть ни в разметку, ни в ответ, который её питает."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page = _open(play, live_admin)
        page.goto(f"{live_admin}/admin/#sources", wait_until="networkidle")
        page.wait_for_timeout(900)
        html = page.content()
        for leak in ("s3cret", "10.0.0.9", "postgresql://"):
            assert leak not in html, f"страница содержит {leak}"
        browser.close()


def test_declaring_a_source_from_the_panel_works(live_admin):
    """Кнопка обязана доехать до обработчика: нарисованная — ещё не работающая."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page = _open(play, live_admin)
        page.goto(f"{live_admin}/admin/#sources", wait_until="networkidle")
        page.wait_for_timeout(900)
        page.locator("button", has_text="Объявить источник").click()
        page.wait_for_timeout(300)
        page.fill("#srcName", "billing")
        page.select_option("#srcKind", "sqlite")
        page.fill("#srcEnv", "UI_BILLING_DSN")
        page.fill("#srcDesc", "биллинг")
        page.locator("#modalFoot button", has_text="Объявить").click()
        page.wait_for_timeout(900)

        body = page.locator("#app").inner_text()
        assert "billing" in body, "объявленный источник не появился в списке"

        # И обратное действие тоже должно доезжать.
        row = page.locator("tr", has_text="billing")
        row.locator("button", has_text="Забыть").click()
        page.wait_for_timeout(900)
        assert "billing" not in page.locator("#app").inner_text()
        browser.close()
