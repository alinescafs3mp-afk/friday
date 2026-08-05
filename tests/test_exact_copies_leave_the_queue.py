"""Очередь разбора не должна состоять из решений, где решать нечего.

Замерено на живом архиве 2026-08-03: 200 конфликтов «почти-дубликат» ждали
человека, последний раз он их разбирал 1 августа. Разбор состава:

    точные копии (текст совпал знак в знак) ...  56
    версии (имя то же, текст другой) ..........  54
    «разные» (и имя, и текст другие) ..........  90   медиана похожести 0.99

Все 200 пришли ОДНИМ импортом папки, и ни одна из точных копий не ловилась
дедупликацией по хешу файла. То есть двести решений система создала себе сама.

Здесь закрываются ТОЛЬКО точные копии. Порог похожести не участвует вовсе:
`friday/dedup.py` замерил, что «дубликат» и «следующая заметка в серии»
перекрываются и никаким порогом не разделяются, поэтому всё, что не совпало
точно, остаётся человеку — включая 90 пар с похожестью 0.99.
"""

from __future__ import annotations

import argparse

from friday.storage.models import KnowledgeObject, RawObject


def _document(storage, ko_id: str, *, title: str, content: str, when: str) -> str:
    raw = RawObject(
        id=f"raw-{ko_id}",
        user_id="alice",
        source="upload",
        source_ref=ko_id,
        raw_content=content,
        content_type="file",
        received_at=when,
        created_at=when,
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=ko_id,
            user_id="alice",
            raw_object_id=raw.id,
            content=content,
            title=title,
            knowledge_kind="document",
            created_at=when,
        )
    )
    return ko_id


def _conflict(storage, a: str, b: str) -> str:
    record = storage.store_knowledge_conflict(
        "alice",
        a,
        b,
        conflict_type="near_duplicate",
        confidence=1.0,
        evidence={"cosine": 1.0},
    )
    return str(record["id"])


def _run(monkeypatch, settings, *, apply_changes: bool) -> None:
    from friday import cli

    monkeypatch.setattr(cli, "_purge", cli._purge)  # noqa: SLF001 — модуль уже импортирован
    cli._resolve_exact_duplicates(argparse.Namespace(apply=apply_changes))


def test_an_exact_copy_leaves_the_queue(settings, storage, monkeypatch, capsys) -> None:
    """Мутация: сравнивать не текст, а что угодно ещё — пара не закроется."""
    storage.ensure_user("alice", preset_key="admin")
    text = "Приказ №214. О проведении поверки."
    a = _document(storage, "ko-a", title="Приказ.docx", content=text, when="2026-07-29T10:00:00+00:00")
    b = _document(storage, "ko-b", title="Приказ (1).docx", content=text, when="2026-07-29T11:00:00+00:00")
    conflict_id = _conflict(storage, a, b)
    storage.close()

    _run(monkeypatch, settings, apply_changes=True)

    from friday.storage import init_storage

    fresh = init_storage(settings)
    try:
        record = fresh.get_knowledge_conflict("alice", conflict_id)
        assert str(record["status"]) != "suggested", "точная копия осталась ждать человека"
    finally:
        fresh.close()


def test_the_earlier_record_survives(settings, storage, monkeypatch) -> None:
    """Победитель — более ранняя запись: повтор воспроизводит ПЕРВОЕ решение."""
    storage.ensure_user("alice", preset_key="admin")
    text = "Ведомость инструктажа"
    _document(storage, "ko-old", title="в.docx", content=text, when="2026-07-29T09:00:00+00:00")
    _document(storage, "ko-new", title="в (копия).docx", content=text, when="2026-07-29T18:00:00+00:00")
    _conflict(storage, "ko-old", "ko-new")
    storage.close()

    _run(monkeypatch, settings, apply_changes=True)

    from friday.storage import init_storage

    fresh = init_storage(settings)
    try:
        old = fresh.get_knowledge_object("ko-old", "alice")
        new = fresh.get_knowledge_object("ko-new", "alice")
        assert str(old["lifecycle_stage"]) != "deprecated", "выжила поздняя запись вместо ранней"
        assert str(new["lifecycle_stage"]) == "deprecated"
    finally:
        fresh.close()


def test_a_version_is_left_to_the_person(settings, storage, monkeypatch) -> None:
    """Тот же документ с ДРУГИМ текстом — это версия, и решает человек.

    Иначе правка приказа молча погасила бы его прежнюю редакцию.
    """
    storage.ensure_user("alice", preset_key="admin")
    _document(
        storage, "ko-v1", title="Приказ.docx", content="Поверку до 1 июня", when="2026-07-29T09:00:00+00:00"
    )
    _document(
        storage, "ko-v2", title="Приказ.docx", content="Поверку до 15 июня", when="2026-07-30T09:00:00+00:00"
    )
    conflict_id = _conflict(storage, "ko-v1", "ko-v2")
    storage.close()

    _run(monkeypatch, settings, apply_changes=True)

    from friday.storage import init_storage

    fresh = init_storage(settings)
    try:
        assert str(fresh.get_knowledge_conflict("alice", conflict_id)["status"]) == "suggested"
    finally:
        fresh.close()


