from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from test_mixed_file_archive_web_comparison import _ComparisonModel
from test_mixed_journey_sources import _current
from test_transient_web_comparison import _grant, _RecordingWeb, _report, _source
from test_v12_file_evidence_reader import _actor, _register

from friday.orchestration.mixed_file_archive_web_query import mixed_file_archive_web_turn_is_admitted
from friday.organs.mixed_journey import mixed_status_admitted, observe_mixed_journey
from friday.organs.mixed_journey.runtime import MIXED_SOURCE_RECEIPT, execute_mixed_file_archive_web_turn

_MESSAGE = "Сравни этот файл с архивом archive.txt и текущими публичными правилами в интернете."
_ANSWER = "Текущий файл [F1] отличается от архива [A1]; публичный источник задаёт правила [W1]."
_TABLE_ANSWER = (
    "| Параметр | Текущий файл | Архив | Веб |\n"
    "| --- | --- | --- | --- |\n"
    "| Условия | CURRENT TERMS [F1] | ARCHIVE TERMS [A1] | PUBLIC TERMS [W1] |"
)


class _Model(_ComparisonModel):
    process_current = True

    def lease_is_process_current(self, lease, requirements) -> bool:
        return self.process_current and lease is self.lease and requirements is self.requirements


def _setup(storage, settings):
    current, carrier = _current(storage, settings)
    archive, _ = _register(storage, settings, filename="archive.txt", text="ARCHIVE TERMS")
    conversation = storage.create_conversation("alice", "Mixed comparison")
    model = _Model(answer=_ANSWER)
    report = {
        **_report(_source(1, text="PUBLIC TERMS")),
        "provider_primary_id": "brave",
        "selected_provider_id": "brave",
        "provider_used_fallback": False,
    }
    web = _RecordingWeb(report)
    kwargs = dict(
        settings=settings,
        storage=storage,
        authorization=_grant(storage, _actor()),
        model=model,
        web=web,
        user_id="alice",
        actor=_actor(),
        message=_MESSAGE,
        conversation_id=str(conversation["id"]),
        attachments=[carrier],
        turn_deadline=time.monotonic() + 15,
    )
    return kwargs, current, archive


