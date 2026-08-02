"""Автопроверка появляется там, где есть с чем сверять, — и молчит там, где нет.

Заказ владельца 2026-08-02: «можно ещё убрать эту запись на ответах с
использованием интернета, что ответ не опирается ни на одну запись вашей базы?» и
«разумность появления автопроверки тоже пересмотри».

Корень один. Судья складывает данные из личных записей и результатов
инструментов; если нет ни того, ни другого, он получает строку «(нет данных)» и
обязан забраковать КАЖДОЕ утверждение — другого вердикта у него быть не может.
Замерено на недельном прогоне: совет по ужину, объяснение принципа, рассказ из
собственных знаний модели — всё уходило человеку с пометкой «не подтверждается
вашими данными», хотя своих данных на эти темы у него нет и не предполагается.

Та же болезнь у пометки об опоре на базу: ответ о курсе доллара не должен
опираться на личные записи, а предупреждение стояло под каждым таким ответом.

Предупреждение, появляющееся не по делу, обесценивает те, что по делу: человек
перестаёт их читать — и пропустит настоящее расхождение с документом.
"""

from __future__ import annotations

import inspect

from friday.agent_runtime import AgentRuntime, _grounding_warning


def test_a_web_answer_is_not_scolded_for_ignoring_the_archive() -> None:
    """Мутация: убрать ветку `from_the_web` — тест краснеет."""
    assert _grounding_warning("Курс доллара — 79,46 ₽.", False, from_the_web=True) == ""


def test_an_archive_answer_still_gets_the_warning() -> None:
    """Защита остаётся там, где она осмысленна: записи нашлись, ссылок нет."""
    text = _grounding_warning("Срок — четыре года.", False, from_the_web=False)
    assert "не опирается ни на одну запись" in text


def test_a_retelling_is_still_flagged_even_from_the_web() -> None:
    """Пересказ прежних ходов — отдельный случай, он опаснее и остаётся."""
    body = "По его данным (источник вне текущей выборки) срок — четыре года."
    assert "пересказ" in _grounding_warning(body, None, from_the_web=True)


def test_a_cited_answer_says_nothing() -> None:
    assert _grounding_warning("Смотри [K1].", True) == ""


def test_the_verification_needs_something_to_compare_against() -> None:
    """Мутация: убрать условие — тест краснеет.

    Без данных судье остаётся строка «(нет данных)», и он бракует всё подряд.
    """
    source = inspect.getsource(AgentRuntime.chat)
    checks = source[source.index("self.settings.verify_answers") : source.index("_verify_response")]
    assert "context.knowledge_hits or response.get(\"tool_evidence\")" in checks, (
        "проверка снова запускается там, где сверять не с чем"
    )
    assert "not context.small_talk" in checks, "болтовню снова проверяют на обоснованность"


def test_the_web_flag_reaches_both_notices() -> None:
    """Один признак — обе пометки: и предупреждение об опоре, и текст автопроверки."""
    source = inspect.getsource(AgentRuntime.chat)
    assert source.count("from_the_web=from_the_web") >= 2, (
        "признак «ответ из сети» доходит не до всех пометок"
    )
