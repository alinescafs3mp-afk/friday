"""Кто принёс материал — записывается, а неизвестное называется неизвестным.

Найдено ревью уязвимых участков 2026-08-04 и подтверждено замером на живой базе:
3295 документов из 3296 лежат под ОДНИМ идентификатором (архив общий), и признака
автора у них нет ни в столбцах, ни в метаданных. Значит надзор «что Иван присылал»
был неразрешим в принципе: поиск по человеку давал ноль всегда.

Сделано две вещи, и вторая важнее первой.

ПЕРВОЕ: все восемь дорог приёма пишут `uploaded_by` — Telegram, HTTP, URL,
веб-исследование и оба импорта. Аутентифицированные дороги пишут `actor.own_id`;
неаутентифицированный CLI принимает явное значение либо сохраняет JSON `null`.
Ключ единый, чтобы надзор не гадал, где искать, а tenant не изображал человека.

ВТОРОЕ: у материалов, принятых РАНЬШЕ, автора нет, и приписать их кому-либо
нельзя — догадка здесь означала бы приписать человеку чужие документы. Такие
строки в ответ по автору не попадают и считаются отдельно, честной строкой «без
автора». Это решение принято сознательно: «ноль у Ивана» рядом с тремя тысячами
документов неизвестного происхождения читается как «Иван ничего не присылал», и
на этом строят кадровые выводы.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import RawObject, new_id
from friday.web_surfer import FetchResult

ROOT = Path(__file__).resolve().parents[1]


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


EXPECTED_INGEST_CALLS = [
    ("friday/api/files.py", "upload_file", "ingest_file", "actor.own_id"),
    ("friday/api/ingest.py", "ingest", "ingest_text", "actor.own_id"),
    ("friday/api/ingest.py", "ingest_url", "ingest_text", "actor.own_id"),
    ("friday/bulk_import.py", "_ingest_one", "ingest_file", "uploaded_by"),
    (
        "friday/execution_kernel/__init__.py",
        "ExecutionKernel._capture_web_sources",
        "ingest_text",
        "actor.own_id",
    ),
    ("friday/organs/importer/__init__.py", "_router.run_import", "ingest_text", "actor.own_id"),
    ("friday/server.py", "create_app.chat", "ingest_file", "actor.own_id"),
    ("friday/server.py", "create_app.chat", "ingest_text", "actor.own_id"),
]


class _IngestCallVisitor(ast.NodeVisitor):
    """Name every ingestion call and the expression that supplies its uploader."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.scope: list[str] = []
        self.found: list[tuple[str, str, str, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name in {"ingest_text", "ingest_file"}:
            metadata = next((item.value for item in node.keywords if item.arg == "metadata"), None)
            uploader = "<metadata is not a literal dict>"
            if isinstance(metadata, ast.Dict):
                uploader = "<uploaded_by is missing>"
                for key, value in zip(metadata.keys, metadata.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "uploaded_by":
                        uploader = ast.unparse(value)
                        break
            self.found.append(
                (
                    str(self.path.relative_to(ROOT)),
                    ".".join(self.scope),
                    name,
                    uploader,
                )
            )
        self.generic_visit(node)


def _ingest_calls_with_uploaders() -> list[tuple[str, str, str, str]]:
    found: list[tuple[str, str, str, str]] = []
    for path in (ROOT / "friday").rglob("*.py"):
        visitor = _IngestCallVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.extend(visitor.found)
    return sorted(found)


def test_every_intake_road_records_the_uploader() -> None:
    """Полный переучёт вызовов, а не поиск слова по нескольким модулям.

    Прежний тест был зелёным, когда четыре из восьми дорог не писали автора:
    достаточно было встретить `uploaded_by` где-нибудь в том же модуле. Здесь
    новая дорога, перенос вызова или неверный `actor.user_id` меняют точную
    матрицу и требуют осознанного решения.

    Неаутентифицированный дисковый импорт — единственное исключение: значение
    приходит параметром и может быть `None`. Это явное «неизвестно», а не
    выдуманный из арендатора человек.
    """
    assert _ingest_calls_with_uploaders() == sorted(EXPECTED_INGEST_CALLS)


def _raw_metadata(storage, raw_id: str, user_id: str) -> dict:
    raw = storage.get_raw_object(raw_id, user_id)
    assert raw is not None, raw_id
    metadata = raw.get("metadata_json") or {}
    return json.loads(metadata) if isinstance(metadata, str) else dict(metadata)


def test_authenticated_intake_roads_record_the_person_not_the_shared_tenant(settings, tmp_path) -> None:
    """Все семь actor-aware дорог исполняются с различными tenant/person.

    Обычный owner-token этого не доказывает: у него оба идентификатора равны.
    Мутация `actor.own_id -> actor.user_id` на любой дороге должна дать общий
    tenant вместо человека и покрасить соответствующий элемент матрицы.
    """
    from friday.bulk_import import plan_import, run_import

    person_id = "person-forward-author"
    shared = dataclasses.replace(settings, shared_archive=True)
    app = create_app(shared)

    class Surfer:
        async def fetch(self, _url: str, **_kwargs: object) -> FetchResult:
            text = "Синтетическая страница для проверки автора загрузки. " * 8
            return FetchResult(
                url="https://example.test/direct",
                title="Прямая страница",
                text=text,
                text_length=len(text),
                status_code=200,
            )

        async def research(self, query: str, *, max_sources: int = 3) -> dict:
            del max_sources
            return {
                "query": query,
                "sources": [
                    {
                        "url": "https://example.test/research",
                        "title": "Найденная страница",
                        "text": "Синтетический результат веб-поиска с явным автором. " * 8,
                    }
                ],
                "summary": "ok",
            }

    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {shared.api_token}"}
        created = client.post(
            "/api/admin/users",
            json={"id": person_id, "preset_key": "admin"},
            headers=owner,
        )
        assert created.status_code < 300, created.text
        issued = client.post("/api/admin/tokens", json={"user_id": person_id}, headers=owner)
        assert issued.status_code == 200, issued.text
        person = {"Authorization": f"Bearer {issued.json()['token']}"}

        surfer = Surfer()
        app.state.web_surfer = surfer
        app.state.kernel.web_surfer = surfer

        async def quiet_chat(*_args, **_kwargs):
            return {"conversation_id": "conv-author-matrix", "content": "ok"}

        app.state.agent.chat = quiet_chat

        raw_ids: dict[str, str] = {}
        pasted = client.post(
            "/api/ingest",
            json={"content": "Текстовая проверка полного маршрута авторства." * 4},
            headers=person,
        )
        assert pasted.status_code == 200, pasted.text
        raw_ids["api text"] = pasted.json()["raw_object_id"]

        uploaded = client.post(
            "/api/files",
            files={
                "file": (
                    "author-matrix.txt",
                    ("Содержимое файла для полной проверки автора. " * 5).encode(),
                    "text/plain",
                )
            },
            headers=person,
        )
        assert uploaded.status_code == 200, uploaded.text
        raw_ids["api file"] = uploaded.json()["raw_object_id"]

        by_url = client.post(
            "/api/ingest/url",
            json={"url": "https://example.test/direct"},
            headers=person,
        )
        assert by_url.status_code == 200, by_url.text
        raw_ids["api url"] = by_url.json()["raw_object_id"]

        chat_text = client.post(
            "/api/chat",
            json={
                "message": "Сообщение в чате для проверки автора поступления.",
                "source_ref": "author-matrix:chat-text",
                "enable_tools": False,
            },
            headers=person,
        )
        assert chat_text.status_code == 200, chat_text.text
        assert "raw_object_id" not in chat_text.json()["ingestion"]
        chat_text_raw = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE user_id=? AND source_ref=?",
            (LEGACY_OWNER_USER_ID, "author-matrix:chat-text"),
        ).fetchone()
        assert chat_text_raw is not None
        raw_ids["chat text"] = str(chat_text_raw["id"])

        chat_file = client.post(
            "/api/chat",
            json={
                "message": "",
                "source_ref": "author-matrix:chat-file",
                "enable_tools": False,
                "document": {
                    "filename": "chat-author.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(
                        ("Файл из чата для проверки автора. " * 5).encode()
                    ).decode(),
                },
            },
            headers=person,
        )
        assert chat_file.status_code == 200, chat_file.text
        assert "raw_object_id" not in chat_file.json()["file_ingestion"]
        chat_file_raw = app.state.storage.execute(
            """SELECT id FROM raw_objects
               WHERE user_id=? AND content_type='file'
                 AND json_extract(metadata_json,'$.uploaded_by')=?
                 AND json_extract(metadata_json,'$.filename')='chat-author.txt'
               ORDER BY rowid DESC LIMIT 1""",
            (LEGACY_OWNER_USER_ID, person_id),
        ).fetchone()
        assert chat_file_raw is not None
        raw_ids["chat file"] = str(chat_file_raw["id"])

        actor = app.state.auth_service.actor_for_user(person_id, source="test")
        researched = asyncio.run(
            app.state.kernel.execute("web_research", {"query": "синтетическая проверка"}, actor=actor)
        )
        assert researched.success, researched.error
        raw_ids["web research"] = researched.data["captured"][0]["raw_object_id"]

        calendar = "\r\n".join(
            (
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "UID:author-matrix@example.test",
                "DTSTART:20260806",
                "SUMMARY:Проверка автора импорта",
                "END:VEVENT",
                "END:VCALENDAR",
            )
        )
        imported = client.post(
            "/api/import",
            files={"file": ("author-matrix.ics", calendar.encode(), "text/calendar")},
            headers=person,
        )
        assert imported.status_code == 200, imported.text
        organ_raw = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE user_id=? AND source_ref=?",
            (LEGACY_OWNER_USER_ID, "ics:author-matrix@example.test"),
        ).fetchone()
        assert organ_raw is not None
        raw_ids["organ import"] = str(organ_raw["id"])

        disk_file = tmp_path / "known-author.txt"
        disk_file.write_text("Файл CLI с явно названным автором. " * 5, encoding="utf-8")
        disk = asyncio.run(
            run_import(
                app.state.ingestion,
                LEGACY_OWNER_USER_ID,
                plan_import(disk_file, max_bytes=shared.max_upload_bytes),
                uploaded_by=person_id,
            )
        )
        assert len(disk) == 1 and disk[0].status == "ingested"
        raw_ids["disk import"] = disk[0].raw_object_id

        assert set(raw_ids) == {
            "api text",
            "api file",
            "api url",
            "chat text",
            "chat file",
            "web research",
            "organ import",
            "disk import",
        }
        for road, raw_id in raw_ids.items():
            metadata = _raw_metadata(app.state.storage, raw_id, LEGACY_OWNER_USER_ID)
            assert metadata.get("uploaded_by") == person_id, (
                f"{road}: записан арендатор или неизвестность вместо человека: {metadata}"
            )


