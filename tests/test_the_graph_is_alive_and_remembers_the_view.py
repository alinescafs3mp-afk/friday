"""Картина графа живёт, помнит камеру и закрепляет ровно то, что человек двигал.

Проверяется в НАСТОЯЩЕМ браузере: и живой цикл кадров, и камера, и закрепление
существуют только в обработчиках. Тест, читающий исходник, покраснел бы от
комментария и позеленел бы от сломанного обработчика.

Три дефекта, которые эти пробы закрывают, найдены чтением кода и подтверждены:

1. картинка была неподвижной — `requestAnimationFrame` не встречался в `app.js`
   ни разу, раскладка считалась одним синхронным прогоном на 260 шагов;
2. камера была локальной переменной внутри `bindGraph`, а `bindGraph` зовётся
   заново после КАЖДОГО нажатия фильтра: человек приближал интересный куст,
   включал «только люди» и возвращался на общий план;
3. `saveLayout` писал координаты ВСЕХ узлов вида, поэтому при следующем открытии
   все получали `fixed`, ранний возврат пропускал укладку целиком, и одно
   перетаскивание молча замораживало картину навсегда.

Пропускается, если Playwright или его браузер не установлены.
"""

from __future__ import annotations

import itertools
import os
import threading
import time

import pytest

from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

pytest.importorskip("playwright.sync_api")

# Свой порт каждой пробе, и своя полоса портов каждому работнику pytest-xdist:
# счётчик живёт в процессе, поэтому у двух работников он начинался бы с одного
# числа, и проба падала бы не на дефекте, а на «the admin server did not start».
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_BAND = 8801 + 10 * (int("".join(filter(str.isdigit, _WORKER)) or 0) % 20)
_PORTS = itertools.count(_BAND)
TOKEN = "H" * 48
USER = "usr_ivan"


def _seed(storage) -> None:
    """Картина отбирает узлы по числу связанных документов, поэтому одних сущностей
    мало: нужны документы и принятые ссылки, из которых рождается встречаемость."""
    storage.ensure_user(USER, source="test", display_name="Иван", preset_key="user")
    entities = []
    for index in range(12):
        entity = Entity(
            id=new_id("ent"),
            user_id=USER,
            name=f"Сущность {index:02d}",
            entity_type=EntityType.PERSON if index % 2 else EntityType.ORGANIZATION,
        )
        storage.create_entity(entity)
        entities.append(entity)

    for document_index in range(4):
        raw = RawObject(new_id("raw"), USER, "test", new_id("ref"), f"Документ {document_index}", "text")
        storage.store_raw_object(raw)
        document = KnowledgeObject(
            new_id("ko"),
            USER,
            raw.id,
            content=f"Документ {document_index}",
            title=f"Документ {document_index}",
        )
        storage.store_knowledge_object(document)
        # Окна перекрываются, иначе картина распалась бы на несвязанные группы.
        window = entities[document_index * 3 : document_index * 3 + 4]
        for entity in window:
            storage.link_knowledge_entity(USER, document.id, entity.id, status="accepted")
    storage.commit()


@pytest.fixture
def live_admin(settings):
    from dataclasses import replace

    import uvicorn

    from friday.server import create_app

    port = next(_PORTS)
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
    _seed(app.state.storage)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _open_graph(play, base: str):
    try:
        browser = play.chromium.launch()
    except Exception as exc:  # noqa: BLE001 — отсутствующий браузер это не провал
        pytest.skip(f"no chromium available: {exc}")
    # Окно намеренно высокое: при умолчании 1280x720 полотно графа оказывается
    # НИЖЕ видимой части, и `page.mouse` бьёт мимо — ни одного pointer-события до
    # обработчиков не доходит.
    page = browser.new_page(viewport={"width": 1600, "height": 1200})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.goto(f"{base}/admin/", wait_until="networkidle")
    page.evaluate("token => sessionStorage.setItem('jericho_api_token', token)", TOKEN)
    page.reload(wait_until="networkidle")
    page.select_option("#userSelect", USER)
    page.wait_for_timeout(600)
    page.locator("#nav button", has_text="Граф").click()
    page.wait_for_timeout(1400)
    return browser, page, errors


def _settle(page) -> None:
    """Дождаться, пока картина уляжется и симуляция уснёт.

    Это не удобство, а следствие живой раскладки: узел уезжает из-под курсора
    между замером его места и нажатием, и проба падала бы на собственной
    торопливости, а не на дефекте."""
    page.wait_for_function("() => !state.graphFrame", timeout=15000)


