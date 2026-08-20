"""The Активность screen, opened in a real browser.

`node --check` proves the file parses. It does not prove the screen renders, that
its fetch reaches the endpoint, that the delegated click handlers are wired, or
that typing «Ивану» into the name box lands on the right account — and those are
the product. So this drives an actual Chromium against an actual server.

Skipped when Playwright or its browser is not installed, because a missing browser
is a missing tool, not a failing product.
"""

from __future__ import annotations

import hashlib
import threading
import time

import pytest

from friday.storage.models import RawObject, new_id

playwright_api = pytest.importorskip("playwright.sync_api")

PORT = 8791
TOKEN = "T" * 48
_APP = None  # set by the live_admin fixture: same process, same database


def _seed(storage) -> None:
    for name, user_id in (("Иван", "usr_ivan"), ("Анна", "usr_anna")):
        storage.ensure_user(user_id, source="test", display_name=name, preset_key="user")
        storage.update_user(user_id, display_name=name, preset_key="user")
    for index in range(7):
        uploaded = bool(index % 2)
        raw = RawObject(
            id=new_id("raw"),
            user_id="usr_ivan",
            source="upload" if uploaded else "telegram",
            source_ref=new_id("src"),
            raw_content=f"Материал номер {index} про склад и смету",
            content_type="file" if uploaded else "text",
            content_hash=hashlib.sha256(str(index).encode()).hexdigest(),
            metadata_json={"filename": f"файл-{index}.pdf", "size_bytes": 1000 + index} if uploaded else {},
        )
        storage.store_raw_object(raw)
        storage.execute(
            "UPDATE raw_objects SET received_at=? WHERE id=?",
            (f"2026-07-{10 + index:02d}T10:00:00+00:00", raw.id),
        )
    # Somebody else's row, on a day inside the same window: it must never appear.
    other = RawObject(
        id=new_id("raw"),
        user_id="usr_anna",
        source="telegram",
        source_ref=new_id("src"),
        raw_content="ЧУЖАЯЗАМЕТКА",
        content_type="text",
        content_hash=hashlib.sha256(b"anna").hexdigest(),
    )
    storage.store_raw_object(other)
    storage.execute(
        "UPDATE raw_objects SET received_at=? WHERE id=?", ("2026-07-12T10:00:00+00:00", other.id)
    )
    storage.commit()


@pytest.fixture
def live_admin(settings, tmp_path):
    from dataclasses import replace

    import uvicorn

    from friday.server import create_app

    global _APP
    app = create_app(replace(settings, api_token=TOKEN, api_port=PORT))
    _APP = app
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


def test_the_activity_screen_works_in_a_browser(live_admin):
    from playwright.sync_api import expect, sync_playwright

    problems: list[str] = []
    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - a missing browser binary is not a failure
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        console: list[str] = []
        page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))

        page.goto(f"{live_admin}/admin/", wait_until="networkidle")
        page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
        page.reload(wait_until="networkidle")

        nav = page.locator("#nav button", has_text="Активность")
        assert nav.count() == 1, "the Активность entry is not in the sidebar"
        nav.click()
        # The activity request includes four explicitly requested analyses.  A
        # fixed sub-second sleep raced that real request under a saturated gate
        # and inspected the intermediate «Загрузка…» card.  Wait for the product
        # state itself; this still fails if the endpoint or renderer never lands.
        expect(page.locator("#activityName")).to_be_visible(timeout=5_000)
        expect(page.locator("#app")).to_contain_text("Поступлений", timeout=5_000)
        assert page.locator("#pageTitle").inner_text() == "Активность"

        # `.stat .label` is uppercased by CSS, so compare case-insensitively.
        body = page.locator("#app").inner_text().casefold()
        for block in ("поступлений", "что и когда", "откуда приходило", "по дням"):
            if block not in body:
                problems.append(f"missing block: {block}")

        # The name box: an inflected Cyrillic spelling has to reach the account.
        # Иван can already be the selected account (the fixture's timestamps tie,
        # and the stable id tiebreaker puts usr_ivan first).  Waiting merely for
        # the word «Иван» therefore accepted the OLD render.  The asynchronous
        # change handler could clear it to «Загрузка…» one instruction later, so
        # a saturated full gate sometimes counted zero rows.  Hold the resulting
        # activity response briefly to make that intermediate state deterministic,
        # then wait for both requests and the notice produced by THIS search.
        activity_delayed = False

        def delay_first_ivan_activity(route) -> None:
            nonlocal activity_delayed
            if not activity_delayed:
                activity_delayed = True
                time.sleep(0.35)
            route.continue_()

        activity_route = "**/api/admin/users/usr_ivan/activity**"
        page.route(activity_route, delay_first_ivan_activity)
        with (
            page.expect_response(
                lambda response: "/api/admin/users/resolve?" in response.url,
                timeout=5_000,
            ) as resolved,
            page.expect_response(
                lambda response: "/api/admin/users/usr_ivan/activity?" in response.url,
                timeout=5_000,
            ) as activity_loaded,
            page.expect_request(
                lambda request: "/api/admin/users/usr_ivan/activity?" in request.url,
                timeout=5_000,
            ) as activity_started,
        ):
            page.fill("#activityName", "Ивану")
            page.dispatch_event("#activityName", "change")
            assert activity_started.value.method == "GET"
        assert resolved.value.ok, f"name resolution returned HTTP {resolved.value.status}"
        assert activity_loaded.value.ok, (
            f"resolved account activity returned HTTP {activity_loaded.value.status}"
        )
        assert activity_delayed, "the deterministic search-response delay did not apply"
        page.unroute(activity_route, delay_first_ivan_activity)
        expect(page.locator("#app h2").first).to_have_text("Активность: Иван", timeout=5_000)
        expect(page.locator("#app .notice", has_text="Найден:")).to_contain_text(
            "Найден: Иван", timeout=5_000
        )
        body = page.locator("#app").inner_text()
        if "Иван" not in body:
            problems.append("«Ивану» did not resolve to the account")
        if "ЧУЖАЯЗАМЕТКА" in body:
            problems.append("another account's row appeared in this account's activity")

        rows = page.locator("#app table tbody tr")
        expect(rows).to_have_count(7, timeout=5_000)

        show = page.locator("#app table tbody tr button", has_text="Показать").first
        show.click()
        page.wait_for_timeout(400)
        if not page.locator("#modal[open]").count():
            problems.append("the preview dialog did not open")
        else:
            if "символов" not in page.locator("#modalBody").inner_text():
                problems.append("the preview dialog carried no material")
            page.locator("#modal .dialog-head button").click()

        page.locator("#app button", has_text="7 дней").first.click()
        expect(page.locator("#app")).to_contain_text("Поступлений", timeout=5_000)
        if "поступлений" not in page.locator("#app").inner_text().casefold():
            problems.append("the period filter broke the screen")

        errors = [line for line in console if line.startswith(("error", "pageerror"))]
        if errors:
            problems.append(f"console errors: {errors[:3]}")
        browser.close()

    assert not problems, "\n".join(problems)


