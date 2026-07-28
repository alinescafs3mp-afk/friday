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

from jericho.storage.models import RawObject, new_id

playwright_api = pytest.importorskip("playwright.sync_api")

PORT = 8791
TOKEN = "T" * 48


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


def test_the_activity_screen_works_in_a_browser(live_admin):
    from playwright.sync_api import sync_playwright

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
        page.wait_for_timeout(900)
        assert page.locator("#pageTitle").inner_text() == "Активность"

        # `.stat .label` is uppercased by CSS, so compare case-insensitively.
        body = page.locator("#app").inner_text().casefold()
        for block in ("поступлений", "что и когда", "откуда приходило", "по дням"):
            if block not in body:
                problems.append(f"missing block: {block}")

        # The name box: an inflected Cyrillic spelling has to reach the account.
        page.fill("#activityName", "Ивану")
        page.dispatch_event("#activityName", "change")
        page.wait_for_timeout(900)
        body = page.locator("#app").inner_text()
        if "Иван" not in body:
            problems.append("«Ивану» did not resolve to the account")
        if "ЧУЖАЯЗАМЕТКА" in body:
            problems.append("another account's row appeared in this account's activity")

        rows = page.locator("#app table tbody tr")
        if rows.count() != 7:
            problems.append(f"expected the 7 seeded arrivals, rendered {rows.count()}")

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
        page.wait_for_timeout(800)
        if "поступлений" not in page.locator("#app").inner_text().casefold():
            problems.append("the period filter broke the screen")

        errors = [line for line in console if line.startswith(("error", "pageerror"))]
        if errors:
            problems.append(f"console errors: {errors[:3]}")
        browser.close()

    assert not problems, "\n".join(problems)
