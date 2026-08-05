"""Russian inflection must not decide whether a document is findable.

«Казань», «в Казани», «под Казанью» are one word to a reader and three strings
to a token-matching ranker. Measured on the owner's own corpus before this
existed: a document containing every word of the query in an oblique case scored
**0.0597** lexically — under the evidence floor — and reached the answer only
because it seeded the graph and its own entities vouched for it back.

The stemmer is Snowball's Russian algorithm. These tests pin what it must fold,
what it must NOT fold, and — the part a unit test of the stemmer alone would
miss — that the ranker actually uses it on both sides.
"""

from __future__ import annotations

import pytest

from friday.morphology import stem
from friday.retrieval import lexical_vector, sparse_cosine


@pytest.mark.parametrize(
    "forms",
    [
        ("Казань", "Казани", "Казанью", "Казанем"),
        ("чёрный", "черные", "черных", "чёрного", "черным"),
        ("дежурство", "дежурства", "дежурств", "дежурству", "дежурствами"),
        ("подписка", "подписку", "подписки", "подпиской"),
        ("конфигурация", "конфигурации", "конфигураций", "конфигурацию"),
        ("документ", "документа", "документы", "документов", "документам"),
        ("ведомость", "ведомости", "ведомостей"),
        ("работать", "работает", "работали", "работаю"),
    ],
)
def test_one_word_in_many_cases_is_one_stem(forms):
    stems = {stem(form) for form in forms}
    assert len(stems) == 1, f"{forms} -> {stems}"


@pytest.mark.parametrize(
    ("left", "right"),
    [("стол", "стал"), ("дом", "дым"), ("год", "гад"), ("мир", "мор"), ("рука", "река")],
)
def test_different_words_stay_different(left, right):
    assert stem(left) != stem(right)


@pytest.mark.parametrize(
    "token",
    ["BRK.A", "PK-04-04", "autovacuum_vacuum_scale_factor", "12.5", "C++", "GPL-3.0", "ERC-20"],
)
def test_identifiers_are_never_touched(token):
    """`identifier_coverage` drops any candidate that lacks the identifier
    verbatim, so a stemmer that reshaped one would empty the answer."""
    assert stem(token) == token


def test_a_short_word_keeps_its_shape():
    """«дом» -> «до» would match every second word in a Russian corpus."""
    for word in ("дом", "код", "сон", "год"):
        assert stem(word) == word


def test_the_ranker_folds_both_sides():
    """The wiring, not the mechanism: a unit-tested stemmer nobody calls is a
    stemmer that does nothing. Both texts go through `lexical_vector`."""
    query = lexical_vector("график отпусков караула")
    inflected = lexical_vector("графику отпусков караулом")
    unrelated = lexical_vector("рецепт борща со свёклой")

    assert sparse_cosine(query, inflected) > 0.8, "the same words in another case"
    assert sparse_cosine(query, unrelated) < 0.2


def test_an_oblique_case_becomes_real_evidence():
    """The measurement the honest graph seeding depends on.

    «Казань» against a document saying «в Казани» scored **0.0593** lexically —
    under `_LEXICAL_EVIDENCE_MIN` (0.075). That is why the graph used to be
    seeded from unfiltered lexical hits: the circular route (document → its
    entities → back to the document) was standing in for morphology, and
    filtering the seeds without fixing this first was measured to make retrieval
    worse. With folding the same pair scores **0.3317**, four times the floor, so
    the seeds can be filtered honestly.
    """
    from friday.retrieval import _LEXICAL_EVIDENCE_MIN

    score = sparse_cosine(
        lexical_vector("Казань"),
        lexical_vector("Поездка в Казани прошла хорошо, встретились с коллегами из филиала."),
    )
    assert score >= _LEXICAL_EVIDENCE_MIN, f"{score:.4f} is still under the evidence floor"


