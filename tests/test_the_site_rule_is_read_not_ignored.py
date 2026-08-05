"""Правило хозяина сайта читается, а не игнорируется.

Пятница ходит на чужие страницы браузерным заголовком — то есть выглядит как
человек, будучи программой. Читать при этом `robots.txt` не значит «нельзя
ходить»: значит спросить, что хозяин сайта разрешил, прежде чем брать.

Три свойства, каждое проверено отдельно.

**Отказ называется.** Пустая страница без причины читается как «там ничего нет»,
и модель пересказывает это человеку как факт об интернете — тот же класс, что
отказ поисковика, выданный за пустую выдачу.

**Недоступные правила означают РАЗРЕШЕНО.** Обратное правило превращало бы любой
сбой сети в запрет на весь интернет.

**Названный сайтом темп главнее нашего умолчания.** `Crawl-delay: 3` — просьба
хозяина, а секунда по умолчанию — наша догадка.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from friday.web_surfer import WebSurfer


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Answer:
    def __init__(self, status: int) -> None:
        self.status_code = status


def _surfer(settings, robots: str | None, *, status: int = 200) -> WebSurfer:
    """Обозреватель, которому вместо сети отдают заданный `robots.txt`."""

    made = WebSurfer(replace(settings, web_host_pause_sec=0.0))
    asked: list[str] = []

    async def _request(url: str):
        asked.append(url)
        if robots is None:
            raise ConnectionError("сеть недоступна")
        return robots.encode("utf-8"), _Answer(status), url

    made._request_bytes = _request  # type: ignore[method-assign]  # noqa: SLF001
    made.asked = asked  # type: ignore[attr-defined]
    return made


@pytest.mark.anyio
async def test_a_forbidden_page_is_refused_with_a_reason(settings) -> None:
    """Мутация: вернуть пустую строку из `_robots_verdict` — тест краснеет."""

    surfer = _surfer(settings, "User-agent: *\nDisallow: /private/\n")

    verdict = await surfer._robots_verdict("https://site.example/private/page")  # noqa: SLF001

    assert verdict, "запрещённая страница признана разрешённой"
    assert "robots.txt" in verdict, "человеку не сказано, чьё это правило"


@pytest.mark.anyio
async def test_the_refusal_reaches_the_caller_as_a_result_not_a_crash(settings) -> None:
    surfer = _surfer(settings, "User-agent: *\nDisallow: /\n")

    result = await surfer.fetch("https://site.example/anything")

    assert result.text == ""
    assert result.error, "отказ пришёл молча — это читается как «страница пустая»"
    assert result.url == "https://site.example/anything"


@pytest.mark.anyio
async def test_what_is_allowed_is_not_blocked(settings) -> None:
    surfer = _surfer(settings, "User-agent: *\nDisallow: /private/\n")

    assert await surfer._robots_verdict("https://site.example/public/page") == ""  # noqa: SLF001


@pytest.mark.anyio
async def test_unreadable_rules_mean_allowed(settings) -> None:
    """Сбой сети не должен превращаться в запрет на весь интернет."""

    surfer = _surfer(settings, None)

    assert await surfer._robots_verdict("https://site.example/page") == ""  # noqa: SLF001


@pytest.mark.anyio
async def test_a_missing_robots_file_means_allowed(settings) -> None:
    """404 — это «правил нет», то есть разрешение, а не молчание."""

    surfer = _surfer(settings, "<html>не найдено</html>", status=404)

    assert await surfer._robots_verdict("https://site.example/page") == ""  # noqa: SLF001


@pytest.mark.anyio
async def test_the_rules_are_read_once_per_site(settings) -> None:
    """Спрашивать правила перед каждой страницей — удвоить нагрузку на сайт."""

    surfer = _surfer(settings, "User-agent: *\nDisallow: /private/\n")

    for index in range(5):
        await surfer._robots_verdict(f"https://site.example/page-{index}")  # noqa: SLF001

    assert len(surfer.asked) == 1, f"robots.txt запрошен {len(surfer.asked)} раз вместо одного"


@pytest.mark.anyio
async def test_two_sites_have_their_own_rules(settings) -> None:
    surfer = _surfer(settings, "User-agent: *\nDisallow: /private/\n")

    await surfer._robots_verdict("https://one.example/x")  # noqa: SLF001
    await surfer._robots_verdict("https://two.example/x")  # noqa: SLF001

    assert len(surfer.asked) == 2
    assert "one.example" in surfer.asked[0] and "two.example" in surfer.asked[1]


@pytest.mark.anyio
async def test_the_site_named_tempo_beats_our_default(settings) -> None:
    """`Crawl-delay` — просьба хозяина; наша секунда по умолчанию лишь догадка."""

    surfer = _surfer(settings, "User-agent: *\nCrawl-delay: 3\n")

    await surfer._robots_verdict("https://slow.example/page")  # noqa: SLF001

    assert surfer._pause_for("slow.example") >= 3.0  # noqa: SLF001


@pytest.mark.anyio
async def test_a_faster_crawl_delay_does_not_lower_our_floor(settings) -> None:
    """Разрешение сайта спешить не отменяет нашей собственной вежливости.

    Проверяется ДЕЙСТВУЮЩАЯ пауза, а не запись в словаре: словарь — внутреннее
    устройство, а ждёт `_be_polite_to` ровно то, что вернёт `_pause_for`.
    Заодно замечено на стандартном разборщике: дробную задержку он молча
    отбрасывает вовсе, до сравнения доходят только целые секунды.
    """

    surfer = WebSurfer(replace(settings, web_host_pause_sec=2.0))

    async def _request(url: str):
        return b"User-agent: *\nCrawl-delay: 1\n", _Answer(200), url

    surfer._request_bytes = _request  # type: ignore[method-assign]  # noqa: SLF001
    await surfer._robots_verdict("https://fast.example/page")  # noqa: SLF001

    assert surfer._pause_for("fast.example") == 2.0  # noqa: SLF001


@pytest.mark.anyio
async def test_a_site_we_never_asked_about_gets_the_default_pause(settings) -> None:
    surfer = WebSurfer(replace(settings, web_host_pause_sec=1.5))

    assert surfer._pause_for("unknown.example") == 1.5  # noqa: SLF001


@pytest.mark.anyio
async def test_the_rule_is_asked_for_the_agent_we_present_as(settings) -> None:
    """Представляться браузером, а правила читать для своего имени — обман.

    Заголовок уходит браузерный, поэтому и правило спрашивается для `*`: правило,
    написанное специально для нас, тут не спасёт — мы под ним не ходим.
    """

    surfer = _surfer(
        settings,
        "User-agent: FridayBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
    )

    assert await surfer._robots_verdict("https://site.example/page") == ""  # noqa: SLF001
