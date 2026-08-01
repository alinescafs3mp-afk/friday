"""Даты из документов были извлечены и выброшены — теперь по ним можно искать.

Замерено на архиве владельца: у 630 объектов из 1537 в метаданных лежит список дат,
в среднем по пять на документ, 3180 значений всего. Работа была сделана и не
использовалась НИГДЕ: ни колонкой, ни индексом, ни параметром листинга, ни сортировкой.
А `created_at` у 1531 объекта из 1537 — один и тот же день загрузки, то есть по нему
искать бессмысленно.

Формы, в которых даты лежат (замерено): 2537 как дд.мм.гггг, 345 в ISO, 223 — это
ВРЕМЯ («1:25»), остальное мусор вроде «00.00.0000».

Условие — «документ УПОМИНАЕТ дату в диапазоне», а не «дата документа такая». Второго
данные не дают: документ называет несколько дат, и какая его собственная — неизвестно.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from jericho.storage import iso_date
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make(storage, user_id: str, index: int, dates: list[str]) -> str:
    text = f"Документ {index}. " * 10
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}-{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
        metadata_json={"dates": dates},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


# --- нормализация -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("27.12.2025", "2025-12-27"),
        ("1.5.2020", "2020-05-01"),
        ("2023-03-15", "2023-03-15"),
        ("00.00.0000", None),
        ("1:25", None),
        ("31.02.2020", None),
        ("01.01.0001", None),
        ("", None),
        (None, None),
    ],
)
def test_only_real_dates_survive_normalisation(raw, expected):
    """Время и мусор обязаны отсеиваться ЗДЕСЬ.

    Иначе «за март 2023» вернёт документы, в которых нет ни одной мартовской даты, и
    человек перестанет фильтру верить. `31.02.2020` выглядит датой и датой не является;
    `01.01.0001` — артефакт разбора, а не дата документа.
    """
    assert iso_date(raw) == expected


# --- фильтр -------------------------------------------------------------------


def test_a_period_selects_documents_that_mention_a_date_inside_it(storage):
    storage.ensure_user("alice")
    march = _make(storage, "alice", 1, ["15.03.2023"])
    _make(storage, "alice", 2, ["10.09.2021"])
    _make(storage, "alice", 3, [])

    found = storage.list_knowledge_objects("alice", since="2023-03-01", until="2023-03-31")
    assert [item["id"] for item in found] == [march]


def test_both_bounds_are_optional(storage):
    storage.ensure_user("alice")
    old = _make(storage, "alice", 1, ["01.01.2015"])
    recent = _make(storage, "alice", 2, ["01.01.2025"])

    assert [i["id"] for i in storage.list_knowledge_objects("alice", since="2020-01-01")] == [recent]
    assert [i["id"] for i in storage.list_knowledge_objects("alice", until="2020-01-01")] == [old]


def test_a_document_with_several_dates_matches_on_any_of_them(storage):
    """Так и задумано: «упоминает», а не «датирован»."""
    storage.ensure_user("alice")
    wanted = _make(storage, "alice", 1, ["01.02.2019", "15.03.2023", "20.11.2024"])

    for since, until in (("2019-01-01", "2019-12-31"), ("2023-01-01", "2023-12-31")):
        assert [i["id"] for i in storage.list_knowledge_objects("alice", since=since, until=until)] == [
            wanted
        ]


def test_times_and_junk_never_land_inside_a_range(storage):
    """`1:25` не должен превратиться в дату и попасть в любой диапазон."""
    storage.ensure_user("alice")
    _make(storage, "alice", 1, ["1:25", "00.00.0000", "не дата"])

    assert storage.list_knowledge_objects("alice", since="1900-01-01", until="2200-01-01") == []


def test_the_count_uses_the_same_period_as_the_page(storage):
    """Правило проекта: счёт и выборка отвечают на ОДИН вопрос."""
    storage.ensure_user("alice")
    for index in range(9):
        _make(storage, "alice", index, ["15.03.2023"])
    for index in range(9, 14):
        _make(storage, "alice", index, ["15.03.2021"])

    page = storage.list_knowledge_objects("alice", since="2023-01-01", until="2023-12-31", limit=4)
    assert len(page) == 4
    assert storage.count_filtered_knowledge_objects("alice", since="2023-01-01", until="2023-12-31") == 9


def test_a_period_composes_with_the_title_search(storage):
    storage.ensure_user("alice")
    wanted = _make(storage, "alice", 1, ["15.03.2023"])
    _make(storage, "alice", 2, ["15.03.2023"])

    both = storage.list_knowledge_objects("alice", since="2023-01-01", until="2023-12-31")
    assert len(both) == 2
    narrowed = storage.list_knowledge_objects(
        "alice", since="2023-01-01", until="2023-12-31", query="Документ 1"
    )
    assert [item["id"] for item in narrowed] == [wanted]


def test_the_route_rejects_a_malformed_date_instead_of_ignoring_it(settings):
    """Опечатка в дате не должна тихо снимать фильтр — иначе человек увидит весь архив
    и решит, что за период ничего нет."""
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.get("/api/admin/knowledge?user_id=alice&since=март", headers=headers)
        assert response.status_code == 422


# --- СОБСТВЕННАЯ дата документа (из провенанса файла, не из текста) -----------


def _docx_bytes(created: str | None, *, modified: str | None = None) -> bytes:
    """Минимальный docx: только то, что читают извлекатель и core.xml."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Текст приказа о поверке оборудования.</w:t></w:r></w:p></w:body></w:document>",
        )
        parts = []
        if created:
            parts.append(f"<dcterms:created>{created}</dcterms:created>")
        if modified:
            parts.append(f"<dcterms:modified>{modified}</dcterms:modified>")
        if parts:
            archive.writestr(
                "docProps/core.xml",
                '<?xml version="1.0"?><cp:coreProperties '
                'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dcterms="http://purl.org/dc/terms/">' + "".join(parts) + "</cp:coreProperties>",
            )
    return buffer.getvalue()


