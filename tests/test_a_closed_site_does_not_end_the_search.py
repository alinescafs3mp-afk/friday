"""Сайт, закрытый от роботов, не должен обрывать поиск.

Живой вопрос владельца 2026-08-02: «Сколько стоит самая дешёвая 5090 в ДНС».
Пятница ответила, что точную цену получить не удалось.

Воспроизведено: dns-shop.ru отвечает HTTP 401 на КАЖДОЙ своей странице —
прочитано ноль знаков из пяти источников. Запрос содержал название магазина,
поэтому и запасные ссылки в выдаче вели туда же, и вторая волна повторила тот же
отказ ещё раз. При этом цена есть на десятке других сайтов.

Домен, уже ответивший отказом, отодвигается в конец очереди, а не выбрасывается:
если других нет, попытаться всё равно стоит — вдруг отказала одна страница, а не
весь сайт.
"""

from __future__ import annotations

import inspect

import pytest

from friday.web_surfer import WebSurfer, _host_of


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.dns-shop.ru/product/x", "dns-shop.ru"),
        ("https://dns-shop.ru/p", "dns-shop.ru"),
        ("https://market.yandex.ru/x", "market.yandex.ru"),
        ("", ""),
        ("не ссылка", ""),
    ],
)
def test_the_host_ignores_the_www_prefix(url: str, expected: str) -> None:
    """`dns-shop.ru` и `www.dns-shop.ru` — один магазин, а не два сайта."""
    assert _host_of(url) == expected


def test_the_second_wave_prefers_other_sites() -> None:
    """Мутация: вернуть `spare[:source_limit]` — тест краснеет.

    Тогда вторая попытка снова уходит на тот же закрытый домен, и человек
    получает «цену узнать не удалось» вместо цены с соседнего сайта.
    """
    source = inspect.getsource(WebSurfer.research)
    wave = source[source.index("while len(complete) < target_sources") : source.index("missing = max")]
    assert "attempted_hosts" in wave, "уже проверенные домены больше не запоминаются"
    assert "elsewhere" in wave and "same_place" in wave, (
        "вторая волна снова идёт по порядку выдачи — то есть туда же, откуда отказали"
    )
    assert "elsewhere + same_place" in wave, "отказавший домен выброшен совсем, а не отодвинут"


def test_a_refused_domain_is_still_tried_when_there_is_nothing_else() -> None:
    """Порядок, а не запрет: единственный доступный сайт остаётся в очереди."""
    source = inspect.getsource(WebSurfer.research)
    wave = source[source.index("while len(complete) < target_sources") : source.index("missing = max")]
    # `same_place` идёт в тот же список, а не отбрасывается фильтром.
    assert "same_place = [" in wave
    assert "(elsewhere + same_place)[:deficit]" in wave
