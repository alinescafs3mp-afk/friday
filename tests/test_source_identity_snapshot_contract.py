"""Same-snapshot authority for source excerpts and reparsed file bytes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import (
    _OWNED_SAFE_DOCUMENT_METADATA,
    _RAW_SOURCE_IDENTITY_KEY,
    AgentRuntime,
    _OwnedAttachment,
)
from friday.source_identity import (
    authorized_file_snapshot_token,
    raw_source_identity_sha256,
    source_search_page_snapshots,
)
from tests.test_attachment_publication_reauthorization import (
    _SOURCE_CANARY as _ATTACHMENT_SOURCE_CANARY,
)
from tests.test_attachment_publication_reauthorization import (
    _actor,
    _assert_failed_closed,
    _ingest,
    _MutatingReviewModel,
)
from tests.test_source_search_publication_reauthorization import (
    _FOCUS,
    _OWNER,
    _QUERY,
    _REQUEST,
    _SOURCE_CANARY,
    _assert_source_publication_failed_closed,
    _DeterministicSourceModel,
    _EmptySearcher,
    _runtime,
    _seed_source,
)


def _force_current_parser_recovery(storage: Any, raw_id: str, filename: str) -> None:
    with storage.transaction() as connection:
        row = connection.execute(
            "SELECT metadata_json FROM raw_objects WHERE id=?",
            (raw_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row["metadata_json"]))
        metadata["extraction_success"] = False
        metadata["extraction_chars"] = 0
        cursor = connection.execute(
            "UPDATE raw_objects SET raw_content=?, metadata_json=? WHERE id=?",
            (
                f"[File: {filename}]",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                raw_id,
            ),
        )
        assert cursor.rowcount == 1


def _successful_parser_result(text: str) -> dict[str, Any]:
    return {
        "_runtime_source_text": text,
        "text_preview": text,
        "extraction_success": True,
        "advisory_only": False,
        "parse_deadline_reached": False,
        "parse_pages_read": 0,
        "parse_pages_truncated": False,
        "parse_total_pages": 0,
        "archive_truncated": False,
        "source_truncated_for_parse": False,
    }


@pytest.mark.asyncio
async def test_metadata_parser_rejects_old_header_after_same_id_identity_change(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_metadata_snapshot_race"
    body = b"legacy metadata bytes"
    content_sha256 = hashlib.sha256(body).hexdigest()

    def raw_projection(marker: str) -> dict[str, str]:
        return {
            "id": raw_id,
            "source": "upload",
            "source_ref": "metadata-snapshot-race",
            "content_type": "file",
            "received_at": "2026-08-14T00:00:00+00:00",
            "content_hash": content_sha256,
            "_raw_content": marker,
            "_raw_metadata": "{}",
        }

    before = raw_projection("before")
    after = raw_projection("after")
    current = before

    def canonical(*_args: Any, **_kwargs: Any) -> _OwnedAttachment:
        return _OwnedAttachment(
            {
                "raw_object_id": raw_id,
                "filename": "legacy.doc",
                "mime_type": "application/msword",
                "transient_text": "",
                "extraction_success": False,
                "_registered_file_record": "valid",
                _RAW_SOURCE_IDENTITY_KEY: raw_source_identity_sha256(current),
            }
        )

    parser_calls = 0

    async def inspect_then_replace(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal current, parser_calls
        parser_calls += 1
        current = after
        return {
            "_document_metadata": {
                "format": "doc",
                "title": "STALE-METADATA-CANARY",
            }
        }

    token = authorized_file_snapshot_token(before, content_sha256=content_sha256)
    assert token is not None
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.storage = object()
    runtime.settings = SimpleNamespace(
        files_dir=settings.files_dir,
        max_upload_bytes=settings.max_upload_bytes,
    )
    runtime.kernel = SimpleNamespace(ingestion=SimpleNamespace(inspect_file_transient=inspect_then_replace))
    runtime._owned_file_attachment = canonical  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(
        "friday.agent_runtime.read_authorized_file",
        lambda *_args, **_kwargs: SimpleNamespace(
            content=body,
            filename="legacy.doc",
            mime_type="application/msword",
            snapshot_token=token,
        ),
    )
    original = canonical()

    hydrated = await runtime._hydrate_legacy_document_metadata(  # noqa: SLF001
        [original],
        tenant_id="alice",
        person_id="alice",
    )

    assert parser_calls == 1
    assert hydrated == [original]
    assert _OWNED_SAFE_DOCUMENT_METADATA not in hydrated[0]


@pytest.mark.asyncio
async def test_source_search_stamp_is_private_and_unchanged_page_publishes(
    settings: Any,
    storage: Any,
) -> None:
    raw = _seed_source(storage)
    model = _DeterministicSourceModel()
    runtime, kernel, actor = _runtime(settings, storage, model)

    tool_result = await kernel.execute(
        "source_search",
        {"query": _QUERY, "focus": _FOCUS, "limit": 10},
        actor=actor,
    )
    snapshots = source_search_page_snapshots(tool_result.data)
    assert tool_result.success is True
    assert snapshots is not None and len(snapshots) == 1
    assert snapshots[0].raw_id == raw.id
    private_identity = snapshots[0].identity_sha256
    assert private_identity not in json.dumps(tool_result.data, ensure_ascii=False, sort_keys=True)
    assert private_identity not in tool_result.to_llm_message()
    assert private_identity not in json.dumps(tool_result.to_dict(), ensure_ascii=False, sort_keys=True)

    kernel.calls.clear()
    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert len(model.calls) == 1
    assert _SOURCE_CANARY in response["message"]
    assert response["source_search_authority_changed_before_publication"] is False
    assert private_identity not in json.dumps(response, ensure_ascii=False, sort_keys=True)


@pytest.mark.asyncio
async def test_source_search_rejects_excerpt_if_raw_changes_after_kernel_snapshot(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _seed_source(storage)
    model = _DeterministicSourceModel()
    runtime, kernel, actor = _runtime(settings, storage, model)
    execute = kernel.execute

    async def change_after_source_read(
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
        execution_scope: str = "dialogue",
    ) -> Any:
        result = await execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )
        if name == "source_search" and result.success:
            with storage.transaction() as connection:
                cursor = connection.execute(
                    "UPDATE raw_objects SET raw_content=? WHERE id=?",
                    ("Сведения отозваны; старой должности здесь нет.", raw.id),
                )
            assert cursor.rowcount == 1
        return result

    monkeypatch.setattr(kernel, "execute", change_after_source_read)
    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert model.calls == []
    _assert_source_publication_failed_closed(storage, response)


@pytest.mark.asyncio
async def test_current_parser_accepts_unchanged_same_read_snapshot(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    filename = "parser-snapshot-positive.md"
    ingested = await _ingest(
        settings,
        storage,
        filename=filename,
        suffix="parser-snapshot-positive",
    )
    raw_id = str(ingested["raw_object_id"])
    _force_current_parser_recovery(storage, raw_id, filename)
    model = _MutatingReviewModel()
    runtime = __import__("friday.agent_runtime", fromlist=["AgentRuntime"]).AgentRuntime(
        settings,
        storage,
        llm=model,
    )
    parser_calls = 0

    async def inspect_unchanged(content: bytes, **kwargs: Any) -> dict[str, Any]:
        nonlocal parser_calls
        del kwargs
        parser_calls += 1
        text = content.decode("utf-8")
        assert _ATTACHMENT_SOURCE_CANARY in text
        return _successful_parser_result(text)

    monkeypatch.setattr(
        runtime.kernel,
        "ingestion",
        SimpleNamespace(inspect_file_transient=inspect_unchanged),
    )
    response = await runtime.chat(
        "alice",
        "о чём речь в этом файле?",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        reply_assistant_reference=True,
    )

    assert parser_calls == 1
    assert len(model.calls) == 1
    assert _ATTACHMENT_SOURCE_CANARY in response["message"]
    assert response["attachment_authority_changed_before_publication"] is False


@pytest.mark.asyncio
async def test_current_parser_rejects_old_output_after_registration_replacement(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    filename = "parser-snapshot-negative.md"
    ingested = await _ingest(
        settings,
        storage,
        filename=filename,
        suffix="parser-snapshot-negative",
    )
    raw_id = str(ingested["raw_object_id"])
    _force_current_parser_recovery(storage, raw_id, filename)
    model = _MutatingReviewModel()
    runtime = __import__("friday.agent_runtime", fromlist=["AgentRuntime"]).AgentRuntime(
        settings,
        storage,
        llm=model,
    )

    async def inspect_then_replace(content: bytes, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        old_text = content.decode("utf-8")
        assert _ATTACHMENT_SOURCE_CANARY in old_text
        replacement = b"# Replacement\n\nThe prior registered source was withdrawn.\n"
        replacement_digest = hashlib.sha256(replacement).hexdigest()
        with storage.transaction() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM raw_objects WHERE id=?",
                (raw_id,),
            ).fetchone()
            assert row is not None
            metadata = json.loads(str(row["metadata_json"]))
            stored_path = Path(settings.files_dir) / str(metadata["stored_path"])
            stored_path.write_bytes(replacement)
            metadata.update(
                {
                    "sha256": replacement_digest,
                    "size_bytes": len(replacement),
                    "extraction_success": False,
                    "extraction_chars": 0,
                }
            )
            cursor = connection.execute(
                """UPDATE raw_objects
                   SET raw_content=?, content_hash=?, metadata_json=?
                   WHERE id=?""",
                (
                    "[File: replacement.md]",
                    replacement_digest,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    raw_id,
                ),
            )
            assert cursor.rowcount == 1
        return _successful_parser_result(old_text)

    monkeypatch.setattr(
        runtime.kernel,
        "ingestion",
        SimpleNamespace(inspect_file_transient=inspect_then_replace),
    )
    response = await runtime.chat(
        "alice",
        "о чём речь в этом файле?",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        reply_assistant_reference=True,
    )

    assert model.calls == []
    _assert_failed_closed(storage, response, expected_count=1)
