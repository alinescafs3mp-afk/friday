"""Кто принёс материал — записывается, а неизвестное называется неизвестным.

Найдено ревью уязвимых участков 2026-08-04 и подтверждено замером на живой базе:
3295 документов из 3296 лежат под ОДНИМ идентификатором (архив общий), и признака
автора у них нет ни в столбцах, ни в метаданных. Значит надзор «что Иван присылал»
был неразрешим в принципе: поиск по человеку давал ноль всегда.

Сделано две вещи, и вторая важнее первой.

ПЕРВОЕ: все дороги приёма пишут `uploaded_by` — телеграм (файл и текст), HTTP
загрузка файла, HTTP приём текста. Ключ единый, чтобы надзор не гадал, где искать.

ВТОРОЕ: у материалов, принятых РАНЬШЕ, автора нет, и приписать их кому-либо
нельзя — догадка здесь означала бы приписать человеку чужие документы. Такие
строки в ответ по автору не попадают и считаются отдельно, честной строкой «без
автора». Это решение принято сознательно: «ноль у Ивана» рядом с тремя тысячами
документов неизвестного происхождения читается как «Иван ничего не присылал», и
на этом строят кадровые выводы.
"""

from __future__ import annotations

import json

import pytest

from friday.storage.models import RawObject, new_id


def _arrived(storage, user_id: str, *, uploaded_by: str | None, at: str) -> None:
    storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="telegram",
            source_ref=new_id("ref"),
            raw_content="текст материала",
            content_type="text",
            metadata_json={"uploaded_by": uploaded_by} if uploaded_by is not None else {},
            received_at=at,
        )
    )


def test_arrivals_are_counted_for_the_person_who_brought_them(storage) -> None:
    """Мутация: убрать условие по автору — в ответ попадут чужие материалы."""
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by="person-a", at="2026-08-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by="person-b", at="2026-08-01T11:00:00+00:00")

    where, params = storage._arrival_window(  # noqa: SLF001
        "tenant", None, None, uploaded_by="person-a"
    )
    count = storage.execute(f"SELECT COUNT(*) AS n FROM raw_objects WHERE {where}", tuple(params)).fetchone()[
        "n"
    ]

    assert count == 1, "надзор по человеку считает чужие материалы"


def test_material_without_an_author_is_counted_separately(storage) -> None:
    """Неизвестность называется вслух, а не превращается в ноль.

    Это и есть смысл правки: «у Ивана ноль» рядом с тремя тысячами документов
    неизвестного происхождения — не факт о человеке, а факт о том, что мы не
    знаем.
    """
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by="person-a", at="2026-08-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-07-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-07-02T10:00:00+00:00")

    assert storage.arrivals_without_an_author("tenant") == 2


def test_old_material_is_not_attributed_to_anybody(storage) -> None:
    """Догадка здесь означала бы приписать человеку чужие документы."""
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-07-01T10:00:00+00:00")

    where, params = storage._arrival_window(  # noqa: SLF001
        "tenant", None, None, uploaded_by="tenant"
    )
    count = storage.execute(f"SELECT COUNT(*) AS n FROM raw_objects WHERE {where}", tuple(params)).fetchone()[
        "n"
    ]

    assert count == 0, "материал без автора приписан владельцу архива"


def test_the_window_without_an_author_still_sees_everything(storage) -> None:
    """Обратная сторона: без указания автора надзор смотрит весь архив.

    Владельцу это и нужно; сузив ответ молча, мы отняли бы у него общий обзор.
    """
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by="person-a", at="2026-08-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-07-01T10:00:00+00:00")

    where, params = storage._arrival_window("tenant", None, None)  # noqa: SLF001
    count = storage.execute(f"SELECT COUNT(*) AS n FROM raw_objects WHERE {where}", tuple(params)).fetchone()[
        "n"
    ]

    assert count == 2


def test_every_intake_road_records_the_uploader() -> None:
    """Все дороги приёма пишут автора одним ключом.

    Ворота на одной дороге не охраняют ничего — здесь тот же счёт: признак,
    записанный на одной дороге из четырёх, делает надзор выборочным и незаметно
    неверным.
    """
    import inspect

    from friday import server
    from friday.api import files, ingest

    for source in (
        inspect.getsource(files),
        inspect.getsource(ingest),
    ):
        assert "uploaded_by" in source, "дорога приёма не пишет автора"

    telegram = inspect.getsource(server.create_app)
    assert telegram.count('"uploaded_by"') + telegram.count("['uploaded_by']") >= 1
    assert 'file_metadata["uploaded_by"]' in telegram, "телеграмный файл идёт без автора"


def test_the_marker_is_json_readable(storage) -> None:
    """Признак читается из метаданных ровно тем выражением, что стоит в запросе."""
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by="person-a", at="2026-08-01T10:00:00+00:00")

    row = storage.execute(
        "SELECT json_extract(metadata_json,'$.uploaded_by') AS who FROM raw_objects LIMIT 1"
    ).fetchone()

    assert row["who"] == "person-a"
    assert (
        json.loads(storage.execute("SELECT metadata_json AS m FROM raw_objects LIMIT 1").fetchone()["m"])[
            "uploaded_by"
        ]
        == "person-a"
    )


@pytest.mark.asyncio
async def test_the_tool_itself_carries_the_count_to_the_model(settings, storage):
    """Счётчик безымянных загрузок доезжает до МОДЕЛИ, а не просто существует.

    Найдено мутацией: удаление строки, которая кладёт число в ответ, оставляло все
    тесты зелёными — один проверял хранилище, другой формулировку, и между ними
    зияла дыра ровно в том месте, где эти двое соединяются. Это на проекте
    отдельный класс: «проверять не механизм, а что он подключён».
    """
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("tenant", preset_key="owner")
    storage.ensure_user("bob", preset_key="user")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-08-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-08-01T11:00:00+00:00")

    auth = AuthorizationService(storage, shared_tenant="tenant")
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    owner = auth.actor_for_user("tenant", source="test")

    result = await kernel.execute("user_activity", {"person": "bob"}, actor=owner)
    rendered = str(result.data or "") + str(result.to_llm_message() or "")
    assert "без отметки о том, кто их загрузил" in rendered, (
        "модель не узнала, что у части загрузок автор неизвестен — и объявит, "
        f"что человек ничего не присылал: {rendered[:300]}"
    )
    # Проверяется ЧИСЛО, а не наличие фразы. Осторожное умолчание (ключа нет —
    # считаем, что безымянные есть) выдаёт ту же фразу, и первая редакция этого
    # теста мутацию не поймала: отключённый счётчик выглядел как подключённый.
    # Двойка приходит только из настоящего запроса к хранилищу.
    assert "2 материалов без отметки" in rendered, (
        f"в ответе не настоящее число безымянных загрузок: {rendered[:300]}"
    )