def test_disk_import_without_an_authenticated_actor_records_explicit_unknown(
    settings, storage, tmp_path
) -> None:
    """CLI не приписывает материал целевому tenant по догадке.

    `run_import` остаётся обратно совместимым: существующие вызовы без нового
    аргумента работают, но кладут JSON `null`. Явный `--uploaded-by` доступен
    тому, кто действительно знает человека; это не исторический backfill.
    """
    from friday.bulk_import import plan_import, run_import
    from friday.cli import build_parser
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    path = tmp_path / "unknown-author.txt"
    path.write_text("Материал, чей автор при запуске CLI неизвестен. " * 5, encoding="utf-8")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    outcome = asyncio.run(
        run_import(pipeline, "shared-tenant", plan_import(path, max_bytes=settings.max_upload_bytes))
    )[0]

    metadata = _raw_metadata(storage, outcome.raw_object_id, "shared-tenant")
    assert "uploaded_by" in metadata, "неизвестность снова представлена отсутствующим контрактом"
    assert metadata["uploaded_by"] is None, "целевой tenant выдуман как автор"

    attributed = asyncio.run(
        run_import(
            pipeline,
            "shared-tenant",
            plan_import(path, max_bytes=settings.max_upload_bytes),
            uploaded_by="person-named-too-late",
        )
    )[0]
    # Exact uploader provenance is also the ownership boundary for later
    # conversation pointers.  A previously explicit-unknown Raw Object cannot
    # be borrowed by a now-named person merely because its bytes match.  Keep
    # the unknown row unchanged and create a distinct attributed provenance
    # row over the same content-addressed bytes.
    assert attributed.status == "ingested"
    assert attributed.raw_object_id != outcome.raw_object_id
    assert _raw_metadata(storage, outcome.raw_object_id, "shared-tenant")["uploaded_by"] is None, (
        "повторный импорт незаметно превратился в исторический backfill"
    )
    assert (
        _raw_metadata(storage, attributed.raw_object_id, "shared-tenant")["uploaded_by"]
        == "person-named-too-late"
    )

    parsed = build_parser().parse_args(
        ["import", str(path), "--uploaded-by", "person-who-really-ran-the-import"]
    )
    assert parsed.uploaded_by == "person-who-really-ran-the-import"


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
