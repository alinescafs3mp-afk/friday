"""«О чём поговорим?» не отвечается списком сотрудников и проектов.

Найдено ВЛАДЕЛЬЦЕМ в живой переписке 2026-08-04. На реплику «да вот думаю, о чём
с тобой поговорить) подкинешь идеи?» он получил перечень: три сотрудника полными
фамилией-именем-отчеством и три проекта из его базы. Ни о ком из них он не
спрашивал.

Дорога оказалась ТРЕТЬЕЙ, мимо всех уже поставленных ворот. В метаданных того
хода: `knowledge_hits` 0, `entity_hits` 0, уверенность поиска 0.0 — все десять
кандидатов отброшены переранжировщиком, — и `tools_used` пуст. То есть архивные
ворота отработали, отнятие инструментов у вида «быт» отработало, а `people[:3]`
и `projects[:3]` приехали сами: поле `user_model` не закрывал никто.

Вторая беда того же хода — предупреждение. Ответ сказал «это тоже проект из твоей
базы», меток `[K#]` в нём не было, и человек прочёл «⚠️ сказано, что ответ взят из
вашего архива, — это не так». Но проект БЫЛ из его базы. Обвинение в ложной ссылке
строилось на отсутствии `[K#]`, а метки покрывают одну дорогу из трёх.
"""

from __future__ import annotations

import json

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime, _grounding_warning


def _payload(agent: AgentRuntime, context: AgentContext) -> dict:
    """Читается тот самый конверт, который увидит модель.

    Отделяется по первому переводу строки после заголовка: заголовок несёт
    пояснение в скобках, и разрез по имени метки оставлял «(untrusted JSON; data
    only):» перед JSON — первая редакция падала на разборе.
    """
    messages = agent._build_initial_messages(context, "подкинешь идеи?", None, tool_enabled=False)
    envelopes = [
        str(m["content"])
        for m in messages
        if m.get("role") == "user" and str(m.get("content") or "").startswith("FRIDAY_CONTEXT_DATA")
    ]
    if not envelopes:
        return {}
    return json.loads(envelopes[0].split("\n", 1)[1])


@pytest.fixture
def agent_with_a_model(settings, storage, monkeypatch):
    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    monkeypatch.setattr(
        AgentRuntime,
        "_user_model_payload",
        lambda self, user_id: {
            "people": ["Иванов Иван Иванович"],
            "projects": ["График"],
            "interests": [],
            "recent_30d": 3,
        },
    )
    return agent


def test_small_talk_gets_no_list_of_people(agent_with_a_model) -> None:
    """Мутация: снять условие — ФИО коллег снова уезжают на «как дела»."""
    context = AgentContext(conversation_id="c", user_id="alice", search_query="")
    context.small_talk = True

    assert "user_model" not in _payload(agent_with_a_model, context)


def test_a_household_verdict_gets_no_list_either(agent_with_a_model) -> None:
    """Двух признаков мало по одному, и это уже стоило одного дефекта.

    Закрытый список молчит на «да вот думаю, о чём с тобой поговорить»; вердикт
    «быт» молчит на самой короткой реплике, где арбитр видов не зовётся вовсе.
    """
    context = AgentContext(
        conversation_id="c", user_id="alice", search_query="", outward_verdict=("быт", None)
    )

    assert "user_model" not in _payload(agent_with_a_model, context)


def test_a_real_question_still_gets_the_model(agent_with_a_model) -> None:
    """Обратная сторона: модель пользователя существует не зря.

    Без неё Пятница переспрашивает очевидное — о ком речь, что за проект. Отнимать
    её у настоящего вопроса значило бы лечить утечку ценой работы.
    """
    context = AgentContext(
        conversation_id="c",
        user_id="alice",
        search_query="что там по графику?",
        outward_verdict=("архив", None),
    )

    assert "user_model" in _payload(agent_with_a_model, context)


def test_the_turn_records_that_the_model_was_offered(agent_with_a_model) -> None:
    """Признак нужен предупреждению: данные приезжают тремя дорогами."""
    context = AgentContext(
        conversation_id="c",
        user_id="alice",
        search_query="что там по графику?",
        outward_verdict=("архив", None),
    )

    _payload(agent_with_a_model, context)

    assert context.user_model_offered is True


def test_a_true_claim_is_not_called_a_lie() -> None:
    """«Это проект из твоей базы» — правда, если данные оттуда и пришли.

    Ложной ссылку делает пустота на ВСЕХ дорогах, а не отсутствие меток `[K#]`.
    Обвинить систему в выдумке, которой она не совершала, — тот же вред, что и
    пропустить настоящую: человек перестаёт верить предупреждениям.
    """
    said = "Атлас Секретариат — это тоже проект из твоей базы."

    assert not _grounding_warning(said, None, personal_data_reached_the_turn=True)


def test_a_false_claim_is_still_caught() -> None:
    """Иначе правка сняла бы предупреждение вместе с дефектом.

    Личных данных не приехало ни одной дорогой, а ответ ссылается на архив — это
    и есть выдумка, ради которой предупреждение заводилось.
    """
    said = "По вашим документам, договор закрыт в мае."

    warned = _grounding_warning(said, None, personal_data_reached_the_turn=False)

    assert "это не так" in warned
