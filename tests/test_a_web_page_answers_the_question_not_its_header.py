"""Загруженная страница отдаётся куском вокруг совпадения, а не шапкой.

Находка Grok (`grok/PROPOSALS.md` №1), проверена и принята. `FetchResult.to_dict`
брал `self.text[:preview_chars]`, то есть модель получала верх страницы — меню,
баннер согласия на cookie, хлебные крошки, — а искомое место оставалось за
границей среза.

Тот же дефект проект уже чинил дважды: сперва в контекстной выдержке («Quote the
passage that matched, not the top of the document»), затем в инструменте памяти,
где до модели доходил титульный лист документа. До веба починка не доехала.
"""

from __future__ import annotations

from friday.web_surfer import FetchResult

HEADER = "Главная Меню Навигация Мы используем cookie. Согласиться. Реклама. " * 30
ANSWER = "Срок поверки весов ВЛТЭ-500 составляет двенадцать месяцев."
PAGE = HEADER + ANSWER + " Дальше идёт подвал и ссылки на другие разделы. " * 10


def _page() -> FetchResult:
    return FetchResult(
        url="https://example.org/page",
        title="Поверка весов",
        text=PAGE,
        text_length=len(PAGE),
        status_code=200,
        error="",
    )


def test_the_passage_that_matched_is_what_the_model_receives():
    rendered = _page().to_dict(preview_chars=400, query="срок поверки весов")
    assert "двенадцать месяцев" in rendered["text"], (
        "модель получила шапку страницы вместо места, отвечающего на вопрос:\n" + rendered["text"][:200]
    )


def test_without_a_query_the_head_is_still_honest():
    """Без запроса «совпадение» не определено, и придумывать его хуже, чем взять начало."""
    rendered = _page().to_dict(preview_chars=200)
    assert rendered["text"] == PAGE[:200]


def test_truncation_still_reports_itself():
    rendered = _page().to_dict(preview_chars=300, query="срок поверки весов")
    assert rendered["truncated"] is True
    assert rendered["text_length"] == len(PAGE), "полная длина страницы обязана остаться"

    whole = FetchResult(
        url="https://example.org/short",
        title="Коротко",
        text="Две строки текста.",
        text_length=18,
        status_code=200,
        error="",
    ).to_dict(preview_chars=400, query="строки")
    assert whole["truncated"] is False
