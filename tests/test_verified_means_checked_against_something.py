"""«Проверено» под ответом, который ни на что не опирался, — неправда.

Замерено на живой базе 2026-08-03: из 617 ответов с известной обоснованностью
374 не опирались НИ НА ОДНУ запись архива, и 80 из них несли пометку «проверено».
Один — со счётом 1.0 при `answer_grounded=False` и предупреждении «ответ не
опирается ни на одну запись вашей базы». Два прибора в одном ходе говорили
противоположное, и никто этого не замечал.

Механизм. Судья сверяет ответ с ПРОЦИТИРОВАННЫМИ объектами, а при нуле ссылок
берёт запасной путь — верхние пять найденных. То есть оценивает ответ против
документов, которых модель не использовала: речь о разном, противоречий он не
находит и честно ставит «прошло».

Запасной путь сам по себе разумен — модель могла опереться на документ и забыть
метку. Но тогда и вывод должен быть скромнее: «расхождений не видно», а не
«проверено». Отличить одно от другого нечем, цитат нет.

Поэтому вердикт понижается до «не проверялось». Ответ при этом не отменяется и не
чинится: меняется только то, что система о нём УТВЕРЖДАЕТ. Отдельного
предупреждения человеку не добавляется — ложную ссылку на архив ловит
`_grounding_warning`, а лишняя пометка под каждым ответом о внешнем мире
обесценила бы те, что по делу (владелец просил убрать такую дважды).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from friday.agent_runtime import VERDICT_PASSED, VERDICT_SKIPPED, AgentContext, AgentRuntime
from friday.permissions import ActorContext


class _Model:
    """Отвечает всегда одинаково; судья при этом говорит «прошло»."""

    enabled = True
    total_budget_sec = 5.0

    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        asked = " ".join(str(m.get("content") or "") for m in messages)
        if "РАЗГОВОР или ЗАПРОС" in asked:
            return {"content": "ЗАПРОС"}
        if "Проверь ответ" in asked:
            return {"content": '{"ok": true, "score": 1.0, "issues": []}'}
        return {"content": self.answer}


#: Ответ длиннее порога `verify_min_answer_chars` (300): короткий до судьи
#: не доходит вовсе, и первая редакция теста проверяла пустоту.
LONG_ANSWER = (
    "Поверка средств измерений проводится по утверждённому графику. Сроки зависят "
    "от типа прибора и условий эксплуатации; для большинства рабочих эталонов "
    "межповерочный интервал составляет один год, для отдельных категорий он "
    "продлевается до двух лет по результатам предыдущей поверки. Ответственный "
    "подаёт заявку заранее, чтобы прибор не выпал из эксплуатации. "
)


def _hit(index: int, title: str) -> dict:
    return {
        "id": f"ko_{index}",
        "title": title,
        "content": "порядок и сроки проведения поверки средств измерений",
        "summary": "порядок и сроки",
        "_score": 0.8,
        "_rerank_score": 0.8,
    }


class _Searcher:
    async def search(self, user_id, query, **kwargs):  # noqa: ANN001, ARG002
        return {
            "results": [_hit(i, f"Документ {i}") for i in range(3)],
            "entity_matches": [],
            "strategy": "hybrid",
            "trace": [],
        }


def _turn(settings, storage, answer: str) -> dict:
    storage.ensure_user("alice", preset_key="owner")
    # Verification is intentionally opt-in in production.  This module tests
    # the verifier's own semantics, so enable it explicitly.
    agent = AgentRuntime(replace(settings, verify_answers=True), storage)
    agent.llm = _Model(answer)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    return asyncio.run(
        agent.chat(
            "alice",
            "что там по поверке приборов",
            actor=actor,
            hybrid_searcher=_Searcher(),
            enable_tools=False,
        )
    )


def test_an_answer_resting_on_nothing_is_not_called_verified(settings, storage) -> None:
    """Главный случай: 80 таких ответов на живой базе.

    Мутация: убрать понижение вердикта — тест краснеет.
    """
    reply = _turn(settings, storage, LONG_ANSWER)

    assert reply.get("verification_status") != VERDICT_PASSED, (
        "ответ, не сославшийся ни на что, объявлен проверенным"
    )
    assert reply.get("verified") is not True


def test_an_answer_that_cites_its_source_keeps_the_verdict(settings, storage) -> None:
    """Обратная сторона. Правка, гасящая проверку целиком, обесценивает её.

    Ссылка [K1] означает, что судье БЫЛО с чем сверять, и его «прошло» — про
    настоящую опору, а не про случайно подвернувшиеся документы.
    """
    reply = _turn(settings, storage, LONG_ANSWER + "Основание [K1].")

    assert reply.get("verification_status") == VERDICT_PASSED, "проверка обесценена там, где работала"
    assert reply.get("verified") is True


def test_a_failed_verdict_is_untouched(settings, storage) -> None:
    """Понижается только «прошло». «Забраковано» трогать нельзя ни при каких условиях."""
    context = AgentContext(conversation_id="c", user_id="alice")
    assert VERDICT_SKIPPED != VERDICT_PASSED
    # Прямая проверка ветки: понижение обусловлено ИМЕННО статусом «прошло».
    import inspect

    source = inspect.getsource(AgentRuntime.chat)
    marker = source.index("«Проверено» под ответом")
    guard = source[marker : marker + 1800]
    assert "verification_status == VERDICT_PASSED" in guard, "понижение сорвётся и на забракованном"
    assert context.user_id == "alice"


def test_a_tool_grounded_answer_keeps_the_verdict(settings, storage) -> None:
    """Ответ из интернета опирается на выдачу, а не на архив — ссылок [K#] там нет.

    Замерено ранее на этом проекте: почти каждый ответ из сети получал ложное
    «факты не подтверждены», пока судье не начали давать саму выдачу. Понижать
    такой вердикт значило бы повторить ту же ошибку с другой стороны.
    """
    import inspect

    source = inspect.getsource(AgentRuntime.chat)
    marker = source.index("«Проверено» под ответом")
    guard = source[marker : marker + 1800]
    assert 'not response.get("tool_evidence")' in guard, "ответ из сети тоже потеряет вердикт"


@pytest.mark.parametrize(
    "answer",
    [
        LONG_ANSWER + "Основание [K1].",
        LONG_ANSWER + "См. [K2] и [K3].",
    ],
)
def test_any_live_citation_is_enough(settings, storage, answer: str) -> None:
    """Опора — это ссылка на объект из контекста, любая."""
    reply = _turn(settings, storage, answer)

    assert reply.get("verification_status") == VERDICT_PASSED
