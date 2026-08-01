"""Отказ поисковика — не факт о том, что в интернете ничего нет.

Замерено 2026-08-01 на 20 разнообразных запросах, критерий (доля непустых выдач)
объявлен до замера:

    DuckDuckGo html   1/20   — 19 ответов HTTP **202**, анти-бот заглушка
    DuckDuckGo lite   0/20
    Brave без ключа   6/20
    Mojeek/Bing/Startpage 0/20
    Яндекс по ключу владельца  **20/20**, медиана 0.73 с

202 — это 2xx, поэтому `raise_for_status()` пропускал его как успех, разметки
результатов в заглушке нет, и функция возвращала пустой список. Человек слышал
«похоже, внешние источники сейчас не доступны» и когда в интернете правда ничего
нет, и когда нас просто отшили — а это разные вещи: во втором случае надо
спросить следующего провайдера, а не сообщать выдуманный факт.
"""

from __future__ import annotations

import base64
import dataclasses

import httpx
import pytest

from friday.web_surfer import AllProvidersRefusedError, ProviderRefusedError, WebSurfer

_YANDEX_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response><results><grouping><group><doc id="X">
<url>https://cbr.ru/hd_base/KeyRate/</url><domain>cbr.ru</domain>
<title><hlword>Ключевая</hlword> ставка Банка России</title>
<passages><passage>Ключевая <hlword>ставка</hlword> — 21% годовых.</passage></passages>
</doc></group></results></response></yandexsearch>"""


def _surfer(settings, handler, **overrides) -> WebSurfer:
    surfer = WebSurfer(dataclasses.replace(settings, **overrides))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    surfer._client = client  # noqa: SLF001 — подменяем именно транспорт, не логику
    return surfer


@pytest.mark.anyio
async def test_202_from_duckduckgo_is_a_refusal_not_an_empty_result(settings):
    """Мутация: убрать 202 из `_REFUSAL_STATUS` — тест краснеет.

    Тело намеренно содержит ПОЛНОЦЕННУЮ разметку результатов, хотя настоящая
    заглушка приходит пустой. Иначе тест проходил бы за счёт соседней защиты
    («200 без разметки — тоже отказ») и не проверял бы ту, ради которой написан:
    первая редакция так и делала, и мутация кода её не красила.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            text=(
                "<div class='result'><a class='result__a' href='https://example.org/x'>Ссылка</a>"
                "<div class='result__snippet'>Текст</div></div>"
            ),
        )

    surfer = _surfer(settings, handler, yandex_search_api_key="")
    with pytest.raises(AllProvidersRefusedError):
        await surfer.search("ключевая ставка")
    await surfer.close()


@pytest.mark.anyio
async def test_a_provider_that_honestly_found_nothing_is_not_a_refusal(settings):
    """Пустая выдача при живой разметке — это ответ, а не отказ.

    Яндекс отвечает 200 и корректным XML без единого `<doc>`: искать дальше
    незачем, и выдумывать отказ тоже нельзя.
    """
    empty = base64.b64encode(
        b'<?xml version="1.0"?><yandexsearch><response><results></results></response></yandexsearch>'
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rawData": empty})

    surfer = _surfer(settings, handler, yandex_search_api_key="test-key")
    assert await surfer.search("заведомо несуществующее слово") == []
    await surfer.close()


@pytest.mark.anyio
async def test_the_chain_moves_on_when_the_first_provider_refuses(settings):
    """Отказ первого не должен стоить человеку ответа."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen.append(host)
        if "yandex" in host:
            return httpx.Response(429, json={"message": "quota exceeded"})
        if "brave" in host:
            return httpx.Response(
                200,
                text=(
                    "<div id='results'><div class='snippet' data-type='web'>"
                    "<a href='https://example.org/rate'>Ставка ЦБ</a>"
                    "<div class='snippet-description'>21% годовых</div></div></div>"
                ),
            )
        return httpx.Response(202, text="anomaly")

    surfer = _surfer(settings, handler, yandex_search_api_key="test-key")
    results = await surfer.search("ключевая ставка", max_results=3)
    await surfer.close()

    assert [item.url for item in results] == ["https://example.org/rate"]
    assert any("yandex" in host for host in seen), "первым обязан спрашиваться Яндекс"
    assert any("brave" in host for host in seen), "после отказа не спросили следующего"


@pytest.mark.anyio
async def test_yandex_xml_is_parsed_into_results(settings):
    """Выдача Яндекса приходит base64-XML-ом; подсветка `<hlword>` — не текст."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Api-Key test-key"
        return httpx.Response(
            200, json={"rawData": base64.b64encode(_YANDEX_XML.encode()).decode()}
        )

    surfer = _surfer(settings, handler, yandex_search_api_key="test-key")
    results = await surfer.search("ключевая ставка", max_results=5)
    await surfer.close()

    assert len(results) == 1
    assert results[0].url == "https://cbr.ru/hd_base/KeyRate/"
    assert results[0].source == "yandex"
    assert "hlword" not in results[0].title, "разметка подсветки просочилась в заголовок"
    assert results[0].title == "Ключевая ставка Банка России"
    assert "21%" in results[0].snippet


@pytest.mark.anyio
async def test_yandex_error_inside_a_200_is_a_refusal(settings):
    """Исчерпанная квота приходит ВНУТРИ успешного ответа, а не кодом."""
    body = base64.b64encode(
        (
            '<?xml version="1.0"?><yandexsearch><response>'
            '<error code="55">Превышен лимит запросов</error></response></yandexsearch>'
        ).encode()
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if "yandex" in request.url.host:
            return httpx.Response(200, json={"rawData": body})
        return httpx.Response(202, text="anomaly")

    surfer = _surfer(settings, handler, yandex_search_api_key="test-key")
    with pytest.raises(AllProvidersRefusedError):
        await surfer.search("ключевая ставка")
    await surfer.close()


@pytest.mark.anyio
async def test_duckduckgo_200_without_markup_is_also_a_refusal(settings):
    """На бессмысленный запрос DuckDuckGo всё равно отдаёт ссылки.

    Значит 200 без единого `.result` — это заглушка, а не пустая выдача.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body><p>ничего похожего на выдачу</p></body></html>")

    surfer = _surfer(settings, handler)
    with pytest.raises(ProviderRefusedError):
        await surfer._search_duckduckgo_html("что угодно", 5)  # noqa: SLF001
    await surfer.close()
