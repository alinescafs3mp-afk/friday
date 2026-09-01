from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.organs.obsidian.operations import ObsidianOperationService
from friday.organs.obsidian.service import ObsidianService
from friday.organs.obsidian.vault_store import VaultStore
from friday.permissions import AuthorizationService
from friday.retrieval.archive_search_authority import create_archive_model_batch_ledger
from friday.retrieval.archive_search_obsidian_reader import (
    bind_archive_obsidian_exact_file_reader,
)
from friday.web_surfer import WebSurfer


async def _kernel(settings: Any, storage: Any, user_id: str) -> tuple[ExecutionKernel, Any, WebSurfer]:
    storage.ensure_user(user_id, preset_key="user")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, graph, web, ingestion)
    return kernel, authorization.actor_for_user(user_id, source="archive-kernel-test"), web


def _ledger(actor: Any, turn: str) -> Any:
    return create_archive_model_batch_ledger(
        tenant_id=actor.user_id,
        principal_id=actor.own_id,
        turn_discriminator=turn,
    )


def _arguments(invocation: object, *, corpora: list[str] | None = None) -> dict[str, Any]:
    return {
        "query": "PRIVATE-ARCHIVE-QUERY",
        "corpora": corpora or ["generated"],
        "_archive_invocation": invocation,
    }


def _durable_read_signature(storage: Any) -> tuple[object, ...]:
    database = Path(storage.settings.database_path)

    def file_signature(path: Path) -> tuple[bool, int, str]:
        if not path.exists():
            return False, 0, hashlib.sha256(b"").hexdigest()
        body = path.read_bytes()
        return True, len(body), hashlib.sha256(body).hexdigest()

    context = storage.conn.execute(
        "SELECT singleton, batch_id, recorded_at, observed_at "
        "FROM relation_revision_context WHERE singleton=1"
    ).fetchone()
    assert context is not None
    return (
        storage.conn.total_changes,
        tuple(context),
        file_signature(database),
        file_signature(database.with_name(f"{database.name}-wal")),
    )


def test_archive_search_is_an_observe_capability_without_private_schema_fields(settings: Any) -> None:
    kernel = ExecutionKernel(settings=settings)
    tool = kernel.get_tool("archive_search")

    assert tool is not None
    assert tool.security_id == "search.use"
    assert tool.risk == "observe"
    assert tool.parameters["additionalProperties"] is False
    assert "_archive_invocation" not in tool.parameters["properties"]


@pytest.mark.asyncio
async def test_dense_authority_preflight_is_an_effect_free_read_snapshot(
    settings: Any,
    storage: Any,
) -> None:
    kernel, actor, web = await _kernel(settings, storage, "archive-kernel-dense-read-only")
    invocation = kernel.create_archive_search_invocation(
        actor=actor,
        turn_ledger=_ledger(actor, "archive-kernel-dense-read-only-turn"),
    )
    before = _durable_read_signature(storage)
    calls: list[tuple[str, str, str | None]] = []

    async def prepare(user_id: str, query: str, *, principal_id: str | None = None) -> None:
        calls.append((user_id, query, principal_id))
        assert storage.conn.in_transaction is False
        assert _durable_read_signature(storage) == before

    kernel.searcher = SimpleNamespace(prepare_archive_dense_query_plan=prepare)
    try:
        result = await kernel.execute(
            "archive_search",
            _arguments(invocation, corpora=["documents"]),
            actor=actor,
        )
    finally:
        await web.close()

    assert result.success is True
    assert calls == [(actor.user_id, "PRIVATE-ARCHIVE-QUERY", actor.own_id)]


