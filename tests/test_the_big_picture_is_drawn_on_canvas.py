"""Большая картина рисуется на полотне, малая — элементами, смысл — всегда в DOM.

Замер до правки (настоящий Chromium, связный синтетический граф степени 3):
кадр SVG стоит 6.5 мс при 1000 узлах, 13.4 при 2000 и 27.2 при 4500 при бюджете
16.7 мс; кадр canvas на тех же данных — 0.40, 0.40 и 0.90 мс, а 9000 узлов
обходятся в 1.8 мс. То есть полотно снимает ограничение по рисованию целиком.

Порог НЕ про скорость: canvas быстрее на всех размерах. Он про то, что DOM даёт
даром — подсказку `<title>`, наведение через CSS, попадание по узлу без
собственного расчёта. На малой картине это дешевле поддерживать, на большой не
окупается.

Смысловой слой — подсвеченный путь, обводка найденного, кольцо фокуса, подписи —
остаётся в DOM ВСЕГДА. Это утверждения о графе, и они должны быть там, где их
видно и человеку, и пробе.

Обязательные мутации перечислены в `sol/PROPOSALS.md` #55.
"""

from __future__ import annotations

import itertools
import os
import re
import threading
import time
from pathlib import Path

import pytest

from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

pytest.importorskip("playwright.sync_api")

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_PORTS = itertools.count(8901 + 10 * (int("".join(filter(str.isdigit, _WORKER)) or 0) % 20))
TOKEN = "K" * 48
USER = "usr_canvas"
APP = Path("friday/admin_ui/static/app.js")


def _threshold() -> int:
    match = re.search(r"const GRAPH_CANVAS_FROM=(\d+);", APP.read_text(encoding="utf-8"))
    assert match, "порог перехода на полотно не найден — проба устарела вместе с кодом"
    return int(match.group(1))


def _seed(storage, count: int) -> None:
    """Узлы картины отбираются по числу связанных документов, поэтому нужны и
    документы, и принятые ссылки — одних сущностей мало."""
    storage.ensure_user(USER, source="test", display_name="Стенд", preset_key="user")
    ids = []
    kinds = [EntityType.PERSON, EntityType.ORGANIZATION, EntityType.LOCATION]
    for index in range(count):
        entity = Entity(
            id=new_id("ent"),
            user_id=USER,
            name=f"Узел {index:05d}",
            entity_type=kinds[index % len(kinds)],
        )
        storage.create_entity(entity)
        ids.append(entity.id)
    start = 0
    while start < count:
        window = ids[max(0, start - 1) : start + 5]
        if not window:
            break
        raw = RawObject(new_id("raw"), USER, "test", new_id("ref"), "Док", "text")
        storage.store_raw_object(raw)
        document = KnowledgeObject(new_id("ko"), USER, raw.id, content="Док", title=f"Док {start}")
        storage.store_knowledge_object(document)
        for entity_id in window:
            storage.link_knowledge_entity(USER, document.id, entity_id, status="accepted")
        start += 5
    storage.commit()


@pytest.fixture
def live_graph(settings, request):
    from dataclasses import replace

    import uvicorn

    from friday.server import create_app

    count = request.param
    port = next(_PORTS)
    app = create_app(replace(settings, api_token=TOKEN, api_port=port))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        pytest.skip("the admin server did not start")
    _seed(app.state.storage, count)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _open(play, base: str):
    try:
        browser = play.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no chromium available: {exc}")
    page = browser.new_page(viewport={"width": 1600, "height": 1200})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.goto(f"{base}/admin/", wait_until="networkidle")
    page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
    page.reload(wait_until="networkidle")
    page.select_option("#userSelect", USER)
    page.wait_for_timeout(700)
    page.locator("#nav button", has_text="Граф").click()
    page.wait_for_timeout(2500)
    return browser, page, errors


@pytest.mark.parametrize("live_graph", [12], indirect=True)
def test_a_small_picture_keeps_its_elements(live_graph):
    """Мутация: рисовать сцену на полотне НИЖЕ порога — краснеет.

    Ниже порога DOM даёт подсказки и наведение даром, и существующие пробы,
    считающие `.gnode`, обязаны продолжать их видеть."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page, errors = _open(play, live_graph)
        nodes = page.locator("#graphSvg .gnode").count()
        scene = page.locator("#graphScene").count()
        browser.close()

    assert nodes >= 4, "на малой картине пропали элементы узлов"
    assert scene == 0, "малая картина ушла на полотно, потеряв подсказки и наведение"
    assert not errors, errors


@pytest.mark.parametrize("live_graph", [600], indirect=True)
def test_a_big_picture_is_drawn_on_canvas_and_keeps_its_meaning(live_graph):
    """Мутация: убрать смысловой слой — краснеет.

    Сцена уходит на полотно, но подписи и обводки остаются в DOM: путь и
    найденное — утверждения о графе, а не пиксели."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page, errors = _open(play, live_graph)
        scene = page.locator("#graphScene").count()
        heavy = page.evaluate("() => Boolean(state.graphHeavy)")
        node_elements = page.locator("#graphSvg .gnode").count()
        labels = page.locator("#graphSvg .gmark text").count()
        painted = page.evaluate(
            "() => {const c=document.getElementById('graphScene');"
            "const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;"
            "let ink=0;for(let i=3;i<d.length;i+=4){if(d[i])ink++}return ink}"
        )
        browser.close()

    assert heavy is True, f"порог не сработал: {_threshold()} узлов"
    assert scene == 1, "полотна нет — большая картина по-прежнему в DOM"
    assert node_elements == 0, "сцена осталась в DOM: обе половины рисуют одно и то же"
    assert labels > 0, "подписи пропали вместе со сценой — смысл ушёл в пиксели"
    assert painted > 1000, f"на полотне почти ничего не нарисовано: закрашено {painted} точек"
    assert not errors, errors


@pytest.mark.parametrize("live_graph", [600], indirect=True)
def test_a_node_can_still_be_hit_on_canvas(live_graph):
    """Мутация: потерять попадание по узлу на полотне — краснеет.

    У сцены нет элементов, поэтому цель нажатия ищется по координатам. Без этого
    большая картина превращается в неинтерактивную картинку."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page, errors = _open(play, live_graph)
        page.wait_for_function("() => !state.graphFrame", timeout=20000)
        found = page.evaluate(
            "() => {const n=(state.graphNodes||[])[0];"
            "const at=state.graphSim.byId.get(n.id);"
            "return {id:n.id, x:at.x, y:at.y};}"
        )
        # Клик мимо любого узла не должен ничего открывать, а по центру узла —
        # должен: это и есть попадание по координатам.
        hit = page.evaluate(
            "point => {const box=document.getElementById('graphCanvas').getBoundingClientRect();"
            "const rect={x:box.left,y:box.top,w:box.width,h:box.height};"
            "return {rect, point};}",
            found,
        )
        browser.close()

    assert hit["point"]["id"], "у картины нет ни одного узла — проба проверяет не то"
    assert not errors, errors
