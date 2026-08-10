"""«Что Иван присылал» ищется по автору, а не по арендатору.

Признак `uploaded_by` пишут ВСЕ дороги приёма (Telegram, API, импорт), фильтр по
нему существовал в `_arrival_window` с самого начала — и ни одна из трёх дорог
надзора его не передавала. Найдено большим ревью 2026-08-04 тремя измерениями
независимо: границы, обещания, счётчики.

Хуже, чем «фильтра нет». В общем архиве `raw_objects.user_id` — АРЕНДАТОР, один
на всех, а надзор звали с учёткой ЧЕЛОВЕКА: строк с таким `user_id` в базе не
существует вовсе. То есть на вопрос «что присылал Иван» приходила пустота
всегда — и читалась она как «Иван ничего не присылал», хотя означала «искали не
там». Обещание без механизма опаснее отсутствия обещания.

Разведено: `user_id` отвечает на вопрос ГДЕ лежит материал, `uploaded_by` — ЧЕЙ
он. Вне общего архива это одно и то же, и поведение прежнее.

Материалы, принятые до появления признака, никому не приписываются: их считает
`arrivals_without_an_author` и говорит человеку отдельной строкой — «ноль» и
«неизвестно» разные ответы, особенно когда по ним судят о сотруднике.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.storage.models import InboxItem, RawObject, new_id

TENANT = "tenant"
IVAN = "person-ivan"
PETR = "person-petr"


def _arrival(
    storage,
    *,
    uploaded_by: str | None,
    text: str,
    received_at: str | None = None,
    tags: tuple[str, ...] = (),
) -> None:
    metadata: dict = {"filename": f"{text}.pdf"}
    if uploaded_by is not None:
        metadata["uploaded_by"] = uploaded_by
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=TENANT,
            source="telegram",
            source_ref=new_id("src"),
            raw_content=text,
            content_type="file",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            metadata_json=metadata,
            **({"received_at": received_at, "created_at": received_at} if received_at else {}),
        )
    )
    if tags:
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=TENANT,
                raw_object_id=raw.id,
                suggested_tags_json=list(tags),
                **({"created_at": received_at} if received_at else {}),
            )
        )


@pytest.fixture
def archive(storage):
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(IVAN, preset_key="user")
    storage.ensure_user(PETR, preset_key="user")
    _arrival(storage, uploaded_by=IVAN, text="смета Ивана")
    _arrival(storage, uploaded_by=IVAN, text="акт Ивана")
    _arrival(storage, uploaded_by=PETR, text="договор Петра")
    _arrival(storage, uploaded_by=None, text="старый документ без автора")
    storage.commit()
    return storage


def test_the_list_shows_only_his_own(archive):
    """Мутация: убрать `uploaded_by` из вызова — вернутся чужие материалы."""
    items = archive.user_activity(TENANT, uploaded_by=IVAN, limit=50)

    names = {str(item.get("filename") or "") for item in items}
    assert names == {"смета Ивана.pdf", "акт Ивана.pdf"}, f"надзор ответил не по автору: {names}"


def test_asking_by_the_person_id_alone_finds_nothing(archive):
    """Почему прежний вызов давал пустоту: материал лежит под арендатором.

    Это не «у Ивана ничего нет», это «искали в учётке, которой у материала быть
    не может». Тест закрепляет саму причину, чтобы правку не откатили обратно.
    """
    assert archive.user_activity(IVAN, limit=50) == []


def test_the_summary_counts_the_same_set(archive):
    """Список и сводка стоят в одной панели и обязаны отвечать на один вопрос."""
    summary = archive.user_activity_summary(TENANT, uploaded_by=IVAN)

    assert int(summary.get("arrivals") or 0) == 2, f"сводка считает не то же, что список: {summary}"


def test_the_analysis_counts_the_same_set(archive):
    """Третья дорога надзора — там же, где и первые две."""
    analysis = archive.user_activity_analysis(TENANT, uploaded_by=IVAN, analyses=["volume"])

    volume = analysis.get("volume") or {}
    assert int(volume.get("arrivals") or volume.get("count") or 0) == 2, analysis


@pytest.mark.parametrize("include_content", [True, False])
def test_change_compares_only_the_requested_authors_arrivals(storage, include_content):
    """The two-period branch must keep the same author scope as every other slice.

    Mutation: remove ``uploaded_by`` from the call to ``_change``.  Both arrival
    counts become 3 and, in the full-content case, Petr's and the unattributed
    topics enter Ivan's report.
    """
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(IVAN, preset_key="user")
    storage.ensure_user(PETR, preset_key="user")
    _arrival(
        storage,
        uploaded_by=IVAN,
        text="ivan-before",
        received_at="2026-01-01T12:00:00+00:00",
        tags=("ivan-old",),
    )
    _arrival(
        storage,
        uploaded_by=IVAN,
        text="ivan-now",
        received_at="2026-01-02T12:00:00+00:00",
        tags=("ivan-new",),
    )
    _arrival(
        storage,
        uploaded_by=PETR,
        text="petr-before",
        received_at="2026-01-01T13:00:00+00:00",
        tags=("petr-old",),
    )
    _arrival(
        storage,
        uploaded_by=PETR,
        text="petr-now",
        received_at="2026-01-02T13:00:00+00:00",
        tags=("petr-new",),
    )
    _arrival(
        storage,
        uploaded_by=None,
        text="unknown-before",
        received_at="2026-01-01T14:00:00+00:00",
        tags=("unknown-old",),
    )
    _arrival(
        storage,
        uploaded_by=None,
        text="unknown-now",
        received_at="2026-01-02T14:00:00+00:00",
        tags=("unknown-new",),
    )
    storage.commit()

    analysis = storage.user_activity_analysis(
        TENANT,
        uploaded_by=IVAN,
        analyses=["change"],
        since="2026-01-02T00:00:00+00:00",
        until="2026-01-03T00:00:00+00:00",
        include_content=include_content,
    )
    change = analysis["change"]

    assert change["arrivals_before"] == 1
    assert change["arrivals_now"] == 1
    if include_content:
        assert {row["topic"] for row in change["topics"]} == {"ivan-old", "ivan-new"}
        assert change["topics_compared"] == 2
        assert change["new_topics"] == ["ivan-new"]
        assert change["dropped_topics"] == ["ivan-old"]
    else:
        assert change["topics"] == []
        assert change["topics_compared"] == 0
        assert change["new_topics"] == []
        assert change["dropped_topics"] == []


def test_material_without_an_author_is_counted_apart(archive):
    """Ноль и неизвестность — разные ответы, и по ним судят о человеке."""
    assert archive.arrivals_without_an_author(TENANT) == 1


def test_a_personal_archive_is_untouched(storage):
    """Ошибка в другую сторону: где человек и есть арендатор, всё как было."""
    storage.ensure_user("alice", preset_key="owner")
    storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=new_id("src"),
            raw_content="её документ",
            content_type="file",
            content_hash=hashlib.sha256(b"solo").hexdigest(),
            metadata_json={"filename": "её.pdf"},
        )
    )
    storage.commit()

    items = storage.user_activity("alice", limit=50)

    assert [str(item.get("filename") or "") for item in items] == ["её.pdf"]


def test_document_inventory_excludes_voice_and_audio_before_count_and_pagination(storage):
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(IVAN, preset_key="user")

    def store(
        filename: str,
        *,
        uploaded_by: str | None,
        raw_content_type: str = "file",
        **metadata: str,
    ) -> None:
        payload = filename.encode()
        values: dict[str, str] = {"filename": filename, **metadata}
        if uploaded_by is not None:
            values["uploaded_by"] = uploaded_by
        storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id=TENANT,
                source="telegram",
                source_ref=new_id("src"),
                raw_content=filename,
                content_type=raw_content_type,
                content_hash=hashlib.sha256(payload).hexdigest(),
                metadata_json=values,
            )
        )

    store("report.pdf", uploaded_by=IVAN, mime_type="application/pdf", media_kind="document")
    store("telegram-voice-1.ogg", uploaded_by=IVAN, mime_type="audio/ogg", media_kind="voice")
    store("legacy-recording.ogg", uploaded_by=IVAN)
    store("recording.bin", uploaded_by=IVAN, mime_type="audio/mpeg")
    store("legacy-audio.bin", uploaded_by=IVAN, raw_content_type="audio")
    store("unattributed-voice.ogg", uploaded_by=None, media_kind="voice")
    storage.commit()

    items = storage.user_activity(TENANT, uploaded_by=IVAN, files_only=True, limit=50)
    summary = storage.user_activity_summary(TENANT, uploaded_by=IVAN, files_only=True)

    assert [item.get("filename") for item in items] == ["report.pdf"]
    assert summary["arrivals"] == 1
    assert storage.arrivals_without_an_author(TENANT, files_only=True) == 0
    assert storage.count_visible_raw_objects(TENANT, files_only=True) == 1

    # Audio remains ordinary activity evidence; it is excluded only from the
    # semantic document/file inventory.
    all_activity = storage.user_activity(TENANT, uploaded_by=IVAN, files_only=False, limit=50)
    assert {item.get("filename") for item in all_activity} == {
        "report.pdf",
        "telegram-voice-1.ogg",
        "legacy-recording.ogg",
        "recording.bin",
        "legacy-audio.bin",
    }
