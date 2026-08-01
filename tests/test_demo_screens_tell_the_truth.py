"""Две находки состязательного ревью перед показом.

Обе про одно: человек читает то, что написано, и делает вывод. Число, которое не
может быть иным, и служебный маркер в переписке — это не косметика, а неверные
сведения о его собственных данных.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime.llm import strip_service_markup


def test_a_zero_that_cannot_be_anything_else_is_not_shown():
    """Мутация: вернуть безусловную строку «Связей: N подтверждено» — тест краснеет.

    Замерено на боевой базе: `relations` = 0 при 4609 сущностях и 32 219 связях
    знание↔сущность. То есть строка «Связей: 0 подтверждено» появлялась у КАЖДОГО
    объекта и означала не «у этого связей нет», а «связей нет ни у кого». Рядом со
    строкой «Связанных документов: 46» она читается как «граф пустой».

    То же правило уже применено в `_format_status` и `_describe_merge_entity` —
    до карточки объекта оно не доехало.
    """
    import inspect

    from friday.telegram_bridge._views import ViewsMixin

    source = inspect.getsource(ViewsMixin._send_entity_profile)  # noqa: SLF001
    marker = source.index('f"Связей: {len(relations)} подтверждено"')
    head = source[:marker]
    assert "if relations:" in head.splitlines()[-4:][0] or any(
        "if relations:" in line for line in head.splitlines()[-6:]
    ), "строка про связи печатается безусловно"


def test_a_pending_count_is_still_worth_saying():
    """Ноль подтверждённых при непустой очереди — это осмысленная строка."""
    import inspect

    from friday.telegram_bridge._views import ViewsMixin

    source = inspect.getsource(ViewsMixin._send_entity_profile)  # noqa: SLF001
    assert "Связей на проверке" in source, (
        "при нулe подтверждённых очередь на проверку тоже замолчала"
    )


@pytest.mark.parametrize(
    "stored,expected",
    [
        ('Вот ответ. <tool_call>{"name":"list_tags"}</tool_call>', "Вот ответ."),
        ("<think>рассуждение вслух</think>Ответ человеку", "Ответ человеку"),
        ("<TOOL_CALL>{}</TOOL_CALL>", ""),
        ("обычный ответ без разметки", "обычный ответ без разметки"),
        # Слово в прозе — не разметка.
        ("могу сделать tool_call, если нужно", "могу сделать tool_call, если нужно"),
    ],
)
def test_service_markup_is_stripped_when_history_is_shown(stored, expected):
    """Мутация: убрать `strip_service_markup` из `/history` — тест краснеет.

    В боевой базе 21 сообщение содержит `<tool_call>` или `</think>`: они
    записаны ДО появления очистки на выходе модели, а сообщения чата неудаляемы.
    Значит чистить надо на выводе, каждый раз, а не один раз при записи.
    """
    assert strip_service_markup(stored) == expected


def test_history_output_goes_through_the_stripper():
    """Проверяется не помощник, а то, что его зовут именно в `/history`."""
    import inspect

    from friday.telegram_bridge._views import ViewsMixin

    source = inspect.getsource(ViewsMixin._send_history)  # noqa: SLF001
    assert "strip_service_markup(" in source, (
        "история печатает сохранённый текст дословно, вместе со служебными маркерами"
    )
