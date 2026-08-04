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

from friday.storage.models import RawObject, new_id

TENANT = "tenant"
IVAN = "person-ivan"
PETR = "person-petr"


def _arrival(storage, *, uploaded_by: str | None, text: str) -> None:
    metadata: dict = {"filename": f"{text}.pdf"}
    if uploaded_by is not None:
        metadata["uploaded_by"] = uploaded_by
    storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=TENANT,
            source="telegram",
            source_ref=new_id("src"),
            raw_content=text,
            content_type="file",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            metadata_json=metadata,
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