def test_folding_beats_no_folding_on_an_oblique_case():
    """The number that made this worth doing, in miniature."""
    from friday import morphology as _morphology

    query_text, document_text = "Казань", "поездка в Казани прошла хорошо"
    before_min = _morphology._MIN_STEM_INPUT
    try:
        # `stem` is memoized, so flipping the switch is not enough — a cached
        # stem computed under the other setting would answer instead, and the
        # measurement would compare a variant against itself.
        # Порог стал аргументом со значением по умолчанию, поэтому подмена
        # модульной константы его больше не отключает — подменяется сама функция.
        original = _morphology.stem
        _morphology.stem = lambda token, min_input=0: token  # type: ignore[assignment]
        import friday.retrieval as _retrieval

        _retrieval.stem = _morphology.stem  # type: ignore[assignment]
        without = sparse_cosine(lexical_vector(query_text), lexical_vector(document_text))
        _morphology.stem = original  # type: ignore[assignment]
        _retrieval.stem = original  # type: ignore[assignment]
        _morphology.stem.cache_clear()
        with_stemming = sparse_cosine(lexical_vector(query_text), lexical_vector(document_text))
    finally:
        _morphology._MIN_STEM_INPUT = before_min
        _morphology.stem.cache_clear()
    assert with_stemming > without * 1.5, f"{without:.4f} -> {with_stemming:.4f}"


def test_short_but_ordinary_words_lose_their_case_endings():
    """Мутация: вернуть `_MIN_STEM_INPUT = 5` — тест краснеет.

    Порог входа был пять букв, и это стоило целого класса слов, которыми полны
    документы: «акте», «акта», «цеха», «коде», «года». Замерено на боевом
    корпусе: вопрос «что сказано в акте №77?» не находил только что принятый
    документ НИ НА КАКОЙ позиции, тогда как «акт 77» ставил его первым.

    Порог снижен ТОЛЬКО для подбора кандидатов: снижение его в оценке веса
    портило и болтовню («как дела?» → «дел» давало уверенное попадание), и
    разметку упоминаний («Иван» → «ива» склеивает разных людей). Расширять поиск
    и огрублять оценку — разные решения.
    """
    from friday.morphology import LEXICAL_MIN_STEM_INPUT

    for surface, expected in (
        ("акте", "акт"),
        ("акта", "акт"),
        ("актом", "акт"),
        ("цеха", "цех"),
        ("коде", "код"),
        ("года", "год"),
        ("дома", "дом"),
        ("сына", "сын"),
    ):
        assert stem(surface, LEXICAL_MIN_STEM_INPUT) == expected, f"{surface!r} не свёлся к {expected!r}"
        # По умолчанию порог прежний: имена («Иван» → «ива») ломать нельзя.
        assert stem("Иван") == "Иван"


def test_the_output_floor_still_protects_the_shortest_words():
    """От «дом» → «до» защищает порог ВЫХОДА, а не входа.

    Основа короче трёх букв не принимается, и слово возвращается как было —
    иначе «дом» совпал бы с каждым вторым словом корпуса.
    """
    for word in ("дом", "код", "цех", "сын", "год"):
        assert stem(word) == word
    # Трёхбуквенные и короче не трогаются вовсе.
    for word in ("он", "мы", "два"):
        assert stem(word) == word


def test_the_index_is_reached_by_the_stem_not_the_case_form():
    """Мутация: вернуть в `_fts_terms` точные слова — тест краснеет.

    Индекс хранит текст как он написан, а вопрос задают в другом падеже.
    Замерено на боевом корпусе: «что сказано в акте №77?» не находил только что
    принятый документ НИ НА КАКОЙ позиции — в документе слово «акт», в вопросе
    «акте», и до пула кандидатов документ не доходил вовсе. «акт*» покрывает обе
    формы сразу.

    Критерий объявлен до замера: recall@10 на 78 эталонах не должен упасть. Он
    вырос — 0.7179 → 0.7436 при MRR 0.4283 → 0.4258.
    """
    from friday.storage._knowledge import _fts_terms

    assert "акт*" in _fts_terms("что сказано в акте №77?")
    # Слово, уже стоящее в начальной форме, остаётся собой: основа от него не
    # отличается, и превращать «акт» в «акт*» значило бы расширять поиск там,
    # где человек назвал слово точно.
    assert "акт" in _fts_terms("акт приёма-передачи")

    # Слово заменяется основой, а не дополняется ею: бюджет считает слова.
    assert _fts_terms("как чинить кластер") == ["как", "чин*", "кластер"]

    # Оба написания «ё» сохраняются: индекс хранит написанное, и документ с
    # «чёрных» не найдётся по «черн*».
    terms = _fts_terms("чёрных списков")
    assert "чёрн*" in terms and "черн*" in terms

    # Слишком короткая основа префиксом не становится — «до*» нашло бы половину
    # корпуса.
    assert all(not term.startswith("до*") for term in _fts_terms("дом"))
