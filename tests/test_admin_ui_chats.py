"""Вкладка «Переписка» в настоящем браузере.

Заказ владельца 2026-08-02: «видеть мир из глаз Пятницы — кто и что скинул и
написал, и иметь возможность ответить».

`node --check` доказывает, что файл разбирается. Он не доказывает, что экран
рисуется, что запрос доходит до маршрута, что клик по человеку открывает его
переписку и что кнопка ответа кладёт сообщение в очередь доставки — а именно это
и есть заказанное. Поэтому здесь работает настоящий Chromium против настоящего
сервера.

Пропускается, когда Playwright или браузер не установлены: отсутствующий
инструмент — это отсутствующий инструмент, а не сломанный продукт.
"""

from __future__ import annotations

import threading
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

PORT = 8794
TOKEN = "C" * 48


def _seed(storage) -> None:
    storage.ensure_user("usr_petrov", source="telegram", display_name="Петров", preset_key="user")
    storage.update_user("usr_petrov", display_name="Петров", metadata_json={"chat_id": "5001"})
    storage.ensure_user("usr_bez_chata", source="local", display_name="Без чата", preset_key="user")
    storage.update_user("usr_bez_chata", display_name="Без чата")

    conversation = storage.create_conversation("usr_petrov", title="Разговор Петрова")
    storage.store_message(conversation["id"], "usr_petrov", "user", "Скинул смету на поверку весов")
    storage.store_message(conversation["id"], "usr_petrov", "assistant", "Принял, записала в архив")

    other = storage.create_conversation("usr_bez_chata", title="Разговор без чата")
    storage.store_message(other["id"], "usr_bez_chata", "user", "А я пишу из веба")
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
        yield f"http://127.0.0.1:{PORT}", app
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_the_owner_sees_who_wrote_and_can_answer(live_admin):
    from playwright.sync_api import sync_playwright

    base, app = live_admin
    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — отсутствующий браузер это не провал продукта
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        console: list[str] = []
        requests: list[str] = []
        page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))
        page.on("request", lambda request: requests.append(request.url))

        page.goto(f"{base}/admin/", wait_until="networkidle")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.reload(wait_until="networkidle")

        nav = page.locator("#nav button", has_text="Переписка")
        assert nav.count() == 1, "вкладки «Переписка» нет в меню"
        nav.click()
        page.wait_for_timeout(900)
        assert page.locator("#pageTitle").inner_text() == "Переписка"

        # Лента: люди, которые писали, с превью последнего сообщения.
        rows = page.locator(".chat-row")
        assert rows.count() >= 2, f"в ленте {rows.count()} человек, ожидалось минимум двое"
        feed_text = page.locator(".chat-list").inner_text()
        assert "Петров" in feed_text
        assert "смету на поверку весов" in feed_text, "превью последнего сообщения не видно"
        assert "нет чата" in feed_text, "не помечен человек, которому нельзя ответить"

        # Клик открывает переписку этого человека.
        requests.clear()
        page.locator(".chat-row", has_text="Петров").first.click()
        page.wait_for_timeout(900)
        thread = page.locator(".thread").inner_text()
        assert "Скинул смету" in thread, "сообщение человека не показано"
        assert "Принял, записала" in thread, "ответ Пятницы не показан"
        assert sum("/api/admin/chats/usr_petrov/messages?limit=500" in url for url in requests) == 1
        assert not any("/api/admin/chats?limit=" in url for url in requests), (
            "клик повторно загрузил тяжёлую ленту людей"
        )
        assert not any("/api/admin/conversations?" in url for url in requests), (
            "клик снова собирает переписку каскадом по разговорам"
        )

        # И ответ уходит в очередь доставки.
        page.locator("#replyText").fill("Петров, жду отчёт до пятницы")
        page.locator(".reply-box button").click()
        page.wait_for_timeout(900)

        queued = app.state.storage.execute(
            "SELECT user_id, chat_id, body, kind FROM outbound_notifications WHERE kind='owner_reply'"
        ).fetchall()
        assert queued, "ответ не поставлен в очередь"
        assert str(queued[0]["user_id"]) == "usr_petrov"
        assert str(queued[0]["chat_id"]) == "5001"
        assert "жду отчёт до пятницы" in str(queued[0]["body"])
        assert "владельца" in str(queued[0]["body"]), (
            "человек не поймёт, что это ответ владельца, а не сочинение Пятницы"
        )

        errors = [line for line in console if line.startswith(("error", "pageerror"))]
        assert not errors, f"ошибки в консоли браузера: {errors}"
        browser.close()


