"""Кнопки под ответом работают у КАЖДОГО, а не только у хозяина архива.

Живая жалоба владельца 2026-08-02: новый участник (полные права, активен) жал
👍/👎 под сообщением и получал «Действие недоступно». За этим стоял ответ бэкенда
404 «Assistant answer not found».

Причина та же, что уже ловилась в поиске по переписке, в напоминаниях и в заявках
на подтверждение: оценка искала сообщение по `actor.user_id`, а в общем архиве
(`FRIDAY_SHARED_ARCHIVE`) он у всех один, тогда как переписка сохраняется под
самим человеком. Общими владелец делал документы, не разговоры.

Здесь же закрыты три соседних места с той же болезнью: повтор ответа, состояния
напоминаний и «сохранить ответ как знание».
"""

from __future__ import annotations

import inspect

from friday.server import create_app


def _handlers_source() -> str:
    import friday.server as server_module

    return inspect.getsource(server_module)


def test_feedback_looks_up_the_answer_by_the_person() -> None:
    """Мутация: вернуть `actor.user_id` — участник снова получит «Действие недоступно»."""
    source = _handlers_source()
    block = source[source.index("record_feedback(") : source.index("record_feedback(") + 700]
    assert "actor.own_id" in block, "оценка снова ищет сообщение под общим арендатором"


def test_regenerate_finds_the_conversation_by_the_person() -> None:
    source = _handlers_source()
    assert "get_conversation(conversation_id, actor.own_id)" in source


def test_saving_an_answer_finds_the_message_by_the_person() -> None:
    source = _handlers_source()
    assert "get_message(message_id, actor.own_id)" in source


def test_reminder_states_belong_to_the_person() -> None:
    source = _handlers_source()
    assert "reminder_states(actor.own_id" in source


def test_upcoming_reminders_are_filtered_by_author() -> None:
    """Событие под общим арендатором ещё не значит «моё напоминание»."""
    from friday.storage._graph import _bounded_visible_timeline_event_rows

    source = inspect.getsource(_bounded_visible_timeline_event_rows)
    # Фильтр переехал из HTTP-обработчика в общий timeline reader: source один
    # недостаточен, durable owner marker обязан назвать того же человека.
    assert "private_entity_owners" in source, "чужие просьбы снова попадают в мои планы"
    assert "private_owner.person_id=?" in source
    assert "reminder_source" in source


def test_the_app_still_builds(settings) -> None:
    """Правки не должны ломать сборку приложения."""
    assert create_app(settings) is not None
