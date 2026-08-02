"""Whisper transcription — §9: voice notes become text.

Pins the pure helpers (audio routing, per-segment confidence, transcript
assembly) without loading a model or touching a file, plus the ingestion wiring
(transcribe → inbox-first → confirm) with the transcription monkeypatched, so the
suite stays fast and free of the optional faster-whisper dependency.
"""

from __future__ import annotations

import dataclasses
import json
import math

import pytest

from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import InboxStatus
from friday.whisper import (
    Transcript,
    _segment_confidence,
    assemble_transcript,
    looks_like_audio,
)


class _Seg:
    def __init__(self, text: str, avg_logprob: float):
        self.text = text
        self.avg_logprob = avg_logprob


class _Info:
    def __init__(self, language: str, language_probability: float, duration: float):
        self.language = language
        self.language_probability = language_probability
        self.duration = duration


# --- audio routing --------------------------------------------------------


@pytest.mark.parametrize(
    "content_type,filename,expected",
    [
        ("audio/ogg", "voice.oga", True),  # Telegram voice note
        ("audio/ogg; codecs=opus", None, True),
        (None, "note.opus", True),
        (None, "recording.MP3", True),  # case-insensitive extension
        ("video/webm", "clip.webm", True),
        ("image/png", "photo.png", False),
        ("application/pdf", "doc.pdf", False),
        (None, "notes.txt", False),
        (None, None, False),
    ],
)
def test_looks_like_audio(content_type, filename, expected):
    assert looks_like_audio(content_type=content_type, filename=filename) is expected


# --- per-segment confidence ----------------------------------------------


def test_segment_confidence_maps_logprob_to_unit_interval():
    assert _segment_confidence(0.0) == 1.0  # perfect
    assert _segment_confidence(-0.3) == pytest.approx(math.exp(-0.3), rel=1e-6)
    assert 0.0 <= _segment_confidence(-20.0) < 0.01  # very unsure
    assert _segment_confidence(5.0) == 1.0  # clamps above 1


# --- transcript assembly --------------------------------------------------


def test_assemble_joins_segments_and_averages_confidence():
    segments = [
        _Seg("  Привет, ", -0.2),
        _Seg("это тест.", -0.4),
        _Seg("   ", -0.6),  # blank text still counts toward segments/confidence
    ]
    info = _Info("ru", 0.98, 3.5)

    t = assemble_transcript(segments, info, model="small")

    assert isinstance(t, Transcript)
    assert t.text == "Привет, это тест."  # trimmed, joined, blank dropped
    assert t.language == "ru"
    assert t.language_probability == 0.98
    assert t.duration == 3.5
    assert t.segment_count == 3
    expected_conf = round((math.exp(-0.2) + math.exp(-0.4) + math.exp(-0.6)) / 3, 4)
    assert t.confidence == expected_conf
    assert not t.is_empty


def test_assemble_empty_audio_yields_empty_transcript():
    t = assemble_transcript([], _Info("ru", 0.0, 0.0), model="base")
    assert t.text == ""
    assert t.confidence == 0.0
    assert t.segment_count == 0
    assert t.is_empty


# --- ingestion wiring (transcription monkeypatched, no real model) ---------

_VOICE_TEXT = "Напомни завтра купить билеты на поезд в Казань."


def _fake_transcribe(content, **kwargs):
    return Transcript(
        text=_VOICE_TEXT,
        language="ru",
        language_probability=0.99,
        confidence=0.86,
        duration=4.2,
        segment_count=1,
        model=kwargs.get("model", "small"),
    )


@pytest.mark.asyncio
async def test_voice_note_transcribes_inbox_first_then_confirms(settings, storage, monkeypatch):
    monkeypatch.setattr("friday.ingestion._files.transcribe_bytes", _fake_transcribe)
    settings = dataclasses.replace(settings, whisper_enabled=True)
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    result = await pipeline.ingest_file(
        "alice",
        None,
        b"OggS-fake-voice-bytes",
        filename="telegram-voice-42.oga",
        mime_type="audio/ogg",
        media_kind="voice",
        metadata={"duration_sec": 4},
        source_ref="telegram-file:42",
    )

    # A transcript is model-generated text: inbox-first, no KO yet (DATA_LIFECYCLE §3).
    assert result["promoted"] is False
    assert result["knowledge_object"] is None
    assert storage.search_knowledge("alice", "Казань") == []

    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    assert inbox["status"] == "pending" and inbox["knowledge_object_id"] is None
    suggestions = json.loads(inbox["suggestions_json"])
    # Provenance: the transcript is recorded as advisory, model-attributed, and the
    # bulky transcript text is not duplicated into the metadata block.
    tx = suggestions["metadata"]["transcription"]
    assert tx["model"] == "small" and tx["language"] == "ru"
    assert tx["advisory_only"] is True
    assert "text" not in tx

    # Confirmation builds the KO from the transcript; only now is it searchable.
    pipeline.classify_inbox_item(
        "alice", inbox["id"], InboxStatus.CLASSIFIED, promote=True, reviewed_by="alice"
    )
    ko = storage.get_knowledge_by_raw(result["raw_object_id"], "alice")
    assert ko is not None
    assert [hit["id"] for hit in storage.search_knowledge("alice", "Казань")] == [ko["id"]]


