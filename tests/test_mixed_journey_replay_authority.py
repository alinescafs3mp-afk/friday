from __future__ import annotations

import json

import pytest
from test_mixed_journey_runtime import _ANSWER, _setup

from friday.organs.mixed_journey.replay import reauthorize_mixed_cached_reply
from friday.organs.mixed_journey.runtime import execute_mixed_file_archive_web_turn


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_lane", [None, "current", "archive"])
@pytest.mark.parametrize("cache_drift", [None, "missing_context", "changed_mode", "missing_conversation"])
async def test_serialized_cached_reply_reauthorizes_both_sources_without_reexecution(
    storage,
    settings,
    revoked_lane,
    cache_drift,
) -> None:
    kwargs, current, archive = _setup(storage, settings)
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    assert result["message"] == _ANSWER
    cached = json.loads(json.dumps(result))
    if cache_drift in {"missing_context", "missing_conversation"}:
        cached.pop("context")
        if cache_drift == "missing_conversation":
            cached.pop("conversation_id")
    elif cache_drift == "changed_mode":
        cached["context"]["answer_mode"] = "ordinary_dialogue"
    if revoked_lane:
        raw_id = current.id if revoked_lane == "current" else archive.id
        with storage.transaction() as conn:
            conn.execute("UPDATE raw_objects SET deleted_at='2026-09-07T00:00:00Z' WHERE id=?", (raw_id,))
    replay = reauthorize_mixed_cached_reply(cached, storage=storage, settings=settings, actor=kwargs["actor"])
    if revoked_lane or cache_drift:
        assert _ANSWER not in json.dumps(replay, ensure_ascii=False)
        assert replay["files"] == [] and replay["citations"] == []
    else:
        assert replay == cached
    assert len(kwargs["model"].calls) == 2 and len(kwargs["web"].calls) == 1
    assert storage.count_messages(kwargs["conversation_id"], user_id="alice") == 2


@pytest.mark.asyncio
async def test_cached_text_cannot_borrow_an_unrelated_durable_message(storage, settings) -> None:
    kwargs, _, _ = _setup(storage, settings)
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    cached = {**result, "message": "Injected cached content"}
    replay = reauthorize_mixed_cached_reply(cached, storage=storage, settings=settings, actor=kwargs["actor"])
    assert "Injected" not in json.dumps(replay)


@pytest.mark.asyncio
async def test_disguised_cache_cannot_borrow_an_ordinary_owned_assistant(storage, settings) -> None:
    kwargs, _, _ = _setup(storage, settings)
    result = await execute_mixed_file_archive_web_turn(**kwargs)
    ordinary = storage.store_message(kwargs["conversation_id"], "alice", "assistant", "Ordinary answer")
    cached = {**result, "message_id": ordinary["id"], "context": {"answer_mode": "ordinary_dialogue"}}
    replay = reauthorize_mixed_cached_reply(cached, storage=storage, settings=settings, actor=kwargs["actor"])
    assert _ANSWER not in json.dumps(replay, ensure_ascii=False)
    normal = {**cached, "message": ordinary["content"]}
    assert (
        reauthorize_mixed_cached_reply(normal, storage=storage, settings=settings, actor=kwargs["actor"])
        is normal
    )
