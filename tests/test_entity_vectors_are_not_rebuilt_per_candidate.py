"""Вектор имён сущностей строился заново для каждого кандидата.

Замерено на живом архиве 2026-08-03: `lexical_vector` — самое частое место в
поиске (3186 вызовов на четырёх запросах), и 2206 из них приходились на вектор
сущностей внутри `_field_scores`. При этом на 1533 документа набор имён принимает
всего 509 разных значений, а 514 документов (33%) не имеют сущностей вовсе — для
них строился вектор из пустой строки.

Кэшировать его вместе с полями документа было нельзя: имена приходят из таблицы
связей, которая меняется, не трогая строку документа, и ключ по (id, version)
устарел бы молча. Ключом стали САМИ имена — тогда устаревать нечему.

Честная оговорка о величине выигрыша: сам по себе он мал. Замер по секундомеру
на восьми запросах дал 16.495 -> 16.003 с (3%) вместе с починкой предела
переранжировщика; профиль cProfile обещал больше, потому что преувеличивает
стоимость частых мелких вызовов Python. Расхождение приборов и привело к
настоящей причине — сетевому ожиданию переранжировщика (79% времени).
"""

from __future__ import annotations

import pytest

from friday.retrieval import HybridSearcher


@pytest.fixture
def searcher(storage):
    return HybridSearcher(storage, None)


def test_the_same_names_are_vectorised_once(searcher, monkeypatch) -> None:
    """Мутация: строить вектор без кэша — счётчик покажет два вызова."""
    from friday import retrieval

    calls: list[str] = []
    original = retrieval.lexical_vector

    def counted(text: str):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(retrieval, "lexical_vector", counted)

    first = searcher._entity_names_vector(["в/ч 30926", "Бутко"])
    second = searcher._entity_names_vector(["в/ч 30926", "Бутко"])

    assert first == second
    assert len(calls) == 1, f"вектор построен {len(calls)} раза для одних и тех же имён"


def test_different_names_are_different_vectors(searcher) -> None:
    """Кэш не должен путать наборы: ключ — сами имена, а не их число."""
    one = searcher._entity_names_vector(["Бутко"])
    two = searcher._entity_names_vector(["Хасанов"])
    assert one != two


def test_the_order_of_names_is_part_of_the_key(searcher) -> None:
    """Разный порядок — разный ключ, но вектор обязан совпасть по существу.

    Мешок слов от порядка не зависит; проверяется, что кэш не выдаёт за один
    набор другой.
    """
    assert searcher._entity_names_vector(["а", "б"]) == searcher._entity_names_vector(["б", "а"])


def test_no_entities_costs_nothing(searcher, monkeypatch) -> None:
    """Треть корпуса не имеет сущностей — для них вектор не строится вовсе."""
    from friday import retrieval

    calls: list[str] = []
    monkeypatch.setattr(retrieval, "lexical_vector", lambda text: calls.append(text) or {})

    assert searcher._entity_names_vector([]) == {}
    assert calls == [], "для пустого набора имён всё ещё строится вектор"


def test_the_cache_is_bounded(searcher) -> None:
    """Личный корпус конечен, но кэш по именам растёт от ЗАПРОСОВ, не от корпуса."""
    from friday.retrieval import _VECTOR_CACHE_MAX

    for index in range(_VECTOR_CACHE_MAX + 50):
        searcher._entity_names_vector([f"имя-{index}"])
    assert len(searcher._entity_vector_cache) <= _VECTOR_CACHE_MAX


def test_field_scores_use_the_cache(searcher) -> None:
    """Проверяется подключённое: считает `_field_scores`, а не только помощник."""
    import inspect

    source = inspect.getsource(HybridSearcher._field_scores)
    assert "self._entity_names_vector(entity_names)" in source, "кэш снова в обход"
    assert "lexical_vector(entities)" not in source