def test_a_person_without_a_chat_gets_no_reply_box(live_admin):
    """Кнопка, которая не может сработать, хуже её отсутствия."""
    from playwright.sync_api import sync_playwright

    base, _ = live_admin
    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{base}/admin/", wait_until="networkidle")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.reload(wait_until="networkidle")
        page.locator("#nav button", has_text="Переписка").click()
        page.wait_for_timeout(800)
        page.locator(".chat-row", has_text="Без чата").first.click()
        page.wait_for_timeout(800)
        assert page.locator("#replyText").count() == 0, "предложили ответить туда, куда нельзя"
        assert "ответить некуда" in page.locator(".chat-thread").inner_text()
        browser.close()


def test_a_new_message_appears_without_pressing_f5(live_admin):
    """Заказ владельца: «хотелось бы, чтобы было как настоящий телеграм».

    Раньше новое сообщение появлялось только после обновления страницы. Здесь
    проверяется ровно заказанное: сообщение кладётся в базу СНАРУЖИ, страница
    при этом не трогается, и оно само появляется на экране.

    Ждать приходится дольше периода опроса (4 с) — это цена того, что опрос
    дешёвый и не держит соединение.
    """
    from playwright.sync_api import sync_playwright

    base, app = live_admin
    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        console: list[str] = []
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))

        page.goto(f"{base}/admin/", wait_until="networkidle")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.reload(wait_until="networkidle")
        page.locator("#nav button", has_text="Переписка").click()
        page.wait_for_timeout(900)
        page.locator(".chat-row", has_text="Петров").first.click()
        page.wait_for_timeout(900)

        assert "приборы привезли" not in page.locator(".thread").inner_text()

        # Набранный ответ не должен пропасть при автообновлении: человек мог
        # печатать в тот самый момент, когда пришло новое сообщение.
        page.locator("#replyText").fill("черновик ответа")

        storage = app.state.storage
        conversation = storage.list_conversations("usr_petrov", limit=1)[0]
        storage.store_message(conversation["id"], "usr_petrov", "user", "приборы привезли, принимайте")
        storage.commit()

        page.wait_for_selector("text=приборы привезли", timeout=15000)
        assert page.locator("#replyText").input_value() == "черновик ответа", (
            "автообновление стёрло набранный ответ"
        )
        errors = [line for line in console if line.startswith("pageerror")]
        assert not errors, f"ошибки в консоли браузера: {errors}"
        browser.close()


def test_the_polling_stops_when_the_tab_is_left(live_admin):
    """Таймер живёт только на своей вкладке.

    Иначе панель ходила бы в сеть раз в четыре секунды на любом экране — плата
    ни за что, и лишние строки в журнале доступа.

    Предохранителя здесь ДВА: `stopLiveChats()` при уходе с вкладки снимает сам
    интервал, а проверка вида внутри тика не даёт запросу уйти, даже если
    интервал почему-то пережил переход. Мутация только первого этот тест НЕ
    роняет — второй прикрывает, — и это проверено отдельно. Так и задумано:
    поведение важнее того, каким из двух способов оно обеспечено.
    """
    from playwright.sync_api import sync_playwright

    base, _ = live_admin
    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{base}/admin/", wait_until="networkidle")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.reload(wait_until="networkidle")

        calls: list[str] = []
        page.on("request", lambda request: calls.append(request.url))

        page.locator("#nav button", has_text="Переписка").click()
        page.wait_for_timeout(5200)
        polled_here = sum(1 for url in calls if "/chats/cursor" in url)
        assert polled_here >= 1, "на своей вкладке опрос не идёт — обновления не будет"

        page.locator("#nav button", has_text="Обзор").click()
        page.wait_for_timeout(900)
        calls.clear()
        page.wait_for_timeout(5200)
        polled_elsewhere = sum(1 for url in calls if "/chats/cursor" in url)
        assert polled_elsewhere == 0, "опрос переписки продолжается на чужой вкладке"
        browser.close()
