from __future__ import annotations

import pytest

from friday.retrieval.archive_search_message_adapter import (
    _bounded_message_excerpt,
    _excerpt,
)
from friday.retrieval.contracts import MessageRole
from tests.test_archive_search_message_storage import _current, _database, _insert


def test_long_adjacent_context_keeps_the_short_matched_message_visible() -> None:
    marker = "needle exact matched anchor"
    with _database() as conn:
        _insert(
            conn,
            10,
            role="assistant",
            content="long context before " + "alpha " * 260,
            created_at="2026-08-23T08:10:00+00:00",
        )
        _insert(
            conn,
            20,
            content=marker,
            created_at="2026-08-23T08:11:00+00:00",
        )
        _insert(
            conn,
            30,
            role="assistant",
            content="long context after " + "omega " * 260,
            created_at="2026-08-23T08:12:00+00:00",
        )

        page = _current(conn, context_before=1, context_after=1)

        assert page is not None and page.returned == 1
        excerpt = _excerpt(page.hits[0])
        assert len(excerpt) <= 1_900
        assert marker in excerpt


@pytest.mark.parametrize("rendered_length", (1_896, 1_897, 1_900))
def test_matched_row_is_complete_up_to_the_exact_excerpt_bound(rendered_length: int) -> None:
    prefix = "Пользователь: "
    matched = prefix + "x" * (rendered_length - len(prefix))

    excerpt = _bounded_message_excerpt(
        (
            (MessageRole.ASSISTANT, "context before"),
            (MessageRole.USER, matched.removeprefix(prefix)),
            (MessageRole.ASSISTANT, "context after"),
        ),
        matched_index=1,
    )

    assert len(excerpt) <= 1_900
    assert matched in excerpt


def test_oversized_matched_row_remains_bounded() -> None:
    excerpt = _bounded_message_excerpt(
        ((MessageRole.USER, "x" * 2_000),),
        matched_index=0,
    )

    assert len(excerpt) <= 1_900
    assert " … " in excerpt
