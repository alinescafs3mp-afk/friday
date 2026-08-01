"""Карточка объекта — тяжёлое чтение, и она не имеет права морозить остальных.

Найдено состязательным ревью спеки v3: `GET /api/kg/entity-profile` звал
`kg.entity_profile` прямо в `async def`, тогда как СОСЕДНИЕ маршруты того же
файла (`/kg/resolutions/pending`, `/kg/conflicts`, `/kg/merges`) давно ходят
через `run_blocking`. На широкой сущности профиль — это несколько SQL по 22 043
связям знание↔сущность, и на время их выполнения замирает не «этот запрос», а
единственный event loop, то есть все остальные пользователи целиком. Ровно та же
болезнь, что уже чинили у поиска (см. `test_search_does_not_block_other_tenants`),
и лечится тем же способом.

Тик-тест, а не проверка «в коде есть слово run_blocking»: синтаксическое наличие
вызова ничего не доказывает, а вот непрерывность тиков соседней корутины —
доказывает.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Entity, EntityType, new_id


@pytest.mark.asyncio
async def test_entity_profile_route_does_not_block_the_event_loop(settings, monkeypatch):
    """Соседняя корутина обязана продолжать тикать, ПОКА идёт медленный профиль.

    Мутация: вернуть в `entity_profile_by_name` прямой вызов
    `kg.entity_profile(...)` вместо `await run_blocking(...)` — тест обязан
    покраснеть (один разрыв ~0.3 с вместо ровных 0.01 с).

    Меряется максимальный разрыв между тиками, а не их сумма: маршрут делает ДВА
    отложенных вызова (`find_entity` и `entity_profile`), и сумма позволила бы
    одному заблокировать, пока второй «догоняет».
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        storage = app.state.storage
        kg = app.state.kg
        entity = Entity(
            id=new_id("ent"),
            user_id=LEGACY_OWNER_USER_ID,
            name="Атлас",
            entity_type=EntityType.PROJECT,
        )
        storage.create_entity(entity)

        real_profile = kg.entity_profile

        def _slow_profile(*args, **kwargs):
            time.sleep(0.3)
            return real_profile(*args, **kwargs)

        monkeypatch.setattr(kg, "entity_profile", _slow_profile)

        tick_times: list[float] = []

        async def _ticker() -> None:
            while True:
                tick_times.append(time.monotonic())
                await asyncio.sleep(0.01)

        ticker_task = asyncio.create_task(_ticker())
        # Тикер только ПОСТАВЛЕН в очередь; без явной уступки управления
        # блокировка в начале обработчика успела бы пройти до первого тика, и
        # мерить было бы нечего.
        await asyncio.sleep(0)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                response = await client.get(
                    "/api/kg/entity-profile",
                    params={"name": "Атлас"},
                    headers={"Authorization": f"Bearer {settings.api_token}"},
                )
                assert response.status_code == 200, response.text
        finally:
            # Записывается ДО отмены: блокировка, дотянувшая до самого возврата,
            # иначе осталась бы невидимой — отмена выигрывает у просроченного
            # таймера тикера, и хвост разрыва просто не попал бы в список.
            tick_times.append(time.monotonic())
            ticker_task.cancel()

        assert len(tick_times) >= 5, f"тиков всего {len(tick_times)} — стенд сломан"
        gaps = [second - first for first, second in zip(tick_times, tick_times[1:], strict=False)]
        max_gap = max(gaps)
        assert max_gap < 0.15, (
            f"наибольший разрыв между тиками {max_gap:.3f} с (ожидалось ~0.01) — "
            "event loop замирал, то есть карточка объекта морозила остальных пользователей"
        )
