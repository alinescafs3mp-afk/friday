"""Карточка объекта — тяжёлое чтение, и она не имеет права морозить остальных.

Найдено состязательным ревью спеки v3: `GET /api/kg/entity-profile` звал
`kg.entity_profile` прямо в `async def`, тогда как СОСЕДНИЕ маршруты того же
файла (`/kg/resolutions/pending`, `/kg/conflicts`, `/kg/merges`) давно ходят
через `run_blocking`. На широкой сущности профиль — это несколько SQL по 22 043
связям знание↔сущность, и на время их выполнения замирает не «этот запрос», а
единственный event loop, то есть все остальные пользователи целиком. Ровно та же
болезнь, что уже чинили у поиска (см. `test_search_does_not_block_other_tenants`),
и лечится тем же способом.

Синхронизационный тест, а не проверка «в коде есть слово run_blocking»:
синтаксическое наличие вызова ничего не доказывает, а вот способность event loop
отпустить ожидающий синхронный профиль во время запроса — доказывает.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Entity, EntityType, new_id


@pytest.mark.asyncio
async def test_entity_profile_route_does_not_block_the_event_loop(settings, monkeypatch):
    """Event loop обязан сделать шаг, ПОКА синхронный профиль ждёт в другом потоке.

    Мутация: вернуть в `entity_profile_by_name` прямой вызов
    `kg.entity_profile(...)` вместо `await run_blocking(...)` — тест обязан
    покраснеть: callback не сможет освободить профиль до защитного таймаута.

    Handshake проверяет именно прогресс loop, а не wall-clock задержку процесса:
    на перегруженном CI отдельный pytest worker может не получать CPU заметно
    дольше обычного, хотя его event loop ничем не заблокирован.
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
        loop = asyncio.get_running_loop()
        loop_progress = asyncio.Event()
        release_profile = threading.Event()
        handshake_succeeded: list[bool] = []

        def _slow_profile(*args, **kwargs):
            # Если профиль исполняется через run_blocking, loop свободен: он
            # обработает callback, соседняя coroutine отпустит этот поток. При
            # прямом вызове callback останется в очереди до истечения таймаута.
            loop.call_soon_threadsafe(loop_progress.set)
            handshake_succeeded.append(release_profile.wait(2.0))
            return real_profile(*args, **kwargs)

        monkeypatch.setattr(kg, "entity_profile", _slow_profile)

        async def _release_after_loop_progress() -> None:
            await loop_progress.wait()
            release_profile.set()

        release_task = asyncio.create_task(_release_after_loop_progress())
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
            release_profile.set()
            loop_progress.set()
            await release_task

        assert handshake_succeeded == [True], (
            "event loop не смог отпустить синхронный профиль, пока тот выполнялся — "
            "карточка объекта морозила остальных пользователей"
        )