@pytest.mark.asyncio
async def test_dense_revocation_after_preflight_still_fails_before_storage_publication(
    settings: Any,
    storage: Any,
) -> None:
    owner = "archive-kernel-dense-revoke"
    kernel, actor, web = await _kernel(settings, storage, owner)
    invocation = kernel.create_archive_search_invocation(
        actor=actor,
        turn_ledger=_ledger(actor, "archive-kernel-dense-revoke-turn"),
    )
    called = False

    async def revoke(_user_id: str, _query: str, *, principal_id: str | None = None) -> None:
        nonlocal called
        called = True
        assert principal_id == owner
        assert storage.conn.in_transaction is False
        storage.set_permission_override(owner, "search.use", "deny")

    kernel.searcher = SimpleNamespace(prepare_archive_dense_query_plan=revoke)
    try:
        result = await kernel.execute(
            "archive_search",
            _arguments(invocation, corpora=["documents"]),
            actor=actor,
        )
    finally:
        await web.close()

    assert called is True
    assert result.success is False
    assert result.prepared_archive_search is None
    assert "PRIVATE-ARCHIVE-QUERY" not in result.to_llm_message()


@pytest.mark.asyncio
async def test_kernel_carries_exact_authorized_bytes_out_of_band(settings: Any, storage: Any) -> None:
    kernel, actor, web = await _kernel(settings, storage, "archive-kernel-owner")
    ledger = _ledger(actor, "archive-kernel-exact-turn")
    invocation = kernel.create_archive_search_invocation(actor=actor, turn_ledger=ledger)
    try:
        result = await kernel.execute(
            "archive_search",
            _arguments(invocation),
            actor=actor,
        )
    finally:
        await web.close()

    assert result.success is True
    assert result.prepared_archive_search is not None
    exact = result.archive_model_visible_bytes()
    assert result.data == exact.decode("ascii")
    assert result.to_llm_message() == exact.decode("ascii")
    assert result.truncated is False
    public = result.to_dict()
    assert json.loads(public["result"]) == json.loads(exact)
    serialized = json.dumps(public, ensure_ascii=False)
    assert "PreparedArchiveSearch" not in serialized
    assert "ArchiveModelBatchLedger" not in serialized
    assert "PRIVATE-ARCHIVE-QUERY" not in serialized

    prepared = result.prepared_archive_search
    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        exact,
    )
    ledger.freeze_for_publication()


@pytest.mark.asyncio
@pytest.mark.parametrize("spoof", [{"turn_ledger": "copied"}, "copied-ledger"])
async def test_model_supplied_archive_invocation_is_rejected(
    settings: Any,
    storage: Any,
    spoof: object,
) -> None:
    kernel, actor, web = await _kernel(settings, storage, f"archive-spoof-{type(spoof).__name__}")
    try:
        result = await kernel.execute("archive_search", _arguments(spoof), actor=actor)
    finally:
        await web.close()

    assert result.success is False
    assert result.prepared_archive_search is None
    assert "PRIVATE-ARCHIVE-QUERY" not in result.to_llm_message()


@pytest.mark.asyncio
async def test_invocation_is_bound_to_the_exact_actor(settings: Any, storage: Any) -> None:
    kernel, alice, web = await _kernel(settings, storage, "archive-kernel-alice")
    storage.ensure_user("archive-kernel-bob", preset_key="user")
    assert kernel.authorization is not None
    bob = kernel.authorization.actor_for_user("archive-kernel-bob", source="archive-kernel-test")
    invocation = kernel.create_archive_search_invocation(
        actor=alice,
        turn_ledger=_ledger(alice, "archive-kernel-actor-bound-turn"),
    )
    try:
        result = await kernel.execute("archive_search", _arguments(invocation), actor=bob)
    finally:
        await web.close()

    assert result.success is False
    assert result.prepared_archive_search is None


@pytest.mark.asyncio
async def test_unsealed_callable_obsidian_reader_is_not_carried(
    settings: Any,
    storage: Any,
) -> None:
    kernel, actor, web = await _kernel(settings, storage, "archive-kernel-obsidian")
    calls: list[tuple[str, bool]] = []

    def exact_reader(_vault: str, _path: str, _sha256: str, /) -> bytes:
        return b""

    async def factory(owner_id: str) -> Any:
        calls.append((owner_id, storage.conn.in_transaction))
        return exact_reader

    kernel.bind_archive_obsidian_exact_file_reader_factory(factory)
    invocation = kernel.create_archive_search_invocation(
        actor=actor,
        turn_ledger=_ledger(actor, "archive-kernel-obsidian-turn"),
    )
    try:
        result = await kernel.execute(
            "archive_search",
            _arguments(invocation, corpora=["obsidian"]),
            actor=actor,
        )
    finally:
        await web.close()

    assert result.success is True
    assert calls == [(actor.own_id, False)]
    assert result.archive_exact_file_reader is None
    assert "exact_reader" not in json.dumps(result.to_dict())