def test_a_very_similar_pair_is_left_to_the_person(settings, storage, monkeypatch) -> None:
    """90 пар в живом архиве похожи на 0.99 — и всё равно остаются человеку.

    Порог здесь не применяется намеренно: замер dedup.py показал, что «дубликат»
    и «следующая заметка в серии» этим порогом не разделяются.
    """
    storage.ensure_user("alice", preset_key="admin")
    base = "Строевая записка на 5 марта. " * 40
    _document(storage, "ko-x", title="5.03.xlsx", content=base + "Иванов", when="2026-07-29T09:00:00+00:00")
    _document(storage, "ko-y", title="6.03.xlsx", content=base + "Петров", when="2026-07-29T10:00:00+00:00")
    conflict_id = _conflict(storage, "ko-x", "ko-y")
    storage.close()

    _run(monkeypatch, settings, apply_changes=True)

    from friday.storage import init_storage

    fresh = init_storage(settings)
    try:
        assert str(fresh.get_knowledge_conflict("alice", conflict_id)["status"]) == "suggested"
    finally:
        fresh.close()


def test_the_show_mode_changes_nothing(settings, storage, monkeypatch) -> None:
    """По умолчанию проход только считает: это чужие данные, а не черновик."""
    storage.ensure_user("alice", preset_key="admin")
    text = "Акт по допуску"
    _document(storage, "ko-1", title="акт.docx", content=text, when="2026-07-29T09:00:00+00:00")
    _document(storage, "ko-2", title="акт (1).docx", content=text, when="2026-07-29T10:00:00+00:00")
    conflict_id = _conflict(storage, "ko-1", "ko-2")
    storage.close()

    _run(monkeypatch, settings, apply_changes=False)

    from friday.storage import init_storage

    fresh = init_storage(settings)
    try:
        assert str(fresh.get_knowledge_conflict("alice", conflict_id)["status"]) == "suggested"
    finally:
        fresh.close()


def test_a_cluster_of_copies_does_not_break_the_pass(settings, storage, monkeypatch) -> None:
    """Найдено на живом архиве: проход упал на 55-й паре из 56.

    Копии образуют КЛАСТЕРЫ: три копии одного документа дают три пары, и запись,
    проигравшая в первой паре, во второй оказывается «более ранней». Разрешение
    такой пары справедливо отвергается хранилищем («winner is already
    deprecated»), и весь остаток очереди оставался неразобранным.

    Мутация: брать раннюю запись без проверки на `deprecated` — тест краснеет.
    """
    storage.ensure_user("alice", preset_key="admin")
    text = "Строевая записка 5.3"
    for index, when in enumerate(["09:00", "10:00", "11:00"]):
        _document(
            storage,
            f"ko-c{index}",
            title=f"5.3 ({index}).xlsx",
            content=text,
            when=f"2026-07-29T{when}:00+00:00",
        )
    ids = [
        _conflict(storage, "ko-c0", "ko-c1"),
        _conflict(storage, "ko-c1", "ko-c2"),
        _conflict(storage, "ko-c0", "ko-c2"),
    ]
    storage.close()

    _run(monkeypatch, settings, apply_changes=True)

    from friday.storage import init_storage

    fresh = init_storage(settings)
    try:
        left = [i for i in ids if str(fresh.get_knowledge_conflict("alice", i)["status"]) == "suggested"]
        assert left == [], f"проход бросил {len(left)} пар кластера неразобранными"
        # Ровно одна запись кластера остаётся живой — самая ранняя.
        stages = {
            ko: str(fresh.get_knowledge_object(ko, "alice")["lifecycle_stage"])
            for ko in ("ko-c0", "ko-c1", "ko-c2")
        }
        assert stages["ko-c0"] != "deprecated", stages
        assert stages["ko-c1"] == "deprecated" and stages["ko-c2"] == "deprecated", stages
    finally:
        fresh.close()


def test_a_pair_whose_both_sides_are_already_gone_does_not_linger(settings, storage, monkeypatch) -> None:
    """Найдено на живом архиве ПОСЛЕ первой правки: вечный хвост в очереди.

    Когда обе стороны пары уже погашены другими парами кластера, выбирать не из
    чего — но пропуск оставлял пару в статусе `suggested` навсегда. Проход
    печатал «точных копий: 1» и закрывал ноль, а человек не смог бы разобрать её
    и вручную.

    Мутация: вернуть голый `continue` — пара снова зависает.
    """
    storage.ensure_user("alice", preset_key="admin")
    text = "схема дня"
    for index in range(2):
        _document(
            storage,
            f"ko-g{index}",
            title=f"схема {index}.docx",
            content=text,
            when=f"2026-07-29T1{index}:00:00+00:00",
        )
    # Обе записи гасим заранее — так выглядит хвост кластера.
    for index in range(2):
        storage.update_knowledge_fields(f"ko-g{index}", "alice", lifecycle_stage="deprecated")
    conflict_id = _conflict(storage, "ko-g0", "ko-g1")
    storage.close()

    _run(monkeypatch, settings, apply_changes=True)

    from friday.storage import init_storage

    fresh = init_storage(settings)
    try:
        status = str(fresh.get_knowledge_conflict("alice", conflict_id)["status"])
        assert status != "suggested", "пара без живых сторон осталась висеть в очереди"
    finally:
        fresh.close()


def test_whitespace_is_not_a_difference(settings, storage, monkeypatch) -> None:
    """Экспорт из Word и из PDF расставляет переносы по-разному."""
    storage.ensure_user("alice", preset_key="admin")
    _document(
        storage, "ko-w1", title="а.docx", content="Приказ 214\nо поверке", when="2026-07-29T09:00:00+00:00"
    )
    _document(
        storage, "ko-w2", title="а.pdf", content="Приказ 214   о поверке", when="2026-07-29T10:00:00+00:00"
    )
    conflict_id = _conflict(storage, "ko-w1", "ko-w2")
    storage.close()

    _run(monkeypatch, settings, apply_changes=True)

    from friday.storage import init_storage

    fresh = init_storage(settings)
    try:
        assert str(fresh.get_knowledge_conflict("alice", conflict_id)["status"]) != "suggested"
    finally:
        fresh.close()
