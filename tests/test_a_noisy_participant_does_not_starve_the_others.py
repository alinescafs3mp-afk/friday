"""Личный лимит обращений считается по человеку, а не по общему архиву.

Разбор Codex (`sol/HARDENING_FOR_OPUS.md`, §12.4). Ключи `telegram:user:{…}` и
`api:user:{…}` строились по `actor.user_id`, а в общем архиве это арендатор, один
на всех. Один шумный участник выбирал бюджет ОСТАЛЬНЫХ, включая владельца, и те
получали 429 за чужую активность.

Четвёртый случай одного семейства, и путь к нему уже был известен:

    заявка на подтверждение была видна и решаема любым участником (§12.2);
    указание «отвечай мне кратко» ложилось в общую учётку;
    личный запрет не действовал — переопределения читались по арендатору (§12.1);
    личный лимит был общим — здесь.

Глобальный телеграм-лимит остаётся глобальным и это не оплошность: он защищает
не человека, а сам мост от шквала со стороны Telegram. Личный обязан считать
принципала. Разница ровно в этом, и обе стороны проверяются.

Отказ здесь дороже, чем кажется: 429 приходит человеку как «слишком часто
пишете», хотя часто писал не он. Понять, что бюджет съел сосед, по такому ответу
невозможно.
"""

from __future__ import annotations

import asyncio

import pytest

from friday.permissions import ActorContext


def _person(name: str, *, source: str = "api-token") -> ActorContext:
    """Человек в ОБЩЕМ архиве: арендатор один, различает только `person_id`."""
    return ActorContext(
        user_id="tenant",
        preset_key="user",
        source=source,
        shared_tenant=True,
        person_id=name,
    )


async def _spend(limiter, actor, settings, times: int) -> list[bool]:
    from friday.server import _enforce_rate_limit

    class _Request:
        def __init__(self) -> None:
            self.app = type("app", (), {"state": type("state", (), {})()})()
            self.app.state.settings = settings
            self.app.state.rate_limiter = limiter

    results: list[bool] = []
    for _ in range(times):
        try:
            await _enforce_rate_limit(_Request(), actor)
            results.append(True)
        except Exception:  # noqa: BLE001 — 429 приходит исключением
            results.append(False)
    return results


@pytest.fixture
def tight(settings):
    """Настройки с крошечным личным лимитом — иначе проверка стоила бы минуту."""
    import dataclasses

    return dataclasses.replace(
        settings,
        api_user_rate_limit_per_minute=2,
        telegram_user_rate_limit_per_minute=2,
        telegram_global_rate_limit_per_minute=1000,
    )


def test_one_participant_does_not_spend_anothers_budget(tight) -> None:
    """Мутация: вернуть ключ по `actor.user_id` — сосед снова получает 429 за чужое."""
    from friday.server import SlidingWindowLimiter

    limiter = SlidingWindowLimiter()
    noisy = _person("person-a")
    quiet = _person("person-b")

    asyncio.run(_spend(limiter, noisy, tight, 5))
    theirs = asyncio.run(_spend(limiter, quiet, tight, 2))

    assert all(theirs), "сосед получил отказ за чужую активность"


def test_the_noisy_one_is_still_limited(tight) -> None:
    """Обратная сторона: правка не имеет права снять лимит вовсе."""
    from friday.server import SlidingWindowLimiter

    limiter = SlidingWindowLimiter()
    noisy = _person("person-a")

    spent = asyncio.run(_spend(limiter, noisy, tight, 5))

    assert spent[:2] == [True, True] and not any(spent[2:]), f"лимит перестал держать: {spent}"


def test_the_telegram_global_limit_stays_global(tight) -> None:
    """Глобальный лимит защищает МОСТ, а не человека, и общим быть обязан.

    Если развести и его по людям, десять участников дадут десятикратный шквал в
    сторону Telegram — ровно то, от чего он поставлен.
    """
    import dataclasses

    from friday.server import SlidingWindowLimiter

    strict = dataclasses.replace(
        tight, telegram_global_rate_limit_per_minute=3, telegram_user_rate_limit_per_minute=100
    )
    limiter = SlidingWindowLimiter()
    first = _person("person-a", source="telegram-bridge")
    second = _person("person-b", source="telegram-bridge")

    asyncio.run(_spend(limiter, first, strict, 3))
    theirs = asyncio.run(_spend(limiter, second, strict, 1))

    assert theirs == [False], "глобальный лимит развели по людям — мост остался без защиты"


def test_without_a_shared_archive_nothing_changes(tight) -> None:
    """Установка с одним пользователем: человек и арендатор совпадают."""
    from friday.server import SlidingWindowLimiter

    limiter = SlidingWindowLimiter()
    solo = ActorContext(user_id="solo", preset_key="owner", source="api-token")

    spent = asyncio.run(_spend(limiter, solo, tight, 4))

    assert spent[:2] == [True, True] and not any(spent[2:])