@pytest.mark.asyncio
async def test_sealed_owner_bound_obsidian_reader_is_carried(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    owner = "archive-kernel-sealed-reader"
    kernel, actor, web = await _kernel(settings, storage, owner)
    root = tmp_path / "vault"
    bundle = storage.create_obsidian_bundle(
        owner,
        config_root=str(tmp_path / "config"),
        database_root=str(tmp_path / "database"),
        api_endpoint=f"unix://{tmp_path}/reader.sock",
        api_key_ref=f"secret:obsidian:{owner}",
        server_path=str(root),
        folder_id=f"friday-{owner}",
        setup_token_hash=hashlib.sha256(b"sealed-reader-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    service = ObsidianOperationService(storage, ObsidianService(VaultStore(root)), owner_id=owner)
    reader = bind_archive_obsidian_exact_file_reader(
        service,
        owner_id=owner,
        vault_id=str(bundle["vault"]["id"]),
    )

    async def factory(owner_id: str) -> Any:
        assert owner_id == owner
        return reader

    kernel.bind_archive_obsidian_exact_file_reader_factory(factory)
    invocation = kernel.create_archive_search_invocation(
        actor=actor,
        turn_ledger=_ledger(actor, "archive-kernel-sealed-reader-turn"),
    )
    try:
        result = await kernel.execute(
            "archive_search",
            _arguments(invocation, corpora=["obsidian"]),
            actor=actor,
        )
    finally:
        await web.close()

    assert result.success is True
    assert result.archive_exact_file_reader is reader
    assert result.archive_model_visible_bytes()


@pytest.mark.asyncio
async def test_search_revoke_during_obsidian_reader_bind_closes_before_collection(
    settings: Any,
    storage: Any,
) -> None:
    kernel, actor, web = await _kernel(settings, storage, "archive-kernel-reader-revoke")

    def exact_reader(_vault: str, _path: str, _sha256: str, /) -> bytes:
        return b""

    async def revoking_factory(owner_id: str) -> Any:
        storage.set_permission_override(owner_id, "search.use", "deny")
        return exact_reader

    kernel.bind_archive_obsidian_exact_file_reader_factory(revoking_factory)
    invocation = kernel.create_archive_search_invocation(
        actor=actor,
        turn_ledger=_ledger(actor, "archive-kernel-reader-revoke-turn"),
    )
    try:
        result = await kernel.execute(
            "archive_search",
            _arguments(invocation, corpora=["obsidian"]),
            actor=actor,
        )
    finally:
        await web.close()

    assert result.success is False
    assert result.prepared_archive_search is None
    assert "PRIVATE-ARCHIVE-QUERY" not in result.to_llm_message()


@pytest.mark.asyncio
async def test_copied_archive_payload_cannot_reuse_a_prepared_carrier(
    settings: Any,
    storage: Any,
) -> None:
    kernel, actor, web = await _kernel(settings, storage, "archive-kernel-copy")
    invocation = kernel.create_archive_search_invocation(
        actor=actor,
        turn_ledger=_ledger(actor, "archive-kernel-copy-turn"),
    )
    try:
        result = await kernel.execute("archive_search", _arguments(invocation), actor=actor)
    finally:
        await web.close()

    copied = ToolResult(
        "archive_search",
        True,
        data=str(result.data) + " ",
        prepared_archive_search=result.prepared_archive_search,
        archive_exact_file_reader=result.archive_exact_file_reader,
    )
    with pytest.raises(ValueError, match="unavailable"):
        copied.archive_model_visible_bytes()
    assert "PRIVATE-ARCHIVE-QUERY" not in copied.to_llm_message()
    assert copied.to_dict()["success"] is False