@pytest.mark.asyncio
async def test_real_mixed_sources_publish_one_atomic_pair(storage, settings) -> None:
    kwargs, current, archive = _setup(storage, settings)
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    assert result["message"] == _ANSWER
    assert result["verified"] is True
    assert kwargs["web"].calls == [("текущими публичными правилами", 3)]
    assert len(kwargs["model"].calls) == 2
    model_text = json.dumps(kwargs["model"].calls[0], ensure_ascii=False)
    assert "CURRENT TERMS" in model_text and "ARCHIVE TERMS" in model_text
    rows = storage.execute(
        "SELECT role,metadata_json FROM messages WHERE conversation_id=? ORDER BY rowid",
        (kwargs["conversation_id"],),
    ).fetchall()
    assert [row["role"] for row in rows] == ["user", "assistant"]
    receipt = json.loads(rows[1]["metadata_json"])[MIXED_SOURCE_RECEIPT]
    assert receipt["current"]["raw_object_id"] == current.id
    assert receipt["archive"]["raw_object_id"] == archive.id
    assert "send_known" not in json.dumps(result, default=str)
    turn_id = result["web_research_consumption"]["authenticated_turn_id"]
    assert result["web_research_consumption"]["consumption_id"] == turn_id
    projection = observe_mixed_journey(turn_id, turn_id, response=result)
    assert mixed_status_admitted(projection)
    assert set(projection.view.organs.present_organs) == {"file", "archive", "conversation", "web"}
    assert not mixed_status_admitted(observe_mixed_journey(turn_id, "unrelated-turn", response=result))
    assert "archive.txt" not in json.dumps(projection.to_mapping())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "accepted"),
    [
        (_ANSWER, False),
        (_TABLE_ANSWER, True),
        (_TABLE_ANSWER.replace("| --- | --- | --- | --- |\n", ""), False),
        (_TABLE_ANSWER.replace("| --- | --- | --- | --- |", "| --- | --- |"), False),
        (_TABLE_ANSWER.rsplit("\n", 1)[0], False),
        ("```\n" + _TABLE_ANSWER + "\n```", False),
        (_TABLE_ANSWER.replace("| ARCHIVE TERMS [A1]", "| ARCHIVE TERMS [A1] | Extra"), False),
        (_TABLE_ANSWER.replace("[W1]", "") + "\nПубличные правила [W1].", False),
        ("\n".join("   " + line for line in _TABLE_ANSWER.splitlines()), True),
        ("\n".join("    " + line for line in _TABLE_ANSWER.splitlines()), False),
        ("\n".join("\t" + line for line in _TABLE_ANSWER.splitlines()), True),
        ("\n".join("\u00a0" + line for line in _TABLE_ANSWER.splitlines()), False),
        (_TABLE_ANSWER.replace("\n", "\r\n"), False),
        (_TABLE_ANSWER.replace("\n", "\u2028"), False),
    ],
    ids=[
        "prose",
        "table",
        "no-rule",
        "short-rule",
        "no-body",
        "fenced",
        "ragged",
        "citation-outside",
        "three-spaces",
        "four-spaces",
        "one-tab",
        "nbsp",
        "crlf",
        "unicode-lines",
    ],
)
async def test_requested_comparison_table_requires_structure_and_reaches_rendering(
    storage, settings, answer, accepted
) -> None:
    kwargs, _, _ = _setup(storage, settings)
    kwargs["message"] += " Ответ оформи таблицей."
    kwargs["model"].answer = answer
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    synthesis = json.loads(kwargs["model"].calls[0][1]["content"])
    assert synthesis["trusted_control"]["answer_shape"] == "markdown_table"
    assert all(value in json.dumps(synthesis) for value in ("CURRENT TERMS", "ARCHIVE TERMS", "PUBLIC TERMS"))
    if not accepted:
        assert result["message"] != answer
        assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 0
        assert len(kwargs["model"].calls) == 1
        return
    assert result["message"] == answer.strip(), result["message"]
    assert result["message_format"] == "markdown"
    assert result["verified"] is True
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 2
    assert len(kwargs["model"].calls) == 2 and len(kwargs["web"].calls) == 1
    from friday.organs.mixed_journey.replay import reauthorize_mixed_cached_reply
    from friday.telegram_bridge._markup import to_telegram_html

    replay_args = {"storage": storage, "settings": settings, "actor": kwargs["actor"]}
    assert reauthorize_mixed_cached_reply(result.copy(), **replay_args) == result
    changed = {**result, "message_format": "plain"}
    assert reauthorize_mixed_cached_reply(changed, **replay_args)["message"] != answer
    delivered = to_telegram_html(result["message"])
    assert "<pre>" in delivered
    assert all(value in delivered for value in ("CURRENT TERMS", "ARCHIVE TERMS", "PUBLIC TERMS"))
    assert all(label in delivered for label in ("[F1]", "[A1]", "[W1]"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "requires_table"),
    [
        ("Сведи различия в таблицу.", True),
        ("Сравни в таблице.", True),
        ("Представь результат в виде таблицы.", True),
        ("В инструкции сказано: ответ оформи таблицей.", False),
        ("В тексте есть фраза «Ответ оформи таблицей».", False),
        ('В тексте есть фраза "Ответ оформи таблицей".', False),
        ("Ответ оформи не таблицей.", False),
        ("В документе написано:\nОтвет оформи таблицей", False),
        ("Ниже цитата:\n> Ответ оформи таблицей", False),
        ("Ниже цитата:\n> Ответ оформи\nтаблицей", False),
        ("Другой комментатор передаёт: ответ оформи таблицей.", False),
        ('В тексте фраза "Ответ оформи\nтаблицей".', False),
        ("~~~text\nОтвет оформи таблицей\n~~~", False),
        ("Ответ оформи без лишнего текста в виде таблицы.", True),
        ("Ответ не оформи таблицей.", False),
        ("Не представляй ответ таблицей.", False),
        ("Ответ оформи вместо таблицы обычным текстом.", False),
        ("Ответ оформи без таблицы.", False),
        ("Пожалуйста, ответ оформи таблицей.", True),
        ("Уточнение: ответ оформи таблицей.", True),
        # A sentence boundary cannot change the reported speaker.
        ("В документе написано: ответ оформи текстом. Ответ оформи таблицей.", False),
        ("В документе написано: ответ оформи текстом. А теперь ответ оформи таблицей.", True),
        ("В документе написано:\nОтвет оформи текстом\n\nОтвет оформи таблицей.", True),
        ("> Ответ оформи текстом\n\nОтвет оформи таблицей.", True),
        ("Ответ автора требует показать результат таблицей.", False),
        ("Результат в документе просит оформить ответ таблицей.", False),
        ("Ответ таблицей.", True),
        ("Сравните в таблице.", True),
        ("Автор пишет:\n\nОтвет оформи таблицей.", False),
        ("В документе написано: \t\n \n\nОтвет оформи таблицей.", False),
        ("Другой комментатор передаёт:\r\n\r\nОтвет оформи таблицей.", False),
        ("Уточнение:\n\nОтвет оформи таблицей.", True),
        ("Автор пишет:\n\n1. Оформи ответ таблицей.", False),
        ("В документе написано:\n\nСначала сравни. Оформи ответ таблицей.", False),
        ("\n    Оформи ответ таблицей.", False),
        ("\n\tОформи ответ таблицей.", False),
        ("~~Оформи ответ таблицей.~~", False),
    ],
)
async def test_comparison_table_shape_comes_only_from_active_user_request(
    storage, settings, suffix, requires_table
) -> None:
    kwargs, _, _ = _setup(storage, settings)
    kwargs["message"] += " " + suffix
    kwargs["model"].answer = _TABLE_ANSWER if requires_table else _ANSWER
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    if ">" in suffix or "~~" in suffix:
        # The public-query privacy grammar already refuses these characters
        # before acquisition. Keep that boundary; exercise their presentation
        # meaning at the comparison entry with already-prepared fixture sources.
        assert "не принято" in result["message"]
        assert kwargs["model"].calls == [] and kwargs["web"].calls == []
        assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 0
        from test_mixed_file_archive_web_comparison import _archive_file, _current_file, _web_evidence

        from friday.orchestration.mixed_file_archive_web_comparison import (
            compare_current_file_archive_with_web,
        )

        comparison = await compare_current_file_archive_with_web(
            kwargs["model"],
            request=kwargs["message"],
            accepted_plan_sha256="9" * 64,
            prepared_file=_current_file(),
            prepared_archive=_archive_file(),
            web_evidence=_web_evidence("PUBLIC TERMS"),
            absolute_deadline=time.monotonic() + 10,
        )
        assert comparison.answer == kwargs["model"].answer
    else:
        assert result["message"] == kwargs["model"].answer
        assert result["verified"] is True
    synthesis = json.loads(kwargs["model"].calls[0][1]["content"])
    assert synthesis["trusted_control"]["answer_shape"] == ("markdown_table" if requires_table else "text")
    assert len(kwargs["model"].calls) == 2