def test_a_docx_carries_its_own_date_from_the_file_not_the_text():
    """У владельца дата загрузки одна на весь архив — день импорта. Собственную
    дату документа записал редактор при сохранении, и это не угадывание."""
    from jericho.documents import DocumentExtractor

    result = DocumentExtractor().extract(_docx_bytes("2023-04-12T08:30:00Z"), "приказ.docx", "")
    assert result.metadata.get("document_date") == "2023-04-12"


def test_modified_is_used_only_when_created_is_missing():
    from jericho.documents import DocumentExtractor

    extractor = DocumentExtractor()
    only_modified = extractor.extract(_docx_bytes(None, modified="2021-09-01T10:00:00Z"), "a.docx", "")
    assert only_modified.metadata.get("document_date") == "2021-09-01"

    both = extractor.extract(
        _docx_bytes("2019-02-03T10:00:00Z", modified="2024-01-01T10:00:00Z"), "b.docx", ""
    )
    assert both.metadata.get("document_date") == "2019-02-03", "создание важнее правки"


def test_a_file_without_core_properties_gets_no_invented_date():
    """Нет даты в файле — нет даты. Придумывать её значит вернуть ровно то
    угадывание, от которого фильтр по упоминаниям уходил."""
    from jericho.documents import DocumentExtractor

    result = DocumentExtractor().extract(_docx_bytes(None), "без-даты.docx", "")
    assert "document_date" not in result.metadata


@pytest.mark.parametrize("bogus", ["0000-00-00T00:00:00Z", "9999-12-31T00:00:00Z", "мусор"])
def test_implausible_dates_are_refused(bogus):
    from jericho.documents import DocumentExtractor

    result = DocumentExtractor().extract(_docx_bytes(bogus), "кривая.docx", "")
    assert "document_date" not in result.metadata


