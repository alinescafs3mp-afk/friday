"""Bounded Telegram progress for opaque, potentially long chat turns."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from typing import Any, cast

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig
from friday.telegram_bridge import _commands as commands


def _bridge(tmp_path) -> TelegramBridge:  # noqa: ANN001
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _update(text: str, *, update_id: int = 8801) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 91,
            "chat": {"id": 5001, "type": "private"},
            "from": {"id": 5001, "first_name": "Owner"},
            "text": text,
        },
    }


def _client_stub() -> httpx.AsyncClient:
    """Typed opaque client: every network seam is replaced in these tests."""

    return cast(httpx.AsyncClient, object())


async def _never_typing(*_args: Any, **_kwargs: Any) -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("speech", "update_id", "checkpoint_count"),
    [
        ("Декомпилируй его.", 8801, 2),
        ("Сборка Main.java в JAR", 8805, 2),
        ("Проверь этот документ", 8802, 1),
    ],
)
async def test_long_turn_uses_one_revision_stream_without_fake_phase_percent_or_eta(
    tmp_path,
    monkeypatch,
    speech: str,
    update_id: int,
    checkpoint_count: int,
) -> None:
    bridge = _bridge(tmp_path)
    statuses: list[dict[str, Any]] = []
    final_messages: list[str] = []
    checkpoints_seen = asyncio.Event()

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await checkpoints_seen.wait()
        return {"message": "Готово", "message_format": "plain"}

    async def publish(
        _client: object,
        chat_id: int,
        operation_id: str,
        revision: int,
        text: str,
        **kwargs: Any,
    ) -> str:
        statuses.append(
            {
                "chat_id": chat_id,
                "operation_id": operation_id,
                "revision": revision,
                "text": text,
                **kwargs,
            }
        )
        if len([item for item in statuses if item["create"]]) == checkpoint_count:
            checkpoints_seen.set()
        return "sent" if len(statuses) == 1 else "edited"

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        final_messages.append(text)

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    monkeypatch.setattr(bridge._status_messages, "publish", publish)  # noqa: SLF001
    try:
        await bridge._process_update(  # noqa: SLF001
            _client_stub(),
            _client_stub(),
            _update(speech, update_id=update_id),
            cached_response=None,
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert final_messages == ["Готово"]
    assert len([item for item in statuses if item["create"]]) == checkpoint_count
    assert {item["operation_id"] for item in statuses} == {f"chat:{update_id}"}
    assert [item["revision"] for item in statuses] == list(range(1, len(statuses) + 1))
    assert statuses[-1]["terminal"] is True
    assert statuses[-1]["text"].startswith("✅")
    assert all("ETA" not in item["text"] for item in statuses)
    assert all(token in {"0%", "100%"} for item in statuses for token in re.findall(r"\d+%", item["text"]))
    assert all("javac" not in item["text"].casefold() for item in statuses)
    assert all("четырьмя минутами" not in item["text"].casefold() for item in statuses)
    assert all(speech not in item["text"] for item in statuses)
    assert all(item["reply_to_message_id"] == 91 for item in statuses)


@pytest.mark.asyncio
async def test_progress_is_cancelled_before_fast_final_answer_and_does_not_create_status(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    progress_waiting = asyncio.Event()
    progress_cancelled = asyncio.Event()
    statuses: list[str] = []
    sent: list[str] = []

    async def blocked_progress_delay(_delay: float) -> None:
        progress_waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            progress_cancelled.set()

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await progress_waiting.wait()
        return {"message": "Итог", "message_format": "plain"}

    async def publish(*_args: Any, **_kwargs: Any) -> str:
        statuses.append("unexpected")
        return "sent"

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        if text == "Итог":
            assert progress_cancelled.is_set()
        sent.append(text)

    monkeypatch.setattr(commands, "_progress_sleep", blocked_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    monkeypatch.setattr(bridge._status_messages, "publish", publish)  # noqa: SLF001
    try:
        await bridge._process_update(  # noqa: SLF001
            _client_stub(),
            _client_stub(),
            _update("Обычный быстрый запрос", update_id=8803),
            cached_response=None,
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert sent == ["Итог"]
    assert statuses == []


@pytest.mark.asyncio
async def test_progress_delivery_failure_never_fails_or_repeats_backend_turn(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    backend_calls = 0
    final_messages: list[str] = []

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal backend_calls
        backend_calls += 1
        await asyncio.sleep(0)
        return {"message": "Итог сохранён", "message_format": "plain"}

    async def failed_publish(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("synthetic Telegram outage")

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        final_messages.append(text)

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    monkeypatch.setattr(bridge._status_messages, "publish", failed_publish)  # noqa: SLF001
    try:
        await bridge._process_update(  # noqa: SLF001
            _client_stub(),
            _client_stub(),
            _update("Собери подробный ответ", update_id=8804),
            cached_response=None,
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert backend_calls == 1
    assert final_messages == ["Итог сохранён"]


@pytest.mark.asyncio
async def test_terminal_status_failure_never_replays_backend_answer_voice_or_files(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    update = _update("Собери подробный ответ", update_id=8820)
    assert bridge._inbox.store(update) is True  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    backend_calls = 0
    final_messages: list[str] = []
    voice_calls = 0
    file_calls = 0
    terminal_attempts = 0

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal backend_calls
        backend_calls += 1
        await asyncio.sleep(0)
        return {"message": "Итог уже доставлен", "message_format": "plain"}

    async def publish(*_args: Any, **kwargs: Any) -> str:
        nonlocal terminal_attempts
        if kwargs.get("terminal") is True:
            terminal_attempts += 1
            raise httpx.ReadTimeout("terminal edit response lost")
        return "sent"

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        final_messages.append(text)

    async def voice(*_args: Any, **_kwargs: Any) -> None:
        nonlocal voice_calls
        voice_calls += 1

    async def files(*_args: Any, **_kwargs: Any) -> None:
        nonlocal file_calls
        file_calls += 1

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    monkeypatch.setattr(bridge, "_deliver_voice_reply", voice)
    monkeypatch.setattr(bridge, "_deliver_generated_files", files)
    monkeypatch.setattr(bridge._status_messages, "publish", publish)  # noqa: SLF001
    try:
        await bridge._run_update(_client_stub(), _client_stub(), row)  # noqa: SLF001
        retained = bridge._inbox._conn.execute(  # noqa: SLF001
            "SELECT 1 FROM updates WHERE update_id=?",
            (8820,),
        ).fetchone()
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert retained is None
    assert backend_calls == 1
    assert final_messages == ["Итог уже доставлен"]
    assert voice_calls == 1
    assert file_calls == 1
    assert terminal_attempts == 1


@pytest.mark.asyncio
async def test_pre_cache_terminal_status_failure_cannot_retain_update(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    update = _update("Долгий запрос", update_id=8821)
    assert bridge._inbox.store(update) is True  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    terminal_attempts = 0

    async def publish(*_args: Any, **kwargs: Any) -> str:
        nonlocal terminal_attempts
        if kwargs.get("terminal") is True:
            terminal_attempts += 1
            raise httpx.ReadTimeout("pre-cache terminal edit response lost")
        return "sent"

    async def process(*_args: Any, **_kwargs: Any) -> None:
        state = commands._ChatProgressState("opaque", operation_id="chat:8821")  # noqa: SLF001
        state.started = True
        await commands._finish_chat_progress(  # noqa: SLF001
            bridge,
            _client_stub(),
            5001,
            91,
            state,
            commands.TelegramStatusStage.STOPPED,
        )

    monkeypatch.setattr(bridge, "_process_update", process)
    monkeypatch.setattr(bridge._status_messages, "publish", publish)  # noqa: SLF001
    try:
        await bridge._run_update(_client_stub(), _client_stub(), row)  # noqa: SLF001
        retained = bridge._inbox._conn.execute(  # noqa: SLF001
            "SELECT 1 FROM updates WHERE update_id=?",
            (8821,),
        ).fetchone()
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert retained is None
    assert terminal_attempts == 1


@pytest.mark.asyncio
async def test_ambiguous_initial_status_fence_degrades_ui_without_blocking_chat(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    update = _update("Собери подробный ответ", update_id=8822)
    assert bridge._inbox.store(update) is True  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    status_calls: list[str] = []
    status_attempted = asyncio.Event()
    backend_calls = 0
    final_messages: list[str] = []

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    def telegram_status(request: httpx.Request) -> httpx.Response:
        status_calls.append(request.url.path.rsplit("/", 1)[-1])
        status_attempted.set()
        raise httpx.ReadTimeout("accepted response may be lost", request=request)

    async def backend_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal backend_calls
        backend_calls += 1
        await status_attempted.wait()
        return {"message": "Итог", "message_format": "plain"}

    async def send(_client: object, _chat_id: int, text: str, **_kwargs: Any) -> None:
        final_messages.append(text)

    async def no_side_effect(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    monkeypatch.setattr(bridge, "_deliver_voice_reply", no_side_effect)
    monkeypatch.setattr(bridge, "_deliver_generated_files", no_side_effect)
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram_status)) as telegram:
        try:
            await bridge._run_update(telegram, _client_stub(), row)  # noqa: SLF001
            retained = bridge._inbox._conn.execute(  # noqa: SLF001
                "SELECT 1 FROM updates WHERE update_id=?",
                (8822,),
            ).fetchone()
            fence = bridge._inbox.telegram_status_send_fence(5001, "chat:8822")  # noqa: SLF001
        finally:
            bridge._inbox.close()  # noqa: SLF001

    assert retained is None
    assert fence == {"revision": 1}
    assert status_calls == ["sendMessage"]
    assert backend_calls == 1
    assert final_messages == ["Итог"]


@pytest.mark.asyncio
async def test_restarted_notifier_shares_one_finite_decompile_checkpoint_budget(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    revisions: list[int] = []
    delays: list[float] = []

    async def immediate_progress_delay(delay: float) -> None:
        delays.append(delay)
        return None

    async def publish(
        _client: object,
        _chat_id: int,
        _operation_id: str,
        revision: int,
        _text: str,
        **_kwargs: Any,
    ) -> str:
        revisions.append(revision)
        return "sent" if len(revisions) == 1 else "edited"

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge._status_messages, "publish", publish)  # noqa: SLF001
    state = commands._ChatProgressState("Декомпилируй этот файл", operation_id="chat:991")
    try:
        for _ in range(2):
            await commands._emit_chat_progress(  # noqa: SLF001
                bridge,
                _client_stub(),
                5001,
                91,
                state,
            )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert delays == pytest.approx(
        [12.0, 30.0, 60.0, 120.0, 300.0, 600.0, 750.0],
        abs=0.01,
    )
    assert revisions == list(range(1, 8))


@pytest.mark.asyncio
async def test_album_status_observes_download_staging_backend_and_delivery_in_order(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    first = _album_message(8810, 101, "photo-a", caption="private album prompt")
    second = _album_message(8811, 102, "photo-b")
    combined = dict(first)
    combined["friday_media_group_messages"] = [first["message"], second["message"]]
    statuses: list[dict[str, Any]] = []
    receiving_seen = asyncio.Event()
    staging_seen = asyncio.Event()
    backend_seen = asyncio.Event()

    async def immediate_progress_delay(_delay: float) -> None:
        return None

    async def publish(
        _client: object,
        _chat_id: int,
        operation_id: str,
        revision: int,
        text: str,
        **kwargs: Any,
    ) -> str:
        statuses.append({"operation_id": operation_id, "revision": revision, "text": text, **kwargs})
        if "получаю вложения" in text:
            receiving_seen.set()
        if "передаю вложения" in text:
            staging_seen.set()
        if "ядро обрабатывает" in text:
            backend_seen.set()
        return "sent" if len(statuses) == 1 else "edited"

    async def prepare(
        _telegram: object, message: dict[str, Any], _update_value: dict[str, Any]
    ) -> dict[str, Any]:
        await receiving_seen.wait()
        photo = message["photo"][-1]
        return {
            "filename": f"{photo['file_id']}.jpg",
            "mime_type": "image/jpeg",
            "content_base64": base64.b64encode(photo["file_id"].encode()).decode(),
            "source_ref": f"telegram-file:{photo['file_id']}",
        }

    async def backend_json(
        _client: object,
        _method: str,
        _path: str,
        payload: dict[str, Any],
        _user: str,
        _chat: str,
    ) -> dict[str, Any]:
        if payload.get("document_stage_only") is True:
            await staging_seen.wait()
            return {
                "file_ingestions": [
                    {
                        "telegram_item_receipt": {
                            "telegram_message_id": int(item["telegram_message_id"]),
                            "source_ref_sha256": hashlib.sha256(str(item["source_ref"]).encode()).hexdigest(),
                        },
                        "telegram_stage_ready": True,
                    }
                    for item in payload["documents"]
                ]
            }
        await backend_seen.wait()
        return {"message": "Готово", "message_format": "plain"}

    async def send(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(commands, "_progress_sleep", immediate_progress_delay)
    monkeypatch.setattr(bridge._status_messages, "publish", publish)  # noqa: SLF001
    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", _never_typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(  # noqa: SLF001
            _client_stub(),
            _client_stub(),
            combined,
            cached_response=None,
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001

    joined = "\n".join(item["text"] for item in statuses)
    assert joined.index("получаю вложения") < joined.index("передаю вложения")
    assert joined.index("передаю вложения") < joined.index("ядро обрабатывает")
    assert joined.index("ядро обрабатывает") < joined.index("отправляю готовый результат")
    assert statuses[-1]["terminal"] is True
    assert {item["operation_id"] for item in statuses} == {"chat:8810"}
    revisions = [item["revision"] for item in statuses]
    assert revisions == sorted(set(revisions))
    assert "private album prompt" not in joined
    assert any("из 2 файлов" in item["text"] for item in statuses)
    assert "✅ передаю вложения в ядро - 100%" in joined
    assert "✅ ядро обрабатывает запрос - 100%" in joined


def test_recurring_schedule_stays_strictly_below_configured_bridge_ceiling() -> None:
    state = commands._ChatProgressState("opaque", ceiling_sec=300)

    assert state.schedule == (12.0, 30.0, 60.0, 120.0, 270.0)


def _album_message(
    update_id: int,
    message_id: int,
    file_id: str,
    *,
    caption: str = "",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "media_group_id": "progress-album",
        "chat": {"id": 5001, "type": "private"},
        "from": {"id": 5001, "first_name": "Owner"},
        "photo": [
            {
                "file_id": file_id,
                "file_unique_id": f"unique-{file_id}",
                "file_size": 12,
                "width": 10,
                "height": 10,
            }
        ],
    }
    if caption:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}
