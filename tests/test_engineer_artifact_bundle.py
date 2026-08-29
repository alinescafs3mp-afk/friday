"""Exact, non-executing Engineer artifact bundles and publication."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import time
import zipfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from friday.file_delivery import read_authorized_file
from friday.generated_files import (
    GeneratedFilesPersistenceRollbackGuard,
    generated_files_publication_transaction,
    validate_generated_files_persistence_attestation,
)
from friday.organs.engineer.bundles import (
    BUNDLE_MANIFEST_SCHEMA,
    BUNDLE_RECEIPT_SCHEMA,
    EngineerArtifactBundleError,
    build_engineer_artifact_delivery,
    bundle_source_lineage,
    generated_bundle_source,
    produced_artifact,
    reauthorize_bundle_sources_in_transaction,
)
from friday.organs.engineer.publication import (
    ExactGeneratedFilePublicationError,
    exact_generated_file_batch,
    persist_exact_generated_file_batch,
)
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService
from friday.storage.models import RawObject, new_id


def _tool_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "bounded test tool",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


def _schema_names(values: list[dict[str, Any]]) -> set[str]:
    return {str((item.get("function") or {}).get("name") or "") for item in values}


class _ToolSchemaSpy:
    enabled = True
    total_budget_sec = 2.0

    def __init__(self) -> None:
        self.schemas: list[list[dict[str, Any]]] = []

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.schemas.append([dict(item) for item in (tools or [])])
        return {
            "content": "SCHEMA-CONTRACT-OK",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _store_file(settings, storage, *, filename: str, body: bytes) -> str:  # noqa: ANN001
    digest = hashlib.sha256(body).hexdigest()
    relative = f"{LEGACY_OWNER_USER_ID}/{digest[:2]}/{digest}.bin"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    raw = RawObject(
        id=new_id("raw"),
        user_id=LEGACY_OWNER_USER_ID,
        source="upload",
        source_ref=filename,
        raw_content="",
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": filename,
            "uploaded_by": LEGACY_OWNER_USER_ID,
            "stored_path": relative,
            "mime_type": "text/x-java-source",
            "size_bytes": len(body),
            "sha256": digest,
        },
        received_at="2026-08-26T00:00:00+00:00",
        created_at="2026-08-26T00:00:00+00:00",
    )
    storage.store_raw_object(raw)
    return raw.id


def _source_lineage(settings, storage, raw_id: str):  # noqa: ANN001
    stored = read_authorized_file(
        storage,
        settings.files_dir,
        raw_id,
        LEGACY_OWNER_USER_ID,
        person_id=LEGACY_OWNER_USER_ID,
        max_bytes=1024 * 1024,
    )
    return bundle_source_lineage(stored)


def _reauthorize(settings, storage, lineages):  # noqa: ANN001
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    authorization = AuthorizationService(storage)
    with storage.transaction() as conn:
        return reauthorize_bundle_sources_in_transaction(
            conn,
            files_root=settings.files_dir,
            authorization=authorization,
            actor=actor,
            tenant_id=LEGACY_OWNER_USER_ID,
            lineages=lineages,
            max_bytes=1024 * 1024,
        )


def test_bundle_is_byte_deterministic_canonical_and_never_claims_execution(settings, storage) -> None:
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    first_id = _store_file(settings, storage, filename="Main.java", body=b"class Main {}\n")
    second_id = _store_file(settings, storage, filename="Config.txt", body=b"mode=safe\n")
    first = _source_lineage(settings, storage, first_id)
    second = _source_lineage(settings, storage, second_id)
    sources = _reauthorize(settings, storage, (first, second))
    parents = (first.content_sha256, second.content_sha256)
    jar = produced_artifact(
        filename="Friday Test.jar",
        content=b"PK\x03\x04deterministic-library-jar",
        mime_type="application/java-archive",
        role="binary",
        parent_sha256s=parents,
        tool_name="javac",
        tool_version="21.0.12",
        verification_checks=("class_major_65", "zip_structure_valid"),
    )

    delivery = build_engineer_artifact_delivery(
        sources=sources,
        artifacts=(jar,),
        bundle_name="Friday Test build",
        operation="compile",
        max_bundle_bytes=4 * 1024 * 1024,
    )
    repeated = build_engineer_artifact_delivery(
        sources=tuple(reversed(sources)),
        artifacts=(jar,),
        bundle_name="Friday Test build",
        operation="compile",
        max_bundle_bytes=4 * 1024 * 1024,
    )

    assert delivery.bundle.payload == repeated.bundle.payload
    assert delivery.bundle.sha256 == hashlib.sha256(delivery.bundle.payload).hexdigest()
    assert [item["filename"] for item in delivery.attachments] == [
        "Friday Test.jar",
        "Friday Test build.engineer-bundle.zip",
    ]
    assert base64.b64decode(delivery.attachments[0]["content_base64"], validate=True) == jar.content
    assert (
        base64.b64decode(delivery.attachments[1]["content_base64"], validate=True) == delivery.bundle.payload
    )

    with zipfile.ZipFile(BytesIO(delivery.bundle.payload)) as archive:
        names = archive.namelist()
        assert names[:2] == ["MANIFEST.json", "RECEIPT.json"]
        assert len(names) == len(set(name.casefold() for name in names)) == 5
        assert all(not name.startswith(("/", "\\")) and ".." not in Path(name).parts for name in names)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
        assert all(stat.S_IMODE(info.external_attr >> 16) == 0o644 for info in archive.infolist())
        source_payloads = {archive.read(name) for name in names if name.startswith("sources/")}
        assert source_payloads == {b"class Main {}\n", b"mode=safe\n"}
        artifact_path = next(name for name in names if name.startswith("artifacts/"))
        assert archive.read(artifact_path) == jar.content
        manifest = json.loads(archive.read("MANIFEST.json"))
        receipt = json.loads(archive.read("RECEIPT.json"))

    assert manifest["schema"] == BUNDLE_MANIFEST_SCHEMA
    assert receipt["schema"] == BUNDLE_RECEIPT_SCHEMA
    assert (
        receipt["manifest_sha256"]
        == hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        ).hexdigest()
    )
    assert receipt["sample_executed"] is False
    assert receipt["network"] == "none"
    assert receipt["runtime_validation"] == "not_performed"
    serialized = json.dumps((manifest, receipt), ensure_ascii=False)
    assert "raw_" not in serialized
    assert str(settings.files_dir) not in serialized


def test_bundle_names_are_safe_and_payload_limits_are_enforced(settings, storage) -> None:
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    raw_id = _store_file(settings, storage, filename="../danger/../Main.java", body=b"class X {}")
    lineage = _source_lineage(settings, storage, raw_id)
    source = _reauthorize(settings, storage, (lineage,))[0]
    artifact = produced_artifact(
        filename="../../build\x00.jar",
        content=b"artifact",
        mime_type="application/java-archive",
        role="binary",
        parent_sha256s=(lineage.content_sha256,),
        tool_name="javac",
        tool_version="21",
        verification_checks=("class_major_65",),
    )
    delivery = build_engineer_artifact_delivery(
        sources=(source,),
        artifacts=(artifact,),
        bundle_name="../../release\x00",
        operation="compile",
        max_bundle_bytes=1024 * 1024,
    )

    assert delivery.bundle.filename == "release_.engineer-bundle.zip"
    assert delivery.attachments[0]["filename"] == "build_.jar"
    with zipfile.ZipFile(BytesIO(delivery.bundle.payload)) as archive:
        assert all(".." not in Path(name).parts for name in archive.namelist())
        assert not any("\\" in name or "\x00" in name for name in archive.namelist())

    with pytest.raises(EngineerArtifactBundleError, match="bundle_size_limit"):
        build_engineer_artifact_delivery(
            sources=(source,),
            artifacts=(artifact,),
            bundle_name="release",
            operation="compile",
            max_bundle_bytes=32,
        )

    delivery_bytes = len(delivery.bundle.payload) + len(artifact.content)
    with pytest.raises(EngineerArtifactBundleError, match="bundle_size_limit"):
        build_engineer_artifact_delivery(
            sources=(source,),
            artifacts=(artifact,),
            bundle_name="release",
            operation="compile",
            max_bundle_bytes=delivery_bytes - 1,
        )


def test_same_turn_generated_source_keeps_instruction_lineage_without_raw_upload() -> None:
    instruction = "Напиши Main.java и сразу собери"
    message_id = "msg_0123456789abcdef"
    source = generated_bundle_source(
        filename="Main.java",
        content=b"public class Main { public static void main(String[] a) {} }\n",
        mime_type="text/x-java-source",
        origin_user_message_id=message_id,
        instruction_sha256=hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        producer="engineer.source.write",
    )
    artifact = produced_artifact(
        filename="Main.jar",
        content=b"PK\x03\x04compiled-generated-source",
        mime_type="application/java-archive",
        role="binary",
        parent_sha256s=(source.content_sha256,),
        tool_name="javac",
        tool_version="21",
        verification_checks=("class_major_65",),
    )
    delivery = build_engineer_artifact_delivery(
        sources=(source,),
        artifacts=(artifact,),
        bundle_name="Main",
        operation="compile",
        max_bundle_bytes=1024 * 1024,
    )

    source_receipt = delivery.bundle.manifest["sources"][0]
    assert source_receipt["origin_kind"] == "generated"
    assert source_receipt["instruction_sha256"] == hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    assert (
        source_receipt["origin_user_message_sha256"] == hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    )
    serialized = json.dumps(delivery.bundle.manifest, ensure_ascii=False)
    assert message_id not in serialized
    assert "raw_" not in serialized


def test_source_reauthorization_rejects_revocation_and_changed_lineage(settings, storage) -> None:
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    raw_id = _store_file(settings, storage, filename="Main.java", body=b"class Main {}")
    lineage = _source_lineage(settings, storage, raw_id)
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    authorization = AuthorizationService(storage)
    storage.set_permission_override(LEGACY_OWNER_USER_ID, "files.read", "deny")

    with (
        storage.transaction() as conn,
        pytest.raises(EngineerArtifactBundleError, match="source_authority_denied"),
    ):
        reauthorize_bundle_sources_in_transaction(
            conn,
            files_root=settings.files_dir,
            authorization=authorization,
            actor=actor,
            tenant_id=LEGACY_OWNER_USER_ID,
            lineages=(lineage,),
            max_bytes=1024 * 1024,
        )

    storage.set_permission_override(LEGACY_OWNER_USER_ID, "files.read", "allow")
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET source_ref='changed.java' WHERE id=?",
            (raw_id,),
        )
    with (
        storage.transaction() as conn,
        pytest.raises(EngineerArtifactBundleError, match="source_lineage_changed"),
    ):
        reauthorize_bundle_sources_in_transaction(
            conn,
            files_root=settings.files_dir,
            authorization=authorization,
            actor=actor,
            tenant_id=LEGACY_OWNER_USER_ID,
            lineages=(lineage,),
            max_bytes=1024 * 1024,
        )


@pytest.mark.parametrize(
    ("tenant_id", "storage_owner_id"),
    (
        ("other-tenant", LEGACY_OWNER_USER_ID),
        (LEGACY_OWNER_USER_ID, "other-storage-owner"),
    ),
)
def test_source_reauthorization_rejects_lineage_outside_exact_storage_scope(
    settings,
    storage,
    tenant_id: str,
    storage_owner_id: str,
) -> None:
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    raw_id = _store_file(settings, storage, filename="Main.java", body=b"class Main {}")
    lineage = _source_lineage(settings, storage, raw_id)
    wrong_scope = replace(
        lineage,
        snapshot_token=replace(
            lineage.snapshot_token,
            tenant_id=tenant_id,
            storage_owner_id=storage_owner_id,
        ),
    )

    with (
        storage.transaction() as conn,
        pytest.raises(EngineerArtifactBundleError, match="source_lineage_invalid"),
    ):
        reauthorize_bundle_sources_in_transaction(
            conn,
            files_root=settings.files_dir,
            authorization=AuthorizationService(storage),
            actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
            tenant_id=LEGACY_OWNER_USER_ID,
            lineages=(wrong_scope,),
            max_bytes=1024 * 1024,
        )


def test_exact_multi_output_publication_persists_order_bytes_and_attestation(settings, storage) -> None:
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    raw_id = _store_file(settings, storage, filename="Main.java", body=b"class Main {}")
    lineage = _source_lineage(settings, storage, raw_id)
    source = _reauthorize(settings, storage, (lineage,))[0]
    artifact = produced_artifact(
        filename="Main.jar",
        content=b"PK\x03\x04compiled",
        mime_type="application/java-archive",
        role="binary",
        parent_sha256s=(lineage.content_sha256,),
        tool_name="javac",
        tool_version="21",
        verification_checks=("class_major_65",),
    )
    delivery = build_engineer_artifact_delivery(
        sources=(source,),
        artifacts=(artifact,),
        bundle_name="Main",
        operation="compile",
        max_bundle_bytes=1024 * 1024,
    )
    batch = exact_generated_file_batch(delivery.attachments, max_bytes=2 * 1024 * 1024)
    conversation = storage.create_conversation(LEGACY_OWNER_USER_ID, "engineer bundle")
    guard = GeneratedFilesPersistenceRollbackGuard(settings.files_dir)

    with generated_files_publication_transaction(storage, guard):
        assistant = storage.store_message(
            conversation["id"],
            LEGACY_OWNER_USER_ID,
            "assistant",
            "Сборка готова.",
        )
        publication = persist_exact_generated_file_batch(
            storage,
            settings.files_dir,
            {"message_id": assistant["id"], "files": list(delivery.attachments)},
            batch,
            tenant_id=LEGACY_OWNER_USER_ID,
            person_id=LEGACY_OWNER_USER_ID,
            max_bytes=2 * 1024 * 1024,
            rollback_guard=guard,
        )

    files = publication.response["files"]
    assert [item["filename"] for item in files] == ["Main.jar", "Main.engineer-bundle.zip"]
    assert [base64.b64decode(item["content_base64"], validate=True) for item in files] == [
        artifact.content,
        delivery.bundle.payload,
    ]
    assert len({item["id"] for item in files}) == 2
    assert validate_generated_files_persistence_attestation(
        storage,
        publication.response,
        publication.attestation,
        tenant_id=LEGACY_OWNER_USER_ID,
        person_id=LEGACY_OWNER_USER_ID,
    )


def test_exact_publication_rejects_reordered_or_changed_output_before_writes(settings, storage) -> None:
    payloads = (
        {
            "kind": "document",
            "filename": "one.bin",
            "mime_type": "application/octet-stream",
            "content_base64": base64.b64encode(b"one").decode("ascii"),
        },
        {
            "kind": "document",
            "filename": "two.zip",
            "mime_type": "application/zip",
            "content_base64": base64.b64encode(b"two").decode("ascii"),
        },
    )
    batch = exact_generated_file_batch(payloads, max_bytes=100)
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    conversation = storage.create_conversation(LEGACY_OWNER_USER_ID, "tampered bundle")
    assistant = storage.store_message(conversation["id"], LEGACY_OWNER_USER_ID, "assistant", "Готово.")
    guard = GeneratedFilesPersistenceRollbackGuard(settings.files_dir)

    with (
        pytest.raises(ExactGeneratedFilePublicationError, match="generated_batch_changed"),
        generated_files_publication_transaction(storage, guard),
    ):
        persist_exact_generated_file_batch(
            storage,
            settings.files_dir,
            {"message_id": assistant["id"], "files": list(reversed(payloads))},
            batch,
            tenant_id=LEGACY_OWNER_USER_ID,
            person_id=LEGACY_OWNER_USER_ID,
            max_bytes=100,
            rollback_guard=guard,
        )

    count = storage.execute(
        "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
    ).fetchone()[0]
    assert count == 0


def test_engineer_projection_preserves_requested_file_archive_and_web_tools_only() -> None:
    from friday.agent_runtime import _project_engineer_tool_schemas, file_turn_authority

    schemas = [
        _tool_schema(name)
        for name in (
            "engineer_local_tools",
            "make_file",
            "collect_files",
            "web_search",
            "web_fetch",
            "web_research",
            "memory_save",
        )
    ]

    report = _project_engineer_tool_schemas(
        schemas,
        authority=file_turn_authority("Сделай отчёт в PDF по результатам аудита."),
    )
    archive = _project_engineer_tool_schemas(
        schemas,
        authority=file_turn_authority("Собери все загруженные файлы за 26 число в архив."),
    )
    web = _project_engineer_tool_schemas(
        schemas,
        authority=file_turn_authority("Найди в интернете актуальную документацию Python."),
    )
    ordinary = _project_engineer_tool_schemas(
        schemas,
        authority=file_turn_authority("Обсудим архитектуру этого приложения."),
    )

    assert _schema_names(report) == {"engineer_local_tools", "make_file"}
    assert _schema_names(archive) == {"engineer_local_tools", "collect_files"}
    assert _schema_names(web) == {
        "engineer_local_tools",
        "web_search",
        "web_fetch",
        "web_research",
    }
    assert _schema_names(ordinary) == {"engineer_local_tools"}


@pytest.mark.asyncio
async def test_direct_engineer_loop_offers_public_web_but_not_for_a_private_source(
    settings,
    storage,
) -> None:
    from friday.agent_runtime import AgentContext, AgentRuntime

    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    storage.ensure_user(actor.own_id, preset_key="owner")
    public_model = _ToolSchemaSpy()
    public_runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        llm=public_model,  # type: ignore[arg-type]
    )
    public_context = AgentContext(
        conversation_id="conv-engineer-public-web",
        user_id=actor.user_id,
        person_id=actor.own_id,
        interaction_mode="engineer",
        turn_deadline=time.monotonic() + 10.0,
        engineer_dossier={"targets": [], "hosts": [], "artifacts": []},
    )

    await public_runtime._agentic_loop(  # noqa: SLF001
        public_context,
        "Найди в интернете актуальную документацию Python.",
        actor,
        tools=[_tool_schema("web_search"), _tool_schema("make_file")],
        attachments=None,
    )

    assert public_model.schemas
    assert _schema_names(public_model.schemas[0]) == {"web_search"}

    private_model = _ToolSchemaSpy()
    private_runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        llm=private_model,  # type: ignore[arg-type]
    )
    private_context = AgentContext(
        conversation_id="conv-engineer-private-web",
        user_id=actor.user_id,
        person_id=actor.own_id,
        interaction_mode="engineer",
        private_source_boundary_active=True,
        current_attachment_present=True,
        turn_deadline=time.monotonic() + 10.0,
        engineer_dossier={"targets": [], "hosts": [], "artifacts": []},
    )

    await private_runtime._agentic_loop(  # noqa: SLF001
        private_context,
        "Проверь этот приватный файл и найди в интернете похожее.",
        actor,
        tools=[_tool_schema("web_search")],
        attachments=None,
    )

    assert private_model.schemas
    assert private_model.schemas[0] == []


@pytest.mark.asyncio
async def test_ordinary_mode_keeps_ordinary_tools_without_inheriting_engineer_tools(
    settings,
    storage,
) -> None:
    from friday.agent_runtime import AgentContext, AgentRuntime

    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _ToolSchemaSpy()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-dialogue-engineer-boundary",
        user_id=actor.user_id,
        person_id=actor.own_id,
        interaction_mode="dialogue",
        turn_deadline=time.monotonic() + 10.0,
    )

    await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Обычный разговор.",
        actor,
        tools=[
            _tool_schema("engineer_local_tools"),
            _tool_schema("make_file"),
            _tool_schema("collect_files"),
            _tool_schema("web_search"),
        ],
        attachments=None,
    )

    assert model.schemas
    assert _schema_names(model.schemas[0]) == {"make_file", "collect_files", "web_search"}
