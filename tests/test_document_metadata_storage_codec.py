from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from friday.agent_runtime import AgentRuntime, _document_metadata_answer, _OwnedAttachment
from friday.document_metadata_codec import (
    TECHNICAL_METADATA_TEXT_CODEC_FIELD,
    TECHNICAL_METADATA_TEXT_CODEC_VERSION,
)
from friday.ingestion import IngestionPipeline
from friday.ingestion._files import _document_metadata_projection
from friday.knowledge_graph import KnowledgeGraph
from friday.storage import PrivateMaterialQuarantineError
from friday.storage.models import Entity, EntityType, new_id


def _reportlab_pdf(metadata: dict[str, str]) -> bytes:
    source = io.BytesIO()
    canvas = Canvas(source)
    canvas.drawString(72, 720, "Same body text for metadata testing.")
    canvas.save()

    reader = PdfReader(io.BytesIO(source.getvalue()))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.add_metadata({f"/{name}": value for name, value in metadata.items()})
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def _odt_with_omitted_custom_tail(tail: str) -> bytes:
    properties = "".join(
        f'<meta:user-defined meta:name="Field {index}" meta:value-type="string">Value {index}</meta:user-defined>'
        for index in range(32)
    )
    properties += (
        '<meta:user-defined meta:name="Omitted tail" meta:value-type="string">'
        f"{escape(tail)}</meta:user-defined>"
    )
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr(
            "content.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:p>Same bounded ODT body.</text:p></office:text></office:body>
</office:document-content>""",
        )
        archive.writestr(
            "meta.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0">
 <office:meta>"""
            + properties
            + "</office:meta></office:document-meta>",
        )
    return payload.getvalue()


@pytest.mark.asyncio
async def test_real_pdf_risky_metadata_is_stored_and_rendered_exactly(settings, storage) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    payload = _reportlab_pdf(
        {
            "Title": "[Draft]",
            "Author": "{Author}",
            "Subject": '"Quoted"',
            "CustomBracket": "[Custom]",
        }
    )

    first = await pipeline.ingest_file(
        "alice",
        None,
        payload,
        filename="draft.pdf",
        mime_type="application/pdf",
        source_ref="pdf-metadata:risky:first",
        metadata={"uploaded_by": "alice"},
        force_review=True,
    )
    replay = await pipeline.ingest_file(
        "alice",
        None,
        payload + b"\n% distinct transport bytes\n",
        filename="draft-resaved.pdf",
        mime_type="application/pdf",
        source_ref="pdf-metadata:risky:replay",
        metadata={"uploaded_by": "alice"},
        force_review=True,
    )
    changed = await pipeline.ingest_file(
        "alice",
        None,
        _reportlab_pdf(
            {
                "Title": "[Final]",
                "Author": "{Author}",
                "Subject": '"Quoted"',
                "CustomBracket": "[Custom]",
            }
        ),
        filename="final.pdf",
        mime_type="application/pdf",
        source_ref="pdf-metadata:risky:changed",
        metadata={"uploaded_by": "alice"},
        force_review=True,
    )

    assert replay["raw_object_id"] == first["raw_object_id"]
    assert changed["raw_object_id"] != first["raw_object_id"]
    raw = storage.get_raw_object(first["raw_object_id"], "alice")
    assert raw is not None
    stored = json.loads(str(raw["metadata_json"]))
    assert stored[TECHNICAL_METADATA_TEXT_CODEC_FIELD] == TECHNICAL_METADATA_TEXT_CODEC_VERSION
    assert stored["title"].endswith("[Draft]")
    assert stored["creator"].endswith("{Author}")
    assert stored["subject"].endswith('"Quoted"')
    projection = _document_metadata_projection(stored)
    assert _document_metadata_projection(projection) == projection

    rendered = _document_metadata_answer([_OwnedAttachment({"_safe_document_metadata": stored})])
    assert "Заголовок: [Draft]" in rendered
    assert "Автор: {Author}" in rendered
    assert 'Тема: "Quoted"' in rendered
    assert "CustomBracket (string): [Custom]" in rendered
    assert "Keywords (string): (пустое значение)" in rendered
    assert "technical-metadata-text-v1:" not in rendered

    async def forbidden_inspection(*_args, **_kwargs):
        raise AssertionError("current metadata schema reparsed authorized bytes")

    runtime = AgentRuntime(settings, storage)
    runtime.kernel.ingestion = SimpleNamespace(inspect_file_transient=forbidden_inspection)
    owned = runtime._owned_file_attachment(  # noqa: SLF001 - current-schema hydration seam
        str(first["raw_object_id"]),
        tenant_id="alice",
        person_id="alice",
    )
    assert owned is not None
    hydrated = await runtime._hydrate_legacy_document_metadata(  # noqa: SLF001
        [owned],
        tenant_id="alice",
        person_id="alice",
    )
    assert hydrated == [owned]


@pytest.mark.asyncio
async def test_plaintext_codec_does_not_bypass_private_material_quarantine(settings, storage) -> None:
    storage.ensure_user("alice")
    private = Entity(new_id("ent"), "alice", "PRIVATE METADATA CANARY 91af", EntityType.EVENT)
    storage.create_entity(private)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, '2026-08-13T00:00:00Z', 'day', 'reminder:somebody-else',
                      '2026-08-11T00:00:00Z')""",
            (private.id, "alice"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'somebody-else', 'reminder', '2026-08-11T00:00:00Z')""",
            (private.id,),
        )

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    for index, private_marker in enumerate((private.id, private.name)):
        with pytest.raises(PrivateMaterialQuarantineError):
            await pipeline.ingest_file(
                "alice",
                None,
                _reportlab_pdf({"Title": f"[{private_marker}]"}),
                filename=f"private-{index}.pdf",
                mime_type="application/pdf",
                source_ref=f"pdf-metadata:private:{index}",
                force_review=True,
            )
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM raw_objects WHERE source_ref LIKE 'pdf-metadata:private:%'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_incomplete_equal_metadata_prefix_never_authorizes_text_dedup(settings, storage) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    first = await pipeline.ingest_file(
        "alice",
        None,
        _odt_with_omitted_custom_tail("Tail A"),
        filename="bounded-a.odt",
        source_ref="odt-metadata:bounded:a",
        force_review=True,
    )
    second = await pipeline.ingest_file(
        "alice",
        None,
        _odt_with_omitted_custom_tail("Tail B"),
        filename="bounded-b.odt",
        source_ref="odt-metadata:bounded:b",
        force_review=True,
    )

    assert first["raw_object_id"] != second["raw_object_id"]
    first_metadata = json.loads(str(storage.get_raw_object(first["raw_object_id"], "alice")["metadata_json"]))
    second_metadata = json.loads(
        str(storage.get_raw_object(second["raw_object_id"], "alice")["metadata_json"])
    )
    assert first_metadata["technical_metadata_incomplete"] is True
    assert second_metadata["technical_metadata_incomplete"] is True
    assert _document_metadata_projection(first_metadata) == _document_metadata_projection(second_metadata)