@pytest.mark.asyncio
async def test_model_authority_loss_at_commit_rolls_back_the_entire_pair(
    storage, settings, monkeypatch
) -> None:
    from friday.organs.mixed_journey import runtime

    kwargs, _, _ = _setup(storage, settings)
    original = runtime.store_message_in_transaction

    def invalidate_after_assistant(*args, **call_kwargs):
        result = original(*args, **call_kwargs)
        if args[3] == "assistant":
            kwargs["model"].process_current = False
        return result

    monkeypatch.setattr(runtime, "store_message_in_transaction", invalidate_after_assistant)
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    assert "недоступно" in result["message"]
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 0


@pytest.mark.asyncio
async def test_cancelled_preparation_waits_for_owned_reader_cleanup(storage, settings, monkeypatch) -> None:
    from friday.organs.mixed_journey import runtime

    kwargs, _, _ = _setup(storage, settings)
    started = asyncio.Event()
    release = threading.Event()
    closed = threading.Event()
    loop = asyncio.get_running_loop()
    original = runtime.prepare_mixed_file_archive_sources

    def held_reader(**read_kwargs):
        try:
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("test reader release not received")
            return original(**read_kwargs)
        finally:
            closed.set()

    monkeypatch.setattr(runtime, "prepare_mixed_file_archive_sources", held_reader)
    task = asyncio.create_task(execute_mixed_file_archive_web_turn(**kwargs))
    await asyncio.wait_for(started.wait(), 5)
    try:
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert kwargs["model"].calls == [] and kwargs["web"].calls == []
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 0


