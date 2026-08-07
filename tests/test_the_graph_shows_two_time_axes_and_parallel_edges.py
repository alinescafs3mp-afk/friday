"""Две оси времени у картины графа и разведённые кратные рёбра.

Проверяется в НАСТОЯЩЕМ браузере: обе вещи существуют только в обработчиках и в
рисовании, и проба, читающая исходник, покраснела бы от комментария и позеленела
бы от сломанного органа управления.

**`known_at` — вторая ось времени.** Сервер принимал её с самого начала
(`as_of` — когда связь была ВЕРНА, `known_at` — когда мы о ней УЗНАЛИ), а вывести
её было нечем: в интерфейсе стоял только `as_of`. Две оси намеренно не подменяют
друг друга — связь, существовавшая в 2019-м, но записанная вчера, при вопросе
«что мы знали к 2020-му» появиться не должна.

**Кратные рёбра.** Две сущности могут быть связаны несколькими способами
(«руководит» и «состоит в»). Линии рисовались ровно друг на друге: человек видел
ОДНУ связь вместо двух, и вид связи у верхней. Разводятся перпендикулярно
направлению ребра — прямые остаются прямыми, потому что дугу пришлось бы считать
и на полотне, и в попадании мышью.

Пропускается, если Playwright или его браузер не установлены.
"""

from __future__ import annotations

import itertools
import os
import threading
import time

import pytest

from friday.storage.models import (
    Entity,
    EntityType,
    KnowledgeObject,
    RawObject,
    Relation,
    RelationType,
    new_id,
)

pytest.importorskip("playwright.sync_api")

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_BAND = 9401 + 10 * (int("".join(filter(str.isdigit, _WORKER)) or 0) % 20)
_PORTS = itertools.count(_BAND)
TOKEN = "H" * 48
USER = "usr_ivan"


def _seed(storage) -> None:
    """Пара сущностей, связанных ДВАЖДЫ, плюс документы — картина отбирает по ним."""
    storage.ensure_user(USER, source="test", display_name="Иван", preset_key="user")
    left = Entity(id=new_id("ent"), user_id=USER, name="Иванов", entity_type=EntityType.PERSON)
    right = Entity(id=new_id("ent"), user_id=USER, name="Отдел", entity_type=EntityType.ORGANIZATION)
    storage.create_entity(left)
    storage.create_entity(right)
    # Одна связь в одну сторону, вторая во ВСТРЕЧНУЮ — и это не прихоть фикстуры.
    # Перпендикуляр считается от направления ребра, поэтому у встречной пары он
    # смотрит в другую сторону, и одинаковое смещение уводит обе линии на одно
    # место. Пара в одну сторону такую ошибку не ловит: первая редакция этой
    # правки прошла бы её и сломалась бы на настоящих данных.
    for kind, forward in ((RelationType.MEMBER_OF, True), (RelationType.MANAGES, False)):
        storage.create_relation(
            Relation(
                id=new_id("rel"),
                user_id=USER,
                source_entity_id=left.id if forward else right.id,
                target_entity_id=right.id if forward else left.id,
                relation_type=kind,
            )
        )
    raw = RawObject(new_id("raw"), USER, "test", new_id("ref"), "Приказ", "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), USER, raw.id, content="Приказ", title="Приказ")
    storage.store_knowledge_object(document)
    for entity in (left, right):
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


def test_the_second_time_axis_has_a_control_that_reaches_the_server(live_admin):
    """Мутация: не класть `known_at` в запрос — краснеет.

    Проверяется НАСТОЯЩИЙ сетевой запрос, а не состояние экрана: орган, который
    меняет переменную и не доходит до сервера, — это обещание без механизма.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page, errors = _open_graph(play, live_admin)
        try:
            requests: list[str] = []
            page.on("request", lambda request: requests.append(request.url))
            page.fill("#graphKnownAt", "2026-08-01")
            page.locator("button", has_text="Показать").last.click()
            page.wait_for_timeout(1500)
            asked = [url for url in requests if "known_at=" in url]
            assert asked, f"органом подвигали, а до сервера ничего не доехало: {requests[-4:]}"
            assert "2026-08-01" in asked[-1], asked[-1]
            # Человеку сказано, что это ДРУГАЯ ось, а не та же дата иначе.
            body = page.inner_text("body")
            assert "ЗНАЛА" in body or "знала" in body, "вторая ось не объяснена"
        finally:
            browser.close()
        assert not errors, errors


def test_two_relations_between_the_same_pair_are_two_lines(live_admin):
    """Мутация: убрать смещение — линии совпадут, краснеет.

    Проверяются НАРИСОВАННЫЕ координаты: две связи между одной парой должны быть
    видимы как две, иначе вид связи у верхней подменяет собой обе.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser, page, errors = _open_graph(play, live_admin)
        try:
            page.wait_for_function("() => !state.graphFrame", timeout=15000)
            segments = page.evaluate(
                """() => Array.from(document.querySelectorAll('#graphSvg line[data-edge]'))
                    .map(line => ({
                        a: line.getAttribute('data-a'),
                        b: line.getAttribute('data-b'),
                        x1: Number(line.getAttribute('x1')), y1: Number(line.getAttribute('y1')),
                        x2: Number(line.getAttribute('x2')), y2: Number(line.getAttribute('y2')),
                    }))"""
            )
            pairs: dict[tuple[str, str], list[dict]] = {}
            for item in segments:
                key = tuple(sorted((str(item["a"]), str(item["b"]))))
                pairs.setdefault(key, []).append(item)
            multi = [items for items in pairs.values() if len(items) > 1]
            assert multi, f"в картине нет пары с несколькими связями: {list(pairs.values())[:3]}"
            for items in multi:
                first, second = items[0], items[1]
                # Сравниваются СЕРЕДИНЫ отрезков, а не концы: у встречных рёбер
                # (`a→b` и `b→a`) концы переставлены местами, и разница по концам
                # получалась большой даже когда линии лежат ровно друг на друге.
                # Первая редакция этой пробы именно так и прошла мутацию.
                middle = lambda item: (  # noqa: E731 — местная мера, не функция модуля
                    (item["x1"] + item["x2"]) / 2,
                    (item["y1"] + item["y2"]) / 2,
                )
                left, right = middle(first), middle(second)
                gap = abs(left[0] - right[0]) + abs(left[1] - right[1])
                assert gap > 1.0, f"кратные рёбра лежат друг на друге: {first} vs {second}"
        finally:
            browser.close()
        assert not errors, errors
