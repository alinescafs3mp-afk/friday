"""«Собери документы за 26 июля» — просьба, и понимать её надо так же, как вопрос.

Найдено живым замером 2026-08-03, уже ПОСЛЕ того, как сборка архива заработала:
архив пришёл, но собрала его сама модель, а ветка понимания молчала. Причина —
условие `_might_be_a_question`: у просьбы нет ни вопросительного знака, ни
вопросительного слова, и арбитра не спрашивали вовсе.

То есть весь разбор намерения — «человек», «файл», дни — был мёртв для
повелительных фраз. А владелец говорит помощнику именно так: «собери»,
«выгрузи», «оформи», «пришли файлом».

Дописать глаголы в шаблон значило бы пойти ровно той дорогой, от которой он
просил уйти («не затыкай всё эвристиками и шаблонами»). Условие снято целиком:
вердикт считается параллельно поиску и своих секунд к ответу не добавляет.
"""

from __future__ import annotations

import inspect

import pytest

from friday.agent_runtime import _QUESTION_LENGTH_LIMIT, AgentRuntime, _might_be_a_question


@pytest.mark.parametrize(
    "message",
    [
        "Собери документы, которые приходили 26 июля",
        "Выгрузи файлы за 29 июля",
        "Скинь архивом всё за вчера",
        "Оформи это в word",
    ],
)
def test_these_requests_are_not_questions(message: str) -> None:
    """Закрепляется измеренное, а не желаемое: шаблон их действительно не видит."""
    assert not _might_be_a_question(message), message


def test_understanding_is_no_longer_gated_on_the_question_shape() -> None:
    """Мутация: вернуть `_might_be_a_question` в условие — тест краснеет."""
    source = inspect.getsource(AgentRuntime._prepare_context)
    start = source.index("arbiter: asyncio.Task")
    guard = source[start : source.index("asyncio.create_task", start)]
    # Комментарии отбрасываются: там имя условия названо намеренно — объяснить,
    # почему его убрали. Проверяется КОД.
    code = "\n".join(
        line for line in guard.splitlines() if not line.strip().startswith("#")
    )
    assert "_might_be_a_question" not in code, "просьбы снова остались без понимания"
    assert "worth_understanding" in code


def test_a_pasted_document_still_does_not_go_to_the_model() -> None:
    """Ограничение длины осталось: присланный текст — не просьба.

    Снять условие целиком, вместе с длиной, значило бы гнать каждый пересланный
    документ через лишний вызов модели.
    """
    source = inspect.getsource(AgentRuntime._prepare_context)
    at = source.index("worth_understanding = ")
    assert "_QUESTION_LENGTH_LIMIT" in source[at : at + 300], "длина перестала ограничиваться"
    assert _QUESTION_LENGTH_LIMIT > 0


def test_a_tool_that_read_the_storage_counts_as_grounding() -> None:
    """«Ответ не опирается ни на одну запись вашей базы» — под ответом, который опирался.

    Замерено на живом экземпляре: «Собери документы, которые приходили 26 июля»
    отработало верно, пришёл архив на 66 файлов, и под ним встало это
    предупреждение. `collect_files` прочитал ровно базу — просто ссылок `[K#]` в
    таком ответе не бывает, потому что данные пришли не поиском.

    Владелец просил убрать это предупреждение дважды; оно осталось ровно для
    случая «модель сочиняет про лежащие перед ней документы», а этот случай —
    не тот.
    """
    from friday.agent_runtime import _TOOLS_THAT_READ_THE_ARCHIVE

    source = inspect.getsource(AgentRuntime.chat)
    at = source.index("about_his_own_papers = (")
    # Ровно САМО условие, от присваивания до его закрывающей скобки. Первая
    # редакция брала окно в семьсот знаков вокруг — и мутация «убрать
    # `not answered_from_storage` из условия» её НЕ уронила: имя оставалось
    # видно в строке выше, где переменная вычисляется.
    condition = source[at : source.index("\n        )", at)]
    assert "not answered_from_storage" in condition, "инструмент снова не считается опорой"
    assert "collect_files" in _TOOLS_THAT_READ_THE_ARCHIVE
    assert "user_activity" in _TOOLS_THAT_READ_THE_ARCHIVE
    # Веб-инструменты сюда не входят: ответ из интернета на базу и правда не
    # опирается, и для него есть своя, отдельная оговорка.
    assert not {"web_search", "web_research", "web_fetch"} & set(_TOOLS_THAT_READ_THE_ARCHIVE)
    # И `memory_search` тоже не входит — найдено ревью этой самой правки.
    #
    # Он и ЕСТЬ поиск по архиву: его результат приходит документами, ссылка
    # `[K#]` на них возможна. В списке он гасил бы предупреждение ровно в том
    # случае, ради которого оно оставлено: модель сочинила про документы,
    # которые лежат перед ней, не сославшись ни на один.
    assert "memory_search" not in _TOOLS_THAT_READ_THE_ARCHIVE