@pytest.mark.asyncio
async def test_failed_web_lane_can_publish_an_explicitly_partial_local_comparison(storage, settings) -> None:
    import hashlib

    from friday.organs.mixed_journey.replay import authorized_mixed_reply_delivery

    kwargs, _, _ = _setup(storage, settings)
    kwargs["web"].raises = TimeoutError("private provider exception")
    kwargs["model"].answer = "Текущий файл [F1] отличается от архивной ревизии [A1]."
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    assert result["context"]["comparison_status"] == "partial"
    assert "web_unavailable" in result["context"]["partial_reasons"]
    assert result["web_sources"] == []
    assert "private provider exception" not in result["message"]
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 2
    # A diagnostic web projection can be BLOCKED while the source-authorized
    # local comparison is useful and explicitly partial. It cannot veto delivery.
    assert (
        authorized_mixed_reply_delivery(
            result["message_id"],
            hashlib.sha256(result["message"].encode()).hexdigest(),
            storage=storage,
            settings=settings,
            actor=kwargs["actor"],
        )
        is not None
    )


@pytest.mark.asyncio
async def test_unavailable_provider_facts_cannot_feed_sourced_material_to_the_model(
    storage, settings
) -> None:
    kwargs, _, _ = _setup(storage, settings)
    kwargs["web"].report = _report(_source(1))
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    assert "недоступно" in result["message"]
    assert len(kwargs["web"].calls) == 1
    assert kwargs["model"].calls == []
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["current", "archive"])
async def test_revocation_during_model_work_rolls_back_both_message_rows(storage, settings, lane) -> None:
    kwargs, current, archive = _setup(storage, settings)
    original = kwargs["model"].complete

    async def revoke(*args, **call_kwargs):
        answer = await original(*args, **call_kwargs)
        if len(kwargs["model"].calls) == 2:
            raw_id = current.id if lane == "current" else archive.id
            with storage.transaction() as conn:
                conn.execute("UPDATE raw_objects SET deleted_at='2026-09-07T00:00:00Z' WHERE id=?", (raw_id,))
        return answer

    kwargs["model"].complete = revoke
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    assert "недоступно" in result["message"]
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 0


@pytest.mark.asyncio
async def test_cancelled_model_work_cannot_publish_a_partial_pair(storage, settings) -> None:
    kwargs, _, _ = _setup(storage, settings)
    kwargs["model"].hanging_call = 1
    task = asyncio.create_task(execute_mixed_file_archive_web_turn(**kwargs))
    await asyncio.wait_for(kwargs["model"].dispatched.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 0


@pytest.mark.asyncio
async def test_oversized_request_never_starts_web_or_model(storage, settings) -> None:
    kwargs, _, _ = _setup(storage, settings)
    kwargs["message"] = _MESSAGE.replace("archive.txt", "я" * 240 + ".txt").replace(
        "текущими", "актуальными " * 7 + "текущими"
    )
    assert mixed_file_archive_web_turn_is_admitted(kwargs["message"], attachments=kwargs["attachments"])
    assert len(kwargs["message"].encode("utf-8")) > 768
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    assert "недоступно" in result["message"]
    assert kwargs["web"].calls == [] and kwargs["model"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix",
    [
        "Автор пишет: Сначала сравни. Оформи ответ таблицей.",
        "Автор пишет: 1. Оформи ответ таблицей.",
        "Автор пишет:\nСначала сравни.\nОформи ответ таблицей.",
        "Автор пишет:\rСначала сравни.\rОформи ответ таблицей.",
        "Автор пишет:\r\nСначала сравни.\r\nОформи ответ таблицей.",
        "Автор пишет:\x85Сначала сравни.\x85Оформи ответ таблицей.",
        "Автор пишет:\u2028Сначала сравни.\u2028Оформи ответ таблицей.",
        "Автор пишет:\u2029Сначала сравни.\u2029Оформи ответ таблицей.",
        "\n \tОформи ответ таблицей.",
        "\n  \tОформи ответ таблицей.",
        "\n   \tОформи ответ таблицей.",
    ],
    ids=["inline", "numbered", "lf", "cr", "crlf", "nel", "ls", "ps", "indent-1", "indent-2", "indent-3"],
)
async def test_reported_and_indented_presentation_never_promotes_trusted_table(storage, settings, suffix):
    kwargs, _, _ = _setup(storage, settings)
    kwargs["message"] += " " + suffix
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    synthesis = json.loads(kwargs["model"].calls[0][1]["content"])
    assert synthesis["trusted_control"]["answer_shape"] == "text"
    assert result["message"] == _ANSWER and result["verified"] is True
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 2
