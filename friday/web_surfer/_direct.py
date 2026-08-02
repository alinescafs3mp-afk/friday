"""Прямые источники данных: там, где страница отдаёт график, а не число.

Замерено на двенадцати вопросах через полный путь: одиннадцать получили
конкретное значение, а «сколько стоит нефть Brent» и «какой курс биткоина» —
нет. Причина не в поиске: выдача правильная (TradingView, Investing.com, РБК),
но котировка на этих страницах рисуется скриптом, и в тексте её нет вовсе.
Добор запасных ссылок из той же выдачи не помогает — они устроены так же.

Поэтому для трёх самых частых числовых вопросов — курс валюты, погода,
криптовалюта — есть открытые источники, отдающие ЧИСЛА, а не разметку:

* ЦБ РФ (`cbr.ru/scripts/XML_daily.asp`) — официальный курс, XML;
* Open-Meteo — прогноз по координатам, без ключа;
* CoinGecko — цена криптовалюты, без ключа.

Это не «подгонка под сайт»: источник официальный или общепризнанный, ответ
проверяемый, ссылка приводится человеку как обычно. Всё остальное по-прежнему
ищется поиском — сюда добавляется только то, что поиском в принципе не берётся.

Ни один сбой здесь не должен мешать обычному поиску: любая ошибка гасится и
превращается в «прямого ответа нет».
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree

import httpx

LOGGER = logging.getLogger(__name__)

_TIMEOUT = 8.0

#: Валюты, которые называют по-русски. Ключ — код ЦБ, значения — как их зовут.
_CURRENCIES: dict[str, tuple[str, ...]] = {
    "USD": ("доллар", "долар", "бакс", "usd", "dollar"),
    "EUR": ("евро", "eur", "euro"),
    "CNY": ("юан", "cny", "yuan"),
    "GBP": ("фунт", "gbp", "pound"),
    "JPY": ("иен", "йен", "jpy", "yen"),
    "TRY": ("лир", "try", "lira"),
    "KZT": ("тенге", "kzt"),
    "BYN": ("белорусск", "byn"),
    "CHF": ("франк", "chf"),
    "AED": ("дирхам", "aed"),
}
_ASKS_RATE = re.compile(
    r"курс\w*|сколько\s+стоит|стоимост\w*|цена\s|почём|по\s+чём|обменн\w*|exchange\s+rate",
    re.IGNORECASE,
)
_ASKS_WEATHER = re.compile(r"погод\w*|прогноз\w*\s+погод|температур\w*|дожд\w*|снег\b", re.IGNORECASE)
_WEATHER_PLACE = re.compile(
    r"(?:погод\w*|температур\w*|прогноз\w*)[^.?!]{0,20}?\bв(?:о)?\s+([А-ЯЁ][\w-]+(?:\s+[А-ЯЁ][\w-]+)?)"
    r"|\bв(?:о)\s+([А-ЯЁ][\w-]+)[^.?!]{0,20}?погод",
    re.IGNORECASE,
)
_CRYPTO: dict[str, tuple[str, ...]] = {
    "bitcoin": ("биткоин", "биткойн", "битк", "bitcoin", "btc"),
    "ethereum": ("эфир", "эфириум", "ethereum", "eth"),
    "tether": ("тезер", "tether", "usdt"),
    "the-open-network": ("тонкоин", "toncoin", "ton "),
}
#: Когда «завтра» — это завтра. Прогноз просят на день, а не на час.
_ASKS_TOMORROW = re.compile(r"\bзавтра\w*|\btomorrow\b", re.IGNORECASE)
_ASKS_DAY_AFTER = re.compile(r"послезавтра", re.IGNORECASE)


@dataclass(frozen=True)
class DirectAnswer:
    """Готовое число из открытого источника — в той же форме, что страница поиска."""

    title: str
    url: str
    text: str
    source: str

    def as_source(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "search_title": self.title,
            "text": self.text,
            "text_length": len(self.text),
            "status_code": 200,
            "error": "",
            "truncated": False,
            "snippet": self.text[:300],
            "source": self.source,
        }


#: Начало фразы, после которого идёт материал на сохранение, а не вопрос.
#: «Запиши: курс доллара обсудим завтра» — заметка, и ходить за курсом в ЦБ по
#: ней незачем. Проверка живёт здесь, а не только у вызывающего: модуль обязан
#: быть безопасен сам по себе — ровно этим классом ошибки пересланный приказ
#: уходил в публичный поисковик.
_LOOKS_LIKE_A_NOTE = re.compile(
    r"^\s*(?:запиши|запишите|сохрани|сохраните|заметка|запомни|запомните|"
    r"добавь|добавьте|занеси|внеси|отметь|пометь)\b",
    re.IGNORECASE,
)


def _is_a_note(query: str) -> bool:
    return bool(_LOOKS_LIKE_A_NOTE.match(str(query or "")))


def _wanted_currencies(query: str) -> list[str]:
    lowered = query.casefold()
    if _is_a_note(query) or not _ASKS_RATE.search(lowered):
        return []
    found = [code for code, names in _CURRENCIES.items() if any(name in lowered for name in names)]
    # «какой сегодня курс?» без валюты — самые ходовые две.
    if not found and re.search(r"курс\w*\s*(?:валют\w*|на\s+сегодня|цб)?\s*[?.]?$", lowered):
        return ["USD", "EUR"]
    return found


def _wanted_crypto(query: str) -> list[str]:
    lowered = query.casefold()
    if _is_a_note(query) or not _ASKS_RATE.search(lowered):
        return []
    return [coin for coin, names in _CRYPTO.items() if any(name in lowered for name in names)]


def _wanted_place(query: str) -> str:
    if _is_a_note(query) or not _ASKS_WEATHER.search(query):
        return ""
    match = _WEATHER_PLACE.search(query)
    if not match:
        return ""
    place = (match.group(1) or match.group(2) or "").strip(" ,.?!")
    # «в Москве» → «Москве»: геокодер понимает и падеж, но именительный точнее.
    return place


async def _currency_rates(client: httpx.AsyncClient, codes: list[str]) -> DirectAnswer | None:
    """Официальный курс ЦБ РФ. XML в windows-1251, поэтому читаются байты."""
    response = await client.get(
        "https://www.cbr.ru/scripts/XML_daily.asp", timeout=_TIMEOUT, follow_redirects=True
    )
    if response.status_code != 200:
        return None
    root = ElementTree.fromstring(response.content)  # noqa: S314 — источник фиксированный
    on_date = str(root.get("Date") or "")
    lines: list[str] = []
    for valute in root.findall("Valute"):
        code = (valute.findtext("CharCode") or "").strip()
        if code not in codes:
            continue
        value = (valute.findtext("Value") or "").strip()
        nominal = (valute.findtext("Nominal") or "1").strip()
        name = (valute.findtext("Name") or code).strip()
        unit = f"{nominal} " if nominal not in {"1", ""} else ""
        lines.append(f"{name} ({code}): {value} ₽ за {unit}единицу")
    if not lines:
        return None
    return DirectAnswer(
        title=f"Официальный курс ЦБ РФ на {on_date}",
        url="https://www.cbr.ru/currency_base/daily/",
        text=(
            f"Официальные курсы валют Банка России на {on_date}:\n"
            + "\n".join(lines)
            + "\nИсточник: Центральный банк Российской Федерации."
        ),
        source="cbr",
    )


def _place_forms(place: str) -> list[str]:
    """Формы названия для геокодера: он не знает русских падежей.

    Замерено: «в Москве», «в Севастополе», «в Новосибирске» — геокодер отвечает
    пустотой на все три, а на «Москва», «Севастополь», «Новосибирск» находит
    сразу. Основа берётся тем же стеммером, что и поиск по архиву, чтобы падежи
    сводились одинаково во всей системе; короткие основы («Уфе» → «уф»)
    добираются заменой последней буквы.
    """
    from friday.morphology import stem

    root = stem(place.casefold())
    forms = [place, root, f"{root}а", f"{root}ь", f"{root}я"]
    if len(place) > 2:
        forms.extend([f"{place[:-1]}а", f"{place[:-1]}ь"])
    seen: set[str] = set()
    unique: list[str] = []
    for form in forms:
        key = form.casefold()
        if form and key not in seen:
            seen.add(key)
            unique.append(form)
    return unique


async def _weather(client: httpx.AsyncClient, place: str, query: str) -> DirectAnswer | None:
    hits: list[dict[str, Any]] = []
    for candidate in _place_forms(place):
        geo = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": candidate, "count": 1, "language": "ru"},
            timeout=_TIMEOUT,
        )
        if geo.status_code != 200:
            return None
        hits = (geo.json() or {}).get("results") or []
        if hits:
            break
    if not hits:
        return None
    spot = hits[0]
    latitude, longitude = spot.get("latitude"), spot.get("longitude")
    name = str(spot.get("name") or place)
    forecast = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
            "current": "temperature_2m,wind_speed_10m",
            "timezone": "auto",
            "forecast_days": 4,
        },
        timeout=_TIMEOUT,
    )
    if forecast.status_code != 200:
        return None
    payload = forecast.json() or {}
    daily = payload.get("daily") or {}
    days = list(daily.get("time") or [])
    if not days:
        return None
    # Какой именно день спрашивают: «завтра» — второй в списке, «послезавтра» — третий.
    wanted = 0
    if _ASKS_DAY_AFTER.search(query):
        wanted = 2
    elif _ASKS_TOMORROW.search(query):
        wanted = 1
    wanted = min(wanted, len(days) - 1)

    def _at(key: str, index: int) -> Any:
        values = daily.get(key) or []
        return values[index] if index < len(values) else None

    lines = [f"Прогноз погоды: {name}."]
    current = payload.get("current") or {}
    if wanted == 0 and current.get("temperature_2m") is not None:
        lines.append(
            f"Сейчас: {current['temperature_2m']}°C, ветер {current.get('wind_speed_10m', '—')} км/ч."
        )
    for index in range(len(days)):
        marker = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(index, "")
        label = f"{days[index]}{f' ({marker})' if marker else ''}"
        lines.append(
            f"{label}: от {_at('temperature_2m_min', index)}°C до {_at('temperature_2m_max', index)}°C, "
            f"осадки {_at('precipitation_sum', index)} мм, ветер до "
            f"{_at('wind_speed_10m_max', index)} км/ч."
        )
    lines.append("Источник: Open-Meteo, данные национальных метеослужб.")
    return DirectAnswer(
        title=f"Погода: {name}",
        url=f"https://open-meteo.com/en/docs#latitude={latitude}&longitude={longitude}",
        text="\n".join(lines),
        source="open-meteo",
    )


async def _crypto_prices(client: httpx.AsyncClient, coins: list[str]) -> DirectAnswer | None:
    response = await client.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ",".join(coins), "vs_currencies": "usd,rub"},
        timeout=_TIMEOUT,
    )
    if response.status_code != 200:
        return None
    payload = response.json() or {}
    names = {
        "bitcoin": "Bitcoin (BTC)",
        "ethereum": "Ethereum (ETH)",
        "tether": "Tether (USDT)",
        "the-open-network": "Toncoin (TON)",
    }
    lines: list[str] = []
    for coin in coins:
        prices = payload.get(coin) or {}
        if not prices:
            continue
        lines.append(
            f"{names.get(coin, coin)}: {prices.get('usd', '—')} USD, {prices.get('rub', '—')} ₽"
        )
    if not lines:
        return None
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return DirectAnswer(
        title="Курс криптовалют (CoinGecko)",
        url="https://www.coingecko.com/",
        text=f"Цены на {stamp}:\n" + "\n".join(lines) + "\nИсточник: CoinGecko.",
        source="coingecko",
    )


async def direct_answers(query: str, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Прямые ответы на числовой вопрос, если он из тех трёх, что берутся так.

    Возвращает источники в том же виде, что и прочитанные страницы, — вызывающий
    ставит их первыми, и модель видит числа, а не «график рисуется скриптом».
    """
    query = str(query or "").strip()
    if not query:
        return []
    answers: list[DirectAnswer] = []
    try:
        codes = _wanted_currencies(query)
        if codes:
            rate = await _currency_rates(client, codes)
            if rate:
                answers.append(rate)
        coins = _wanted_crypto(query)
        if coins:
            price = await _crypto_prices(client, coins)
            if price:
                answers.append(price)
        place = _wanted_place(query)
        if place:
            weather = await _weather(client, place, query)
            if weather:
                answers.append(weather)
    except Exception:  # noqa: BLE001 — прямой источник не должен мешать обычному поиску
        LOGGER.warning("Прямой источник данных не ответил", exc_info=True)
    return [answer.as_source() for answer in answers]