@pytest.mark.asyncio
async def test_voice_note_untouched_when_whisper_disabled(settings, storage, monkeypatch):
    calls: list[bool] = []

    def _spy(content, **kwargs):
        calls.append(True)
        return _fake_transcribe(content, **kwargs)

    monkeypatch.setattr("friday.ingestion._files.transcribe_bytes", _spy)
    settings = dataclasses.replace(settings, whisper_enabled=False)
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    result = await pipeline.ingest_file(
        "alice",
        None,
        b"OggS-fake",
        filename="voice.oga",
        mime_type="audio/ogg",
        media_kind="voice",
        source_ref="telegram-file:99",
    )

    assert calls == []  # disabled: no transcription attempted
    assert result["knowledge_object"] is None  # still un-extractable, inbox-first
    raw = storage.get_raw_object(result["raw_object_id"], "alice")
    assert "voice.oga" in raw["raw_content"]  # placeholder marker, not a transcript


@pytest.mark.asyncio
async def test_empty_transcript_falls_back_to_unextractable(settings, storage, monkeypatch):
    def _silent(content, **kwargs):
        return Transcript(
            text="   ",
            language="ru",
            language_probability=0.4,
            confidence=0.0,
            duration=1.0,
            segment_count=0,
            model="small",
        )

    monkeypatch.setattr("friday.ingestion._files.transcribe_bytes", _silent)
    settings = dataclasses.replace(settings, whisper_enabled=True)
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    result = await pipeline.ingest_file(
        "alice",
        None,
        b"OggS-fake",
        filename="voice.oga",
        mime_type="audio/ogg",
        media_kind="voice",
        source_ref="telegram-file:silent",
    )

    assert result["knowledge_object"] is None
    raw = storage.get_raw_object(result["raw_object_id"], "alice")
    assert "voice.oga" in raw["raw_content"]  # empty transcript → placeholder, no KO


@pytest.mark.asyncio
async def test_long_audio_skipped_by_duration_guard(settings, storage, monkeypatch):
    calls: list[bool] = []

    def _spy(content, **kwargs):
        calls.append(True)
        return _fake_transcribe(content, **kwargs)

    monkeypatch.setattr("friday.ingestion._files.transcribe_bytes", _spy)
    settings = dataclasses.replace(settings, whisper_enabled=True, whisper_max_audio_sec=60.0)
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    result = await pipeline.ingest_file(
        "alice",
        None,
        b"OggS-fake",
        filename="voice.oga",
        mime_type="audio/ogg",
        media_kind="voice",
        metadata={"duration_sec": 3600},  # an hour — over the 60s guard
        source_ref="telegram-file:long",
    )

    assert calls == []  # guard skipped transcription before loading a huge file
    assert result["knowledge_object"] is None


# --- голосовой вопрос отвечается СЕЙЧАС (проводка через /api/chat) -----------


def test_ingest_file_returns_the_transcript_for_the_caller(settings, storage, monkeypatch):
    """Чату нужен транскрипт, чтобы ответить на сказанное сразу; раньше он жил
    только в raw_content инбокс-элемента."""
    import asyncio

    monkeypatch.setattr("friday.ingestion._files.transcribe_bytes", _fake_transcribe)
    tuned = dataclasses.replace(settings, whisper_enabled=True)
    pipeline = IngestionPipeline(tuned, storage, KnowledgeGraph(storage), None)

    result = asyncio.run(
        pipeline.ingest_file(
            "alice",
            None,
            b"OggS-fake-voice-bytes",
            filename="telegram-voice-51.oga",
            mime_type="audio/ogg",
            media_kind="voice",
            metadata={"duration_sec": 4},
            source_ref="telegram-file:51",
        )
    )
    assert result["transcript_text"] == _VOICE_TEXT


