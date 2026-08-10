"""Ответ из интернета сверяется с выдачей, а не с личным архивом.

Найдено недельным прогоном 2026-08-02: почти каждый ответ, собранный из
интернета, получал предупреждение «Автопроверка нашла возможные несоответствия с
вашими данными» — новости, цены на видеокарты, военные сводки, курс валют.

Две причины, обе здесь.

Первая — судья видел ВЫРЕЗКУ в 500 знаков из выдачи на несколько тысяч и честно
не находил в ней ни одного названного факта. Ложная тревога на каждом внешнем
ответе обесценивает и настоящие: человек перестаёт читать предупреждения вовсе.

Вторая — сама формулировка. У человека нет «своих данных» о курсе доллара, и
несоответствия с ними быть не может; сверка шла с источниками, о них и надо
говорить.
"""

from __future__ import annotations

import inspect

from friday.agent_runtime import VERDICT_FAILED, AgentRuntime, _verification_caution


def test_the_judge_sees_the_search_results_not_a_snippet() -> None:
    """Мутация: вернуть 500 знаков — тест краснеет."""
    from friday.agent_runtime import _TOOL_EVIDENCE_CHARS, _secondary_tool_evidence

    assert _TOOL_EVIDENCE_CHARS >= 2000, (
        f"судья снова видит {_TOOL_EVIDENCE_CHARS} знаков выдачи — этого не хватает "
        "ни на одну новостную сводку"
    )
    verifier_source = inspect.getsource(AgentRuntime._verify_response)
    projection_source = inspect.getsource(_secondary_tool_evidence)
    assert "_secondary_tool_evidence(entry, query)" in verifier_source
    assert "max_chars=_TOOL_EVIDENCE_CHARS" in projection_source, "предел выдачи снова зашит числом"


def test_a_web_answer_is_not_accused_of_contradicting_personal_data() -> None:
    text = _verification_caution(VERDICT_FAILED, ["курс не найден в выдаче"], from_the_web=True)
    assert "вашими данными" not in text, "ответу из сети приписали спор с личным архивом"
    assert "источник" in text.casefold()


def test_an_archive_answer_keeps_the_old_wording() -> None:
    text = _verification_caution(VERDICT_FAILED, ["дата не совпадает"], from_the_web=False)
    assert "вашими данными" in text


def test_the_turn_decides_by_the_tools_it_actually_used() -> None:
    """Признак берётся из code-owned вердикта веб-выдачи."""
    source = inspect.getsource(AgentRuntime.chat)
    assert "context.web_evidence_status" in source
    assert 'web_evidence_status in {"sourced", "partial"}' in source
    assert "from_the_web=from_the_web" in source, "признак вычисляется, но не передаётся"