def _with_own_date(storage, user_id: str, index: int, document_date: str) -> str:
    text = f"Документ {index}. " * 10
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="file",
        content_hash=hashlib.sha256(f"own-{user_id}-{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="file",
        title=f"Свой {index}",
        metadata_json={"document_date": document_date},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_a_period_finds_documents_by_their_own_date(storage):
    """Документ 2023 года, не упоминающий дат в тексте, обязан находиться за 2023-й."""
    storage.ensure_user("alice")
    old = _with_own_date(storage, "alice", 1, "2023-03-15")
    new = _with_own_date(storage, "alice", 2, "2025-06-01")

    found = storage.list_knowledge_objects("alice", since="2023-01-01", until="2023-12-31")
    ids = [item["id"] for item in found]
    assert old in ids and new not in ids


def test_own_date_and_mentions_both_count(storage):
    """Дизъюнкция, а не замена: собственная дата есть не у всех, и сужение до неё
    молча потеряло бы всё, что пришло текстом."""
    storage.ensure_user("alice")
    by_own = _with_own_date(storage, "alice", 3, "2024-05-05")
    by_mention = _make(storage, "alice", 4, ["2024-05-06"])

    found = storage.list_knowledge_objects("alice", since="2024-05-01", until="2024-05-31")
    ids = {item["id"] for item in found}
    assert {by_own, by_mention} <= ids

    counted = storage.count_filtered_knowledge_objects("alice", since="2024-05-01", until="2024-05-31")
    assert counted == len(ids), "счётчик разошёлся со страницей"


def test_the_date_survives_a_failed_text_extraction():
    """Дата снимается независимо от разбора текста — и это не мелочь: на корпусе
    владельца 35 файлов не читаются вовсе, а их место в хронологии от этого не
    исчезает. Здесь docx намеренно неполон (нет _rels), python-docx на нём
    падает — дата обязана остаться."""
    from jericho.documents import DocumentExtractor

    result = DocumentExtractor().extract(_docx_bytes("2018-11-20T12:00:00Z"), "битый.docx", "")

    assert result.success is False, "проба перестала проверять именно неудачный разбор"
    assert result.metadata.get("document_date") == "2018-11-20"


def test_the_backfill_reaches_objects_ingested_before_dates_were_captured(settings, storage, tmp_path):
    """Дату начали снимать при приёме, а корпус загружен раньше: у владельца 1531
    объект из 1537 «создан» в день импорта. Проход достаёт дату из файла, который
    никуда не делся, и не создаёт версию — это дозапись провенанса, не правка."""
    import argparse

    from jericho.cli import _backfill_document_dates

    storage.ensure_user("alice")
    stored = settings.files_dir / "alice" / "old.docx"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(_docx_bytes("2015-06-08T09:00:00Z"))

    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref=new_id("src"),
        raw_content="Текст старого документа.",
        content_type="file",
        content_hash=hashlib.sha256(b"old").hexdigest(),
        metadata_json={"stored_path": "alice/old.docx", "filename": "old.docx"},
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content="Текст старого документа.",
        content_type="file",
        title="Старый",
    )
    storage.store_knowledge_object(knowledge)
    before = storage.get_knowledge_object(knowledge.id, "alice")

    assert _backfill_document_dates(argparse.Namespace(user=None, batch=50, limit=0)) == 0

    after = storage.get_knowledge_object(knowledge.id, "alice")
    assert json.loads(after["metadata_json"])["document_date"] == "2015-06-08"
    assert after["version"] == before["version"], "проход создал версию, хотя знание не менялось"

    # Идемпотентность: второй прогон не берёт объект снова.
    assert storage.knowledge_missing_document_date(user_id="alice") == []


def test_the_backfill_terminates_when_files_carry_no_dates(settings, storage):
    """Объект, у которого даты в файле нет, остаётся в выборке «без даты» навсегда.

    Первый прогон на живом архиве завершился только потому, что дательных
    объектов случайно хватило: пачка, целиком состоящая из файлов без дат,
    возвращалась бы бесконечно. Здесь таких объектов больше, чем размер пачки.
    """
    import argparse

    from jericho.cli import _backfill_document_dates

    storage.ensure_user("alice")
    for index in range(7):
        stored = settings.files_dir / "alice" / f"nodate{index}.docx"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(_docx_bytes(None))
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=new_id("src"),
            raw_content="Файл без даты.",
            content_type="file",
            content_hash=hashlib.sha256(f"nodate{index}".encode()).hexdigest(),
            metadata_json={"stored_path": f"alice/nodate{index}.docx"},
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="alice",
                raw_object_id=raw.id,
                content="Файл без даты.",
                content_type="file",
                title=f"Без даты {index}",
            )
        )

    # Пачка меньше числа объектов: прежний цикл на этом не остановился бы.
    assert _backfill_document_dates(argparse.Namespace(user=None, batch=2, limit=0)) == 0

    # И — главное — он обязан ПОСМОТРЕТЬ все семь, а не только первую пачку.
    # Прежняя редакция теста проверяла лишь код возврата, и потому оставалась
    # зелёной, когда проход осматривал 2 объекта из 7: выборка «первые N без
    # даты» отдаёт одни и те же строки, отфильтрованные по «уже виденным» они
    # дают пустую пачку, и цикл принимал конец первой страницы за конец корпуса.
    # Счётчик берётся из события, которое проход и так пишет.
    #
    # Мутация: убрать курсор `after_rowid` из вызова в `_backfill_document_dates`
    # — тест обязан покраснеть (осмотрено 2 вместо 7).
    events = storage.list_events(event_type="documents.dates_backfilled", limit=5)
    assert events, "проход не записал событие о своей работе"
    payload = events[0].get("payload")
    payload = payload if isinstance(payload, dict) else json.loads(str(payload or "{}"))
    assert payload["scanned"] == 7, (
        f"проход осмотрел {payload['scanned']} объектов из 7 — остальные для него не существуют"
    )


