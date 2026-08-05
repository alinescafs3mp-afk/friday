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
    """Мутация: снять условие — тест краснеет.

    Владелец просил убрать эту пометку дважды. Своей записи о курсе доллара у
    него нет, и упрекать ответ в том, что он на неё не опирается, — бессмыслица.
    """
    assert _grounding_warning("Курс доллара — 79,46 ₽.", False, about_his_own_papers=False) == ""


def test_the_warning_survives_only_for_his_own_papers() -> None:
    """Единственный случай, ради которого пометка и оставлена.

    Человек спросил о СВОИХ данных, архив ответил уверенно, а модель его не
    использовала — то есть сочинила про документы, лежащие перед ней.
    """
    text = _grounding_warning("Срок — четыре года.", False, about_his_own_papers=True)
    assert "не опирается ни на одну запись" in text


def test_a_retelling_is_still_flagged_anywhere() -> None:
    """Пересказ прежних ходов — отдельный случай, он опаснее и остаётся."""
    body = "По его данным (источник вне текущей выборки) срок — четыре года."
    assert "пересказ" in _grounding_warning(body, None, about_his_own_papers=False)


def test_a_cited_answer_says_nothing() -> None:
    assert _grounding_warning("Смотри [K1].", True) == ""


def test_the_verification_needs_something_to_compare_against() -> None:
    """Мутация: убрать условие — тест краснеет.

    Без данных судье остаётся строка «(нет данных)», и он бракует всё подряд.
    """
    source = inspect.getsource(AgentRuntime.chat)
    checks = source[source.index("self.settings.verify_answers") : source.index("_verify_response")]
    assert 'context.knowledge_hits or response.get("tool_evidence")' in checks, (
        "проверка снова запускается там, где сверять не с чем"
    )
    assert "not context.small_talk" in checks, "болтовню снова проверяют на обоснованность"


def test_both_notices_know_where_the_answer_came_from() -> None:
    """Признак «ответ из сети» доходит и до текста автопроверки, и до пометки."""
    source = inspect.getsource(AgentRuntime.chat)
    assert "from_the_web=from_the_web" in source, "автопроверка не знает про сеть"
    assert "about_his_own_papers=about_his_own_papers" in source, "пометка не знает про сеть"
    # Пометка сужена до личного вопроса с уверенным совпадением.
    narrowing = source[source.index("about_his_own_papers = ") : source.index("_grounding_warning(")]
    assert 'context.answer_mode == "personal_knowledge"' in narrowing
    assert "_archive_is_weak(context.knowledge_hits)" in narrowing
