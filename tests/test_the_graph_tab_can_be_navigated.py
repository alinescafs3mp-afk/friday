"""Вкладка «Граф» как инструмент обхода, а не одна неподвижная картинка.

Проверяется в НАСТОЯЩЕМ браузере, потому что проверять здесь нечего иначе:
фильтр, переключение «весь граф ↔ окрестность узла», поиск и запоминание
раскладки живут в обработчиках, а не в разметке. Тест, читающий исходник
`app.js`, покраснел бы от комментария и позеленел бы от сломанного обработчика.

Пропускается, если Playwright или его браузер не установлены.
"""

from __future__ import annotations

import threading
import time

import pytest

from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, Relation, new_id

pytest.importorskip("playwright.sync_api")

PORT = 8799
TOKEN = "G" * 48


def _seed(storage) -> None:
    storage.ensure_user("usr_ivan", source="test", display_name="Иван", preset_key="user")
    made: dict[str, Entity] = {}
    for name, kind in (
        ("Кублик Александр Юрьевич", EntityType.PERSON),
        ("Варламова Ольга Васильевна", EntityType.PERSON),
        ("в/ч 30926", EntityType.ORGANIZATION),
        ("Волжский", EntityType.LOCATION),
    ):
        entity = Entity(id=new_id("ent"), user_id="usr_ivan", name=name, entity_type=kind)
        storage.create_entity(entity)
        made[name] = entity

    raw = RawObject(new_id("raw"), "usr_ivan", "test", new_id("ref"), "Рапорт", "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), "usr_ivan", raw.id, content="Рапорт", title="Рапорт")
    storage.store_knowledge_object(document)
    for entity in made.values():
        storage.link_knowledge_entity("usr_ivan", document.id, entity.id, status="accepted")

    storage.create_relation(
        Relation(
            id=new_id("rel"),
            user_id="usr_ivan",
            source_entity_id=made["Кублик Александр Юрьевич"].id,
            target_entity_id=made["Варламова Ольга Васильевна"].id,
            relation_type="family_of",
            weight=0.9,
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


def _open_graph(page, base: str):
    page.goto(f"{base}/admin/", wait_until="networkidle")
    page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
    page.reload(wait_until="networkidle")
    page.select_option("#userSelect", "usr_ivan")
    page.wait_for_timeout(600)
    page.locator("#nav button", has_text="Граф").click()
    page.wait_for_timeout(1200)


def test_the_graph_tab_filters_searches_and_focuses(live_admin):
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    with sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — отсутствующий браузер это не провал
            pytest.skip(f"no chromium available: {exc}")
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))

        _open_graph(page, live_admin)
        card = page.locator("#app section.card", has=page.locator("h2", has_text="Картина графа"))
        if card.count() != 1:
            pytest.fail("карточка «Картина графа» не найдена")
        if page.locator("#graphSvg .gnode").count() < 4:
            problems.append("на картине меньше узлов, чем заведено сущностей")

        # Фильтр по типу обязан уйти В ЗАПРОС: применённый к готовой сотне узлов,
        # он показал бы «людей столько, сколько их среди самых связанных».
        requests.clear()
        card.locator("button", has_text="person").first.click()
        page.wait_for_timeout(900)
        if not any("entity_types=person" in url for url in requests):
            problems.append("фильтр по типу не доехал до запроса")
        if page.locator("#graphSvg .gnode").count() != 2:
            problems.append("после фильтра «person» на картине не двое людей")
        card.locator("button", has_text="все").first.click()
        page.wait_for_timeout(900)

        # Поиск подсвечивает, а не прячет: вид остаётся тем же, найденное обведено.
        page.fill("#graphSearch", "Кублик")
        card.locator("button", has_text="Найти").click()
        page.wait_for_timeout(900)
        if "Найдено на картине" not in page.locator("#app").inner_text():
            problems.append("поиск не сказал, сколько нашёл")

        # Окрестность узла — главный способ обхода: от общей картины к одному
        # человеку. Переход живёт в карточке узла, а не на двойном клике: первый
        # клик уже открывает карточку, и она перехватывает второй.
        page.fill("#graphSearch", "")
        card.locator("button", has_text="Найти").click()
        page.wait_for_timeout(900)
        requests.clear()
        page.locator("#graphSvg .gnode").first.click()
        page.wait_for_timeout(900)
        page.locator("#modal button", has_text="Показать окрестность").click()
        page.wait_for_timeout(1200)
        if not any("/api/admin/graph/ent_" in url for url in requests):
            problems.append("двойной клик не запросил окрестность узла")
        text = page.locator("#app").inner_text()
        if "фокус:" not in text:
            problems.append("вид не показал, вокруг какого узла построен")
        if "глубина" not in text:
            problems.append("в локальном виде нет управления глубиной")

        # Раскладка запоминается: иначе каждое открытие экрана отменяет работу
        # человека, расставившего узлы.
        saved = page.evaluate(
            "() => Object.keys(localStorage).filter(k => k.startsWith('friday:graph:')).length"
        )
        page.locator("#app button", has_text="Сбросить раскладку").click()
        page.wait_for_timeout(600)
        after = page.evaluate(
            "() => Object.keys(localStorage).filter(k => k.startsWith('friday:graph:')).length"
        )
        if after > saved:
            problems.append("«Сбросить раскладку» не убрала сохранённое")

        browser.close()

    assert not errors, errors
    assert not problems, problems