SLOW_AUDIT_FETCH = """
window.__origFetch = window.fetch;
window.fetch = (input, init) => {
  const url = String((input && input.url) || input || '');
  if (url.includes('/api/admin/audit')) {
    return new Promise(resolve => setTimeout(() => resolve(window.__origFetch(input, init)), 2000));
  }
  return window.__origFetch(input, init);
};
"""


def test_a_slow_section_cannot_paint_over_the_one_you_switched_to(live_admin):
    """Navigation does not cancel a request that is already in flight.

    Every renderer writes to `#app` AFTER its await, so a slow section landed on top
    of whatever the user had moved to: the highlighted menu entry and the heading said
    one thing and the table showed another. Reproduced deterministically by making one
    endpoint answer two seconds late — the fix is a generation counter, so this test
    fails the moment that check is removed.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{live_admin}/admin/", wait_until="networkidle")
        page.evaluate("t => sessionStorage.setItem('jericho_api_token', t)", TOKEN)
        page.reload(wait_until="networkidle")
        page.evaluate(SLOW_AUDIT_FETCH)

        page.locator("#nav button", has_text="Аудит").click()
        page.wait_for_timeout(150)
        page.locator("#nav button", has_text="Обзор").click()
        page.wait_for_timeout(3000)  # the audit answer lands inside this window

        heading = page.locator("#pageTitle").inner_text()
        body = page.locator("#app").inner_text()
        browser.close()

    assert heading == "Обзор"
    assert "Состояние хранилища" in body, "the section the user chose is not the one on screen"
    assert "Действие" not in body, "the abandoned section painted over the current one"


def test_a_long_list_pages_instead_of_pretending_to_be_complete(live_admin):
    """A page used to present itself as the whole set.

    The response carries `count = len(items)`, which on a full page equals the limit —
    indistinguishable from «that is all there is». The audit log is the fastest-growing
    list here, so it hit that first; its route now returns a real `total`, and the
    screen pages over it.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{live_admin}/admin/", wait_until="networkidle")
        page.evaluate("t => sessionStorage.setItem('jericho_api_token', t)", TOKEN)
        page.reload(wait_until="networkidle")

        # Enough audit rows to need a second page. Reading the audit log is itself
        # audited, so simply asking for it repeatedly fills it.
        for _ in range(130):
            page.evaluate(
                "t => fetch('/api/admin/audit?limit=1', {headers:{Authorization:'Bearer '+t}})", TOKEN
            )
        page.wait_for_timeout(1500)

        page.locator("#nav button", has_text="Аудит").click()
        page.wait_for_timeout(1200)
        body = page.locator("#app").inner_text()
        rows_first = page.locator("#app table tbody tr").count()

        back = page.locator("#app button", has_text="← Назад").first
        forward = page.locator("#app button", has_text="Вперёд →").first
        first_page_state = (back.is_disabled(), forward.is_disabled())

        forward.click()
        page.wait_for_timeout(1200)
        second_body = page.locator("#app").inner_text()
        second_back_disabled = page.locator("#app button", has_text="← Назад").first.is_disabled()
        browser.close()

    assert rows_first > 0, "the audit list rendered nothing"
    assert " из " in body, f"the pager does not say what the page is a page of: {body[:200]}"
    assert first_page_state == (True, False), (
        f"on the first page «Назад» must be off and «Вперёд» on, got {first_page_state}"
    )
    assert second_body != body, "«Вперёд» did not change the page"
    assert second_back_disabled is False, "«Назад» stayed disabled on the second page"