# --- ЖЁСТКИЙ предфильтр по периоду внутри поиска ------------------------------


async def _search(storage, **kwargs):
    from jericho.retrieval import HybridSearcher

    return await HybridSearcher(storage, None, record_usage=False).search("alice", "документ", **kwargs)


@pytest.mark.asyncio
async def test_the_window_filters_the_search_itself(storage):
    """Окно должно резать сам поиск, а не выдачу после него."""
    storage.ensure_user("alice")
    old = _with_own_date(storage, "alice", 10, "2019-04-01")
    new = _with_own_date(storage, "alice", 11, "2025-04-01")

    everything = await _search(storage)
    assert {old, new} <= {item["id"] for item in everything["results"]}

    windowed = await _search(storage, since="2019-01-01", until="2019-12-31")
    ids = {item["id"] for item in windowed["results"]}
    assert old in ids and new not in ids
    assert windowed["strategy"]["date_window"] is True
    assert windowed["strategy"]["date_window_applied"] is True


@pytest.mark.asyncio
async def test_an_empty_period_says_so_instead_of_looking_like_an_empty_archive(storage):
    """«В этот период ничего» и «в архиве ничего по теме» — разные ответы, и совет
    «загляните в Inbox» верен только для второго."""
    storage.ensure_user("alice")
    _with_own_date(storage, "alice", 12, "2025-04-01")

    found = await _search(storage, since="1990-01-01", until="1990-12-31")

    assert found["results"] == []
    assert found["strategy"]["date_window_empty"] is True


@pytest.mark.asyncio
async def test_the_window_is_applied_before_ranking_not_after(storage):
    """Ради этого предфильтр и стоит ПЕРЕД отбором.

    Пул собирается по релевантности и ограничен по размеру. Если наполнить архив
    документами вне окна, фильтрация ПОСЛЕ отбора вернула бы пустоту: в пул попали
    бы только они. Документ внутри окна обязан найтись всё равно.
    """
    storage.ensure_user("alice")
    for index in range(60):
        _with_own_date(storage, "alice", 100 + index, "2025-01-15")
    target = _with_own_date(storage, "alice", 200, "2018-06-01")

    windowed = await _search(storage, since="2018-01-01", until="2018-12-31")

    assert [item["id"] for item in windowed["results"]] == [target]


@pytest.mark.asyncio
async def test_the_window_ids_come_from_the_same_predicate_as_the_listing(storage):
    """Два определения «попадает в период» однажды разойдутся, и разойдутся молча."""
    storage.ensure_user("alice")
    by_own = _with_own_date(storage, "alice", 300, "2022-03-10")
    by_mention = _make(storage, "alice", 301, ["2022-03-11"])
    _with_own_date(storage, "alice", 302, "2024-01-01")

    listed = {
        item["id"] for item in storage.list_knowledge_objects("alice", since="2022-01-01", until="2022-12-31")
    }
    window = storage.knowledge_ids_in_window("alice", since="2022-01-01", until="2022-12-31")

    assert window == listed == {by_own, by_mention}


def test_no_window_means_no_filtering(storage):
    """Отсутствие окна и пустое окно — разные вещи: None против пустого множества."""
    storage.ensure_user("alice")
    _with_own_date(storage, "alice", 400, "2023-05-05")

    assert storage.knowledge_ids_in_window("alice") is None
    assert storage.knowledge_ids_in_window("alice", since="1990-01-01", until="1990-12-31") == set()
