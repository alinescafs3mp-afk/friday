"""Выдача из интернета — это разные источники, а не один сайт восемь раз.

Замер 2026-08-05 на живом провайдере, десять настоящих запросов: в девяти из
десяти один домен занимал два места и больше. «Рецепт борща» — четыре страницы
одного кулинарного сайта из восьми, «цена на дизтопливо» — четыре с одного.
Ответ, собранный по такой выдаче, опирается на одно мнение, выглядя собранным по
многим.

Контроль на ОДНОМ И ТОМ ЖЕ сыром ответе провайдера (иначе сравнивались бы два
разных дня интернета): слотов сверх потолка 10 → 0, разных доменов в первой
пятёрке 37 → 42 из 50, и ни одна выдача не стала короче.

Канонизация адреса здесь — страховка, а не главный механизм: сама по себе она
поймала 2 совпадения из 80. Зеркала оказались РАЗНЫМИ страницами одного сайта, а
не вариантами одного адреса.
"""

from __future__ import annotations

from friday.web_surfer import _PER_HOST_LIMIT, SearchResult, _canonical_url, _diversify


def _result(url: str, title: str = "") -> SearchResult:
    return SearchResult(title=title or url, url=url, snippet="", source="test")


def test_one_host_cannot_take_more_than_its_share() -> None:
    raw = [
        _result("https://cook.example/borsch-1"),
        _result("https://cook.example/borsch-2"),
        _result("https://cook.example/borsch-3"),
        _result("https://cook.example/borsch-4"),
        _result("https://other.example/a"),
        _result("https://third.example/b"),
    ]

    picked = _diversify(raw, 4)

    hosts = [item.url.split("/")[2] for item in picked]
    assert hosts.count("cook.example") == _PER_HOST_LIMIT
    assert "other.example" in hosts and "third.example" in hosts


def test_the_order_of_what_survives_is_the_providers_order() -> None:
    """Переставлять выдачу мы не вправе — провайдер ранжировал, а не мы."""

    raw = [
        _result("https://a.example/1"),
        _result("https://b.example/1"),
        _result("https://a.example/2"),
        _result("https://c.example/1"),
    ]

    assert [item.url for item in _diversify(raw, 4)] == [
        "https://a.example/1",
        "https://b.example/1",
        "https://a.example/2",
        "https://c.example/1",
    ]


def test_a_narrow_question_still_gets_a_full_page() -> None:
    """«Не больше двух с сайта» не должно превращаться в «меньше результатов».

    На узком вопросе, где отвечает один сайт, схлопывание выдачи до двух строк
    было бы не разнообразием, а потерей.
    """

    raw = [_result(f"https://only.example/{index}") for index in range(6)]

    picked = _diversify(raw, 5)

    assert len(picked) == 5, "потолок съел результаты, которых больше взять неоткуда"
    # И первые два всё равно идут в исходном порядке.
    assert picked[0].url.endswith("/0") and picked[1].url.endswith("/1")


def test_the_overflow_returns_after_the_diverse_ones() -> None:
    """Добор идёт В КОНЕЦ: разнообразное показывается первым."""

    raw = [
        _result("https://big.example/1"),
        _result("https://big.example/2"),
        _result("https://big.example/3"),
        _result("https://small.example/1"),
    ]

    picked = _diversify(raw, 4)

    assert [item.url for item in picked] == [
        "https://big.example/1",
        "https://big.example/2",
        "https://small.example/1",
        "https://big.example/3",
    ]


def test_the_same_page_under_two_addresses_is_one_result() -> None:
    raw = [
        _result("https://news.example/story"),
        _result("https://www.news.example/story/"),
        _result("https://news.example/story?utm_source=telegram&utm_medium=post"),
        _result("https://news.example/story#comments"),
    ]

    assert len(_diversify(raw, 8)) == 1


def test_two_pages_of_one_site_are_not_the_same_page() -> None:
    """Канонизация не должна склеивать РАЗНЫЕ документы одного сайта.

    Именно на этом основан выбор потолка: зеркала в живой выдаче оказались
    разными страницами, и склеить их адресом нельзя — только ограничить.
    """

    raw = [_result("https://site.example/a"), _result("https://site.example/b")]

    assert len(_diversify(raw, 8)) == 2


def test_a_meaningful_parameter_survives_canonicalisation() -> None:
    """`?id=7` — это адрес документа, а `?utm_source=…` — метка перехода."""

    assert _canonical_url("https://shop.example/item?id=7") != _canonical_url(
        "https://shop.example/item?id=8"
    )
    assert _canonical_url("https://shop.example/item?id=7&utm_source=vk") == _canonical_url(
        "https://shop.example/item?id=7"
    )
    # Порядок параметров адрес не меняет.
    assert _canonical_url("https://s.example/x?b=2&a=1") == _canonical_url("https://s.example/x?a=1&b=2")


def test_mobile_and_amp_mirrors_count_as_the_same_site() -> None:
    """`m.` и `amp.` — тот же сайт, и занимать лишние места он не должен."""

    raw = [
        _result("https://ria.example/news/1"),
        _result("https://m.ria.example/news/2"),
        _result("https://amp.ria.example/news/3"),
        _result("https://tass.example/news/4"),
    ]

    picked = _diversify(raw, 4)

    assert picked[2].url == "https://tass.example/news/4", "зеркало сайта заняло чужое место"


def test_an_empty_address_is_dropped_not_counted() -> None:
    raw = [_result(""), _result("https://ok.example/1")]

    assert [item.url for item in _diversify(raw, 5)] == ["https://ok.example/1"]


def test_the_limit_is_still_the_limit() -> None:
    raw = [_result(f"https://host{index}.example/x") for index in range(20)]

    assert len(_diversify(raw, 5)) == 5