def test_the_picture_settles_on_its_own_instead_of_standing_still(live_admin):
    """Мутация: не звать `wake()` в `bindGraph` — координаты не изменятся, краснеет.

    Проверяются НАРИСОВАННЫЕ координаты, а не внутренние: симуляция, считающая в
    стороне от экрана, ничем не лучше неподвижной картинки."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page, errors = _open_graph(play, live_admin)

        first = page.evaluate(
            "() => [...document.querySelectorAll('#graphSvg .gnode circle')]"
            ".map(c => c.getAttribute('cx') + ',' + c.getAttribute('cy')).join('|')"
        )
        page.wait_for_timeout(900)
        second = page.evaluate(
            "() => [...document.querySelectorAll('#graphSvg .gnode circle')]"
            ".map(c => c.getAttribute('cx') + ',' + c.getAttribute('cy')).join('|')"
        )

        assert first, "на картине нет ни одного узла — проба проверяет не то"
        assert first != second, "картина не двинулась ни на пиксель: цикл кадров не запущен"

        # И засыпает: жечь кадры на устоявшемся графе значит греть ноутбук зря.
        # Остывание занимает ровно 260 КАДРОВ, а не фиксированное настенное
        # время: под параллельными браузерами три секунды могут вместить меньше
        # кадров. Ждём авторитетный признак остановленного цикла.
        _settle(page)
        settled = page.evaluate(
            "() => ({active: Boolean(state.graphFrame), "
            "frame: state.graphSim ? state.graphSim.frame : -1, "
            "coolingFrames: FridayGraphLayout.COOLING_FRAMES})"
        )
        assert not settled["active"], "цикл кадров не уснул"
        assert settled["frame"] >= settled["coolingFrames"], f"цикл уснул раньше полного остывания: {settled}"
        third = page.evaluate(
            "() => [...document.querySelectorAll('#graphSvg .gnode circle')]"
            ".map(c => c.getAttribute('cx') + ',' + c.getAttribute('cy')).join('|')"
        )
        page.wait_for_timeout(1200)
        fourth = page.evaluate(
            "() => [...document.querySelectorAll('#graphSvg .gnode circle')]"
            ".map(c => c.getAttribute('cx') + ',' + c.getAttribute('cy')).join('|')"
        )
        assert third == fourth, "устоявшаяся картина продолжает жечь кадры"
        assert not errors, errors
        browser.close()


def test_the_camera_survives_a_filter_click(live_admin):
    """Мутация: вернуть `let view={x:0,y:0,k:1}` внутрь `bindGraph` — краснеет."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page, errors = _open_graph(play, live_admin)

        # Приближаемся к интересному месту.
        page.evaluate("() => { state.graphCamera = {x: 220, y: 130, k: 2.5}; }")
        page.evaluate("() => bindGraph()")
        page.wait_for_timeout(300)
        before = page.get_attribute("#graphSvg", "viewBox")

        # Проверять надо ПРИБЛИЖЕНИЕ, а не совпадение двух строк: при сброшенной
        # камере обе стороны одинаково равны умолчанию, и сравнение проходит,
        # ничего не проверив. Первая редакция этой пробы именно так и пережила
        # мутацию «камера снова локальная переменная».
        default = f"0 0 {1200} {700}"
        assert before != default, (
            f"камера не приблизилась: viewBox остался умолчанием {before} — "
            "проба сравнила бы умолчание с умолчанием"
        )
        assert before == "220 130 480 280", f"неожиданный viewBox после приближения: {before}"

        card = page.locator("#app section.card", has=page.locator("h2", has_text="Картина графа"))
        card.locator("button", has_text="person").first.click()
        page.wait_for_timeout(1200)

        after = page.get_attribute("#graphSvg", "viewBox")
        camera = page.evaluate("() => state.graphCamera")

        assert after == before, f"нажатие фильтра сбросило камеру: {before} -> {after}"
        assert abs(camera["k"] - 2.5) < 0.01, "масштаб не пережил перерисовку"
        assert not errors, errors
        browser.close()


def test_only_the_dragged_node_is_pinned(live_admin):
    """Мутация: вернуть `saveLayout(state.graphNodes||[])` в `pointerup` — в хранилище
    окажутся все узлы вида, и проба краснеет.

    Это ровно тот дефект, из-за которого одно перетаскивание замораживало картину:
    при следующем открытии закреплёнными оказывались ВСЕ."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page, errors = _open_graph(play, live_admin)

        total = page.locator("#graphSvg .gnode").count()
        assert total >= 4, "на картине слишком мало узлов для этой пробы"

        _settle(page)
        # Берётся именно КРУЖОК, а не группа: в группу входит подпись, поэтому
        # центр её рамки попадает в пустоту между именем и узлом, а у подписи
        # `pointer-events:none` — нажатие ушло бы в фон и стало бы сдвигом вида.
        node = page.locator("#graphSvg .gnode circle").first
        node.scroll_into_view_if_needed()
        _settle(page)
        box = node.bounding_box()
        assert box, "узел не виден на экране"
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + 140, box["y"] + 90, steps=8)
        page.mouse.up()
        page.wait_for_timeout(500)

        pins = page.evaluate("() => JSON.parse(localStorage.getItem(graphLayoutKey()) || '{}')")
        assert len(pins) == 1, (
            f"закреплено {len(pins)} узлов из {total} — перетаскивание одного заморозило весь вид"
        )
        assert not errors, errors
        browser.close()