def test_a_short_voice_note_becomes_the_question_of_the_turn(settings, monkeypatch):
    """Голосовое — обычно вопрос, произнесённый вслух. Раньше модель отвечала,
    видя лишь имя .ogg-файла, а retrieval искал по «Загружен документ» — на
    голос система не могла ответить по существу НИКОГДА."""
    import base64

    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    monkeypatch.setattr("friday.ingestion._files.transcribe_bytes", _fake_transcribe)
    tuned = dataclasses.replace(settings, whisper_enabled=True)
    app = create_app(tuned)
    headers = {"Authorization": f"Bearer {tuned.api_token}"}
    payload = {
        "document": {
            "filename": "telegram-voice-77.oga",
            "mime_type": "audio/ogg",
            "content_base64": base64.b64encode(b"OggS-fake-voice").decode("ascii"),
            "media_kind": "voice",
            "duration": 4,
        }
    }
    with TestClient(app) as client:
        response = client.post("/api/chat", json=payload, headers=headers)
        assert response.status_code == 200
        conversation_id = response.json().get("conversation_id")
        assert conversation_id
        messages = app.state.storage.get_conversation_messages(conversation_id, user_id=LEGACY_OWNER_USER_ID)
        user_turns = [m for m in messages if m.get("role") == "user"]
        assert user_turns and user_turns[0]["content"] == _VOICE_TEXT, (
            "текстом хода должно стать сказанное, а не имя файла"
        )
        # Файл по-прежнему inbox-first: ответ сейчас не отменяет разбора.
        inbox = client.get("/api/inbox?status=pending", headers=headers).json()["items"]
        assert inbox, "голосовой файл перестал попадать в Inbox"


def test_a_long_voice_note_stays_a_document_upload(settings, monkeypatch):
    """Длиннее трёх минут — диктовка, не вопрос: прежний путь с уведомлением."""
    import base64

    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    monkeypatch.setattr("friday.ingestion._files.transcribe_bytes", _fake_transcribe)
    tuned = dataclasses.replace(settings, whisper_enabled=True)
    app = create_app(tuned)
    headers = {"Authorization": f"Bearer {tuned.api_token}"}
    payload = {
        "document": {
            "filename": "telegram-voice-78.oga",
            "mime_type": "audio/ogg",
            "content_base64": base64.b64encode(b"OggS-long-voice").decode("ascii"),
            "media_kind": "voice",
            "duration": 400,
        }
    }
    with TestClient(app) as client:
        response = client.post("/api/chat", json=payload, headers=headers)
        assert response.status_code == 200
        conversation_id = response.json().get("conversation_id")
        messages = app.state.storage.get_conversation_messages(conversation_id, user_id=LEGACY_OWNER_USER_ID)
        user_turns = [m for m in messages if m.get("role") == "user"]
        assert user_turns and user_turns[0]["content"].startswith("Загружен документ")


def test_a_voice_question_is_answered_with_voice():
    """Мутация: убрать `answer_with_voice` из вызова — тест краснеет.

    Человек записывает голосовое, когда ему неудобно печатать; отвечать ему
    стеной текста — предлагать читать там, где он выбрал слушать. Текст приходит
    рядом, как и раньше.
    """
    import inspect

    from friday import server

    source = inspect.getsource(server)
    assert "answer_with_voice=spoken_question" in source, (
        "голосовой вопрос не помечен как просьба ответить голосом"
    )
    # Флаг объявлен до разбора вложения: без вложения ветка не выполняется, и
    # обычный текстовый ход падал бы на обращении к переменной.
    assert source.index("spoken_question = False") < source.index("answer_with_voice=spoken_question")


def test_a_repeated_voice_note_keeps_its_transcript(settings, storage):
    """Мутация: убрать транскрипт из `_replay_file_source` — тест краснеет.

    Замерено на живой переписке (пользователь Пегас, 2 августа): голосовое
    распозналось верно — «Привет, пятница!» — и легло в архив, а на второй и
    третий присыл того же файла срабатывал дедуп, и вызывающий получал словарь
    БЕЗ транскрипта. Ход превращался в «Загружен документ:
    telegram-voice-63.ogg», и Пятница трижды отвечала «я не могу услышать его
    напрямую» — при том что услышала с первого раза.
    """
    import inspect

    from friday.ingestion._files import FilesMixin

    source = inspect.getsource(FilesMixin._replay_file_source)  # noqa: SLF001
    assert '"transcript_text"' in source, "повтор теряет распознанный голос"
    assert 'raw_metadata.get("transcription")' in source, (
        "транскрипт берётся не из провенанса — значит не оттуда, где он лежит"
    )
    # Служебная подпись файла транскриптом не считается.
    assert 'not spoken.startswith("[")' in source


def test_the_language_is_pinned_not_guessed():
    """Мутация: убрать язык из профиля — тест краснеет.

    Живой случай: голосовое «проверка связи» длиной 1.4 с распозналось как
    португальское «Pra ver com as vezes.» — на короткой записи автоопределение
    ошибается, а в профиле человека стоял `language_code: ru`, то есть ответ был
    известен заранее. Владелец подтвердил: других языков здесь не будет.
    """
    import inspect

    from friday import server
    from friday.ingestion._files import FilesMixin

    source = inspect.getsource(FilesMixin._transcribe_audio)  # noqa: SLF001
    assert 'self.settings.whisper_language' in source
    assert '(metadata or {}).get("language_code")' in source, (
        "язык человека из профиля не используется"
    )
    assert "language=language or None" in source

    # И профиль до приёма доезжает: без этого поле в метаданных всегда пусто.
    assert 'file_metadata["language_code"] = language_code' in inspect.getsource(server)