def test_an_abandoned_render_cannot_leave_its_data_behind_the_new_one(live_admin):
    """The first fix guarded the paint and not the state — and that is worse.

    Every renderer assigned `state.* = data.items` right after its await, BEFORE the
    generation check. So an abandoned render left the NEW section's rows on screen with
    the OLD section's data behind them, and the row buttons addressed that array BY
    POSITION. Found adversarially and reproduced in Chromium: the Активность screen
    showed one account's rows while «Показать» opened another account's material —
    a cross-account disclosure on the one screen that reads across accounts.

    Both halves are pinned here: the generation now guards the state write, and the
    lookups go by identifier, so a disagreement says «не найдено» instead of handing
    over somebody else's record.
    """
    from playwright.sync_api import sync_playwright

    slow_anna = """
    window.__origFetch = window.fetch;
    window.fetch = (input, init) => {
      const url = String((input && input.url) || input || '');
      if (url.includes('usr_anna/activity')) {
        return new Promise(r => setTimeout(() => r(window.__origFetch(input, init)), 2500));
      }
      return window.__origFetch(input, init);
    };
    """

    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{live_admin}/admin/", wait_until="networkidle")
        page.evaluate("t => sessionStorage.setItem('jericho_api_token', t)", TOKEN)
        page.reload(wait_until="networkidle")

        page.locator("#nav button", has_text="Активность").click()
        page.wait_for_timeout(800)
        page.evaluate(slow_anna)

        # Ask for the slow account, then immediately for the fast one. Anna's answer
        # lands last and must be discarded whole — rows AND state.
        # `void`: without it evaluate awaits the promise, the first render finishes
        # before the second starts, and there is no race to observe at all.
        page.evaluate("void actions.activityPick('usr_anna')")
        page.wait_for_timeout(200)
        page.evaluate("void actions.activityPick('usr_ivan')")
        page.wait_for_timeout(3500)

        heading = page.locator("#app h2").first.inner_text()
        state_leaked = page.evaluate(
            "() => (state.activity||[]).some(r => String(r.preview||'').includes('ЧУЖАЯЗАМЕТКА'))"
        )
        show = page.locator("#app table tbody tr button", has_text="Показать").first
        modal_text = ""
        if show.count():
            show.click()
            page.wait_for_timeout(400)
            modal_text = page.locator("#modalBody").inner_text()
        browser.close()

    assert "Иван" in heading, f"the screen is not showing the account that was asked for: {heading}"
    assert state_leaked is False, "the abandoned render left another account's rows in state"
    assert "ЧУЖАЯЗАМЕТКА" not in modal_text, "the preview opened another account's material"


def test_the_knowledge_list_pages_over_a_real_total(live_admin):
    """The seven lists that used to say «список обрезан» now page over a real total.

    Knowledge is the one to drive in a browser: its total is the one that had a wrong
    counter sitting right next to the right one, and its filter chips are what make
    the difference visible.
    """
    import hashlib

    from playwright.sync_api import sync_playwright

    from friday.storage.models import KnowledgeObject, RawObject, new_id

    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        page.goto(f"{live_admin}/admin/", wait_until="networkidle")
        page.evaluate("t => sessionStorage.setItem('jericho_api_token', t)", TOKEN)
        page.reload(wait_until="networkidle")

        # More objects than one page holds, so «Вперёд» has somewhere to go.
        storage = page.evaluate("() => PAGE")  # the page size the client uses
        total = int(storage) + 7
        app_storage = None
        for index in range(total):
            content = f"Знание номер {index} про склад, смету и договор аренды"
            raw = RawObject(
                id=new_id("raw"),
                user_id="usr_ivan",
                source="test",
                source_ref=new_id("src"),
                raw_content=content,
                content_type="text",
                content_hash=hashlib.sha256(f"k{index}".encode()).hexdigest(),
            )
            app_storage = app_storage or _APP.state.storage
            app_storage.store_raw_object(raw)
            app_storage.store_knowledge_object(
                KnowledgeObject(
                    id=new_id("ko"),
                    user_id="usr_ivan",
                    raw_object_id=raw.id,
                    content=content,
                    content_type="text",
                    title=f"Знание {index}",
                )
            )

        page.evaluate("id => { state.userId = id }", "usr_ivan")
        page.locator("#nav button", has_text="Знания").click()
        page.wait_for_timeout(1200)

        body = page.locator("#app").inner_text()
        assert f" из {total}" in body.replace(" ", " "), f"the pager does not show a real total: {body[:300]}"

        first_rows = page.locator("#app table tbody tr").count()
        page.locator("#app button", has_text="Вперёд →").first.click()
        page.wait_for_timeout(1200)
        second = page.locator("#app").inner_text()
        browser.close()

    assert first_rows > 0
    assert second != body, "«Вперёд» did not move the page"
