"""Числа приходят числами: курс, погода и криптовалюта из открытых источников.

Замерено на двенадцати вопросах через полный путь: одиннадцать дали конкретное
значение, а «сколько стоит нефть Brent» и «какой курс биткоина» — нет. Причина
не в поиске: выдача правильная (TradingView, Investing.com, РБК), но котировка
на этих страницах рисуется скриптом, и в тексте её нет вовсе. Добор запасных
ссылок из той же выдачи не спасает — они устроены так же.

После правки те же четыре числовых вопроса — 4/4.

Здесь ничего не ходит в сеть: транспорт подменён, проверяется разбор ответов и
то, на какие вопросы прямой источник срабатывает, а на какие — нет.
"""

from __future__ import annotations

import httpx
import pytest

from friday.web_surfer._direct import _place_forms, direct_answers

_CBR_XML = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="01.08.2026" name="Foreign Currency Market">
<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal>
<Name>Доллар США</Name><Value>79,4637</Value></Valute>
<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode><Nominal>1</Nominal>
<Name>Евро</Name><Value>91,1925</Value></Valute>
<Valute ID="R01375"><NumCode>156</NumCode><CharCode>CNY</CharCode><Nominal>1</Nominal>
<Name>Юань</Name><Value>11,7694</Value></Valute>
<Valute ID="R01820"><NumCode>392</NumCode><CharCode>JPY</CharCode><Nominal>100</Nominal>
<Name>Иен</Name><Value>52,1000</Value></Valute>
</ValCurs>""".encode("windows-1251")

_GEO = {"results": [{"name": "Москва", "latitude": 55.75, "longitude": 37.62, "country_code": "RU"}]}
_FORECAST = {
    "current": {"time": "2026-08-02T12:00", "temperature_2m": 24.1, "wind_speed_10m": 3.4},
    "daily": {
        "time": ["2026-08-02", "2026-08-03", "2026-08-04"],
        "temperature_2m_max": [30.7, 24.5, 25.1],
        "temperature_2m_min": [17.9, 16.9, 14.6],
        "precipitation_sum": [1.5, 0.6, 0.0],
        "wind_speed_10m_max": [11.5, 9.0, 8.2],
    },
}
_COINS = {"bitcoin": {"usd": 63427, "rub": 5027463}}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _router(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if "cbr.ru" in host:
        return httpx.Response(200, content=_CBR_XML)
    if "geocoding-api" in host:
        return httpx.Response(200, json=_GEO)
    if "api.open-meteo" in host:
        return httpx.Response(200, json=_FORECAST)
    if "coingecko" in host:
        return httpx.Response(200, json=_COINS)
    raise AssertionError(f"неожиданный хост: {host}")


@pytest.mark.anyio
async def test_the_official_rate_comes_as_a_number():
    async with _client(_router) as client:
        sources = await direct_answers("какой сейчас курс доллара?", client)
    assert len(sources) == 1
    text = sources[0]["text"]
    assert "79,4637" in text
    assert "01.08.2026" in text
    assert "Центральный банк" in text
    assert sources[0]["url"].startswith("https://www.cbr.ru/")
    # Спрашивали доллар — евро в ответе лишний.
    assert "91,1925" not in text


@pytest.mark.anyio
async def test_two_currencies_in_one_question():
    async with _client(_router) as client:
        sources = await direct_answers("курс евро и юаня на сегодня", client)
    text = sources[0]["text"]
    assert "91,1925" in text and "11,7694" in text
    assert "79,4637" not in text


@pytest.mark.anyio
async def test_a_currency_with_a_nominal_says_so():
    """Иена котируется за 100 единиц — молчать об этом значит соврать в 100 раз."""
    async with _client(_router) as client:
        sources = await direct_answers("какой курс иены?", client)
    assert "100 единицу" in sources[0]["text"] or "за 100" in sources[0]["text"]


@pytest.mark.anyio
async def test_the_forecast_names_the_day_that_was_asked_for():
    async with _client(_router) as client:
        sources = await direct_answers("какая завтра погода в Москве?", client)
    text = sources[0]["text"]
    assert "Москва" in text
    assert "2026-08-03 (завтра)" in text
    assert "24.5" in text and "16.9" in text
    assert "Сейчас:" not in text
    assert "2026-08-02 (сегодня)" not in text
    assert "2026-08-04 (послезавтра)" not in text
    assert "Open-Meteo" in text


@pytest.mark.anyio
async def test_the_forecast_fails_closed_when_the_requested_provider_date_is_absent():
    missing_tomorrow = {
        **_FORECAST,
        "daily": {
            **_FORECAST["daily"],
            "time": ["2026-08-02", "2026-08-04", "2026-08-05"],
        },
    }

    def _missing_day(request: httpx.Request) -> httpx.Response:
        if "api.open-meteo" in request.url.host:
            return httpx.Response(200, json=missing_tomorrow)
        return _router(request)

    async with _client(_missing_day) as client:
        sources = await direct_answers("какая завтра погода в Москве?", client)
    assert sources == []


@pytest.mark.anyio
async def test_an_unsupported_weather_day_does_not_substitute_today():
    called: list[str] = []

    def _watching(request: httpx.Request) -> httpx.Response:
        called.append(request.url.host)
        return _router(request)

    async with _client(_watching) as client:
        sources = await direct_answers("какая погода в Москве в следующий понедельник?", client)
    assert sources == []
    assert called == []


@pytest.mark.anyio
async def test_the_crypto_price_comes_with_both_currencies():
    async with _client(_router) as client:
        sources = await direct_answers("какой курс биткоина сейчас?", client)
    text = sources[0]["text"]
    assert "63427" in text and "5027463" in text
    assert "CoinGecko" in text


@pytest.mark.parametrize(
    "question",
    [
        "что известно про Хасанова?",
        "сколько документов в базе?",
        "напиши отчёт по июлю",
        "кто такой Линус Торвальдс",
        "запиши: курс доллара обсудим завтра",
    ],
)
@pytest.mark.anyio
async def test_an_unrelated_question_reaches_no_direct_source(question):
    """Мутация: убрать проверку `_ASKS_RATE` — «запиши: курс…» уйдёт в ЦБ.

    Прямой источник должен молчать на всём, что не является числовым вопросом
    из этих трёх: лишний поход наружу — это и задержка, и след в чужом логе.
    """
    called: list[str] = []

    def _watching(request: httpx.Request) -> httpx.Response:
        called.append(request.url.host)
        return _router(request)

    async with _client(_watching) as client:
        sources = await direct_answers(question, client)
    assert sources == []
    assert called == [], f"без нужды сходили в {called}"


@pytest.mark.anyio
async def test_a_broken_source_never_breaks_the_search():
    """Мутация: убрать `except` — сбой открытого API уронит весь поиск."""

    def _broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сеть недоступна")

    async with _client(_broken) as client:
        assert await direct_answers("какой курс доллара?", client) == []

    def _five_hundred(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    async with _client(_five_hundred) as client:
        assert await direct_answers("какой курс доллара?", client) == []


@pytest.mark.anyio
async def test_a_place_the_geocoder_does_not_know_is_not_an_error():
    def _empty_geo(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json={"results": []})
        return _router(request)

    async with _client(_empty_geo) as client:
        assert await direct_answers("какая погода в Зажопинске?", client) == []


def test_the_geocoder_gets_a_form_it_understands():
    """Мутация: спрашивать геокодер только исходным словом — тест краснеет.

    Замерено: «Москве», «Севастополе», «Новосибирске» — геокодер отвечает
    пустотой на все три, а на именительный падеж находит сразу. Основа берётся
    тем же стеммером, что и поиск по архиву.
    """
    assert "москв" in [form.casefold() for form in _place_forms("Москве")]
    assert "севастопол" in [form.casefold() for form in _place_forms("Севастополе")]
    # Короткая основа добирается заменой последней буквы: «Уфе» → «Уфа».
    assert "Уфа" in _place_forms("Уфе")
    # Исходная форма пробуется первой: «Сочи» геокодер знает как есть.
    assert _place_forms("Сочи")[0] == "Сочи"


@pytest.mark.anyio
async def test_direct_sources_are_wired_into_research():
    """Мутация: убрать вызов из `research` — работающий модуль никто не спросит."""
    import inspect

    from friday.web_surfer import WebSurfer

    source = inspect.getsource(WebSurfer.research)
    assert "direct_answers(query" in source
    direct_admission = source.index("for item in direct:")
    first_fetch = source.index("await fetch_batch(selected")
    assert "record_item(item, attempted=False)" in source[direct_admission:first_fetch]
    assert direct_admission < first_fetch, "прямые числа не встают перед страницами"
