"""Поиск по графу обязан показывать не только ЧТО нашлось, но и КАК оно связано.

Подсветка узла отвечает на половину вопроса. Человек смотрит на окрестность
одного узла и ищет второй — ему нужен путь между ними, иначе кольцо на дальнем
кружке ничего не объясняет.

Путь считается по НАРИСОВАННЫМ рёбрам: подсветить путь через ребро, отсеянное
фильтром, значило бы показать связь, которой на этой картине нет.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

TOKEN = "P" * 48


@pytest.fixture
def live_graph(settings):
    """Цепочка из четырёх узлов: Иванов → часть → склад → Петров."""

    from dataclasses import replace

    import uvicorn

    from friday.server import create_app
    from friday.storage.models import (
        Entity,
        EntityType,
        KnowledgeObject,
        RawObject,
        Relation,
        RelationType,
        new_id,
    )

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
    listing = httpx.get(
        f"http://127.0.0.1:{port}/api/admin/users",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    owner_id = (listing.json().get("items") or [{}])[0].get("id")
    if not owner_id:
        pytest.skip("в базе нет ни одного пользователя")

    raw = RawObject(
        id=new_id("raw"),
        user_id=owner_id,
        source="test",
        source_ref=new_id("src"),
        raw_content="Приказ",
        content_type="text",
        content_hash="c" * 64,
    )
    storage.store_raw_object(raw)
    knowledge_id = new_id("ko")
    storage.store_knowledge_object(
        KnowledgeObject(
            id=knowledge_id,
            user_id=owner_id,
            raw_object_id=raw.id,
            content="Приказ",
            content_type="text",
            title="Приказ",
        )
    )
    made = {}
    for key, name, kind in (
        ("start", "Иванов Иван", EntityType.PERSON),
        ("middle", "войсковая часть 30926", EntityType.ORGANIZATION),
        ("bridge", "склад номер два", EntityType.LOCATION),
        ("goal", "Петров Пётр", EntityType.PERSON),
    ):
        entity = Entity(id=new_id("ent"), user_id=owner_id, name=name, entity_type=kind)
        storage.create_entity(entity)
        storage.link_knowledge_entity(
            user_id=owner_id,
            knowledge_object_id=knowledge_id,
            entity_id=entity.id,
            status="accepted",
        )
        made[key] = entity.id
    for source, target in (("start", "middle"), ("middle", "bridge"), ("bridge", "goal")):
        storage.create_relation(
            Relation(
                id=new_id("rel"),
                user_id=owner_id,
                source_entity_id=made[source],
                target_entity_id=made[target],
                relation_type=RelationType.MEMBER_OF,
                weight=0.9,
            )
        )
    try:
        yield {"base": f"http://127.0.0.1:{port}", "ids": made}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _open(play, base):
    try:
        browser = play.chromium.launch()
    except Exception as exc:  # noqa: BLE001 — отсутствующий браузер это не провал продукта
        pytest.skip(f"no chromium available: {exc}")
    page = browser.new_page()
    # Ключ кладётся ДО того, как страница поднимется со вкладкой графа: смена
    # одного лишь хэша документ не перезагружает, и вкладка успевала отрисоваться
    # без авторизации — экран показывал «Требуется авторизация», а не картину.
    page.goto(f"{base}/admin/#graph", wait_until="networkidle")
    page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1400)
    return browser, page


def test_the_path_from_the_focus_to_the_hit_is_lit(live_graph):
    """Мутация: вернуть `graphPaths` пустое множество — подсветки нет, тест краснеет."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page = _open(play, live_graph["base"])
        # Встаём в окрестность начального узла и ищем дальний.
        page.evaluate(
            "id => { state.graphView='local'; state.graphFocus=id; state.graphFocusName='Иванов Иван';"
            " state.graphDepth=4; graphState().search='Петров'; }",
            live_graph["ids"]["start"],
        )
        page.evaluate("() => refresh()")
        page.wait_for_timeout(1200)

        lit = page.locator("line.gpath").count()
        assert lit >= 1, "путь от фокуса до найденного узла не подсвечен"
        note = page.locator("#app .notice").all_inner_texts()
        assert any("Путь от" in text for text in note), "про путь не сказано ни слова"
        browser.close()


def test_the_overview_says_there_is_no_anchor_instead_of_staying_silent(live_graph):
    """Молчание читается как «пути нет». На общей картине точки отсчёта просто нет."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page = _open(play, live_graph["base"])
        page.evaluate("() => { state.graphView='global'; graphState().search='Петров'; }")
        page.evaluate("() => refresh()")
        page.wait_for_timeout(1200)

        assert page.locator("line.gpath").count() == 0
        texts = " ".join(page.locator("#app .notice").all_inner_texts())
        assert "окрестность узла" in texts, "вид промолчал о том, почему пути не показаны"
        browser.close()


def test_the_date_and_the_confidence_are_on_the_screen(live_graph):
    """Оба органа управления обязаны быть, иначе фильтр существует только в API."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page = _open(play, live_graph["base"])
        assert page.locator("#graphAsOf").count() == 1, "нет поля даты"
        body = page.locator("#app").inner_text()
        assert "уверенность связи не ниже" in body
        assert "картина на дату" in body
        browser.close()


def test_the_neighbourhood_hides_the_shared_documents_control(live_graph):
    """«Общих документов не меньше» в окрестности узла не значит ничего.

    Совместной встречаемости там нет вовсе — обход идёт по подтверждённым
    связям. Прежде орган показывался и молча делился на 50, превращаясь в порог
    уверенности: человек двигал одно, менялось другое.
    """

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page = _open(play, live_graph["base"])
        assert "общих документов не меньше" in page.locator("#app").inner_text()

        page.evaluate(
            "id => { state.graphView='local'; state.graphFocus=id; state.graphFocusName='Иванов Иван'; }",
            live_graph["ids"]["start"],
        )
        page.evaluate("() => refresh()")
        page.wait_for_timeout(1200)
        assert "общих документов не меньше" not in page.locator("#app").inner_text()
        browser.close()
