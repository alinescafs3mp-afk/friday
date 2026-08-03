"""Голос несёт ОТВЕТ, а список замечаний остаётся глазам.

Найдено владельцем 2026-08-03 на своём голосовом про мировые новости: «опять
этот блок про факты появился, ещё и озвучила только эту часть голосовым».

Механизм точный и целиком арифметический:
  * оговорка автопроверки разворачивается в перечень замечаний длиной до 600
    знаков (`_CAUTION_DETAIL_LIMIT`);
  * она ставится ПЕРЕД ответом — намеренно, чтобы человек услышал предупреждение
    до фактов, а не после;
  * синтез режет вход на 2000 знаках (`tts_max_chars`).

Ответ на 1406 знаков плюс оговорка на 600 — это 2006. В голос попадала оговорка
и огрызок ответа. Замерено на живых ответах того дня: 4, 6 и 8 замечаний.

Перечень нужен ГЛАЗАМ: его читают, сверяя с текстом. На слух восемь пунктов
подряд не запомнить, а место они съедают у того единственного, ради чего голос и
просили. Поэтому вслух — только первая строка оговорки.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from friday.agent_runtime import _CAUTION_DETAIL_LIMIT, AgentRuntime


class _Kernel:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
        self.spoken.append(str(params.get("text") or ""))

        class _Result:
            success = True
            attachment = {"kind": "voice", "content_base64": "AA=="}

        return _Result()


def _speak(content: str, *, caution: str = "", warning: str = "") -> tuple[str, AgentRuntime]:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    bound = AgentRuntime._voice_of_the_final_answer.__get__(runtime, AgentRuntime)
    asyncio.run(
        bound(None, content, warning=warning, caution=caution, actor=None, asked_for_voice=True)
    )
    return runtime.kernel.spoken[0], runtime


LONG_CAUTION = "⚠️ Автопроверка нашла возможные несоответствия.\n" + "\n".join(
    f"• замечание номер {i} про несовпадение факта в ответе" for i in range(1, 9)
)


def test_the_answer_survives_a_long_caution() -> None:
    """Мутация: вернуть оговорку целиком — ответ снова вытесняется за предел."""
    answer = "Ответ по существу. " * 70  # ~1400 знаков, как в живом случае
    spoken, _ = _speak(answer, caution=LONG_CAUTION)

    assert len(spoken) < 2000, "озвучиваемый текст снова упирается в предел синтеза"
    assert spoken.rstrip().endswith("Ответ по существу."), "ответ обрезан — слышно не его"


def test_the_warning_is_still_heard_first() -> None:
    """Предупреждение по-прежнему звучит ДО фактов — иначе человек уже поверил."""
    spoken, _ = _speak("Сам ответ.", caution=LONG_CAUTION)
    assert spoken.startswith("⚠️ Автопроверка нашла возможные несоответствия.")


def test_only_the_first_line_of_the_caution_is_spoken() -> None:
    """Восемь пунктов подряд на слух не запомнить, а место они съедают."""
    spoken, _ = _speak("Сам ответ.", caution=LONG_CAUTION)
    assert "замечание номер 1" not in spoken, "перечень замечаний снова уходит в голос"
    assert "Сам ответ." in spoken


def test_a_short_caution_is_unchanged() -> None:
    """Однострочная оговорка и раньше не мешала — ничего не должно измениться."""
    spoken, _ = _speak("Сам ответ.", warning="⚠️ Проверьте ключевые факты.")
    assert spoken == "⚠️ Проверьте ключевые факты.\n\nСам ответ."


def test_without_a_caution_only_the_answer_is_spoken() -> None:
    spoken, _ = _speak("Просто ответ.")
    assert spoken == "Просто ответ."


def test_the_detail_limit_is_still_generous_for_the_eyes() -> None:
    """Текст глазам не урезан: правка касается только произносимого."""
    assert _CAUTION_DETAIL_LIMIT == 600


def test_the_trim_happens_where_the_voice_is_made() -> None:
    """Проверяется подключённое: сокращение стоит в самой озвучке."""
    source = inspect.getsource(AgentRuntime._voice_of_the_final_answer)
    assert 'lead.split("\\n", 1)[0]' in source, "в голос снова уходит вся оговорка"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_an_empty_answer_is_not_voiced(blank: str) -> None:
    """Озвучивать нечего — и клипа быть не должно."""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    bound = AgentRuntime._voice_of_the_final_answer.__get__(runtime, AgentRuntime)
    asyncio.run(bound(None, blank, warning="", caution="", actor=None, asked_for_voice=True))
    assert runtime.kernel.spoken == []
