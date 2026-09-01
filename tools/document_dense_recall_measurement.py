#!/usr/bin/env python
"""Measure archive dense admission on the current frozen synthetic corpus.

The instrument deliberately separates two questions:

* Does the shipped archive federation ranking/reauthorization path admit a
  revalidated dense passage and improve recall over the exact lexical arm?
* Is a particular production embedding model good enough?

Only the first is measured here.  The vectors are a frozen, auditable qrel-axis
fixture, so this report must never be presented as production-model quality.
The input is the complete 24-qrel corpus already maintained by
``tools/retrieval_bench.py``, projected to long-form documents so the shipped
passage index (rather than an invented whole-object evidence path) is exercised.
This instrument captures the private stable federation before publication.  It
does not traverse ``ExecutionKernel`` or establish model-visible/answer quality.
All output is body-, query-, title-, filename-, tenant-, and object-ID-free.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import socket
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, cast

import retrieval_bench as legacy_corpus

_SCHEMA: Final = "friday.document-dense-recall-measurement.body-free.v1"
_TENANT: Final = "document-dense-measurement-tenant"
_PRINCIPAL: Final = "document-dense-measurement-principal"
_FOREIGN: Final = "document-dense-measurement-foreign"
_MODEL: Final = "frozen-qrel-axis-v1-not-a-production-model"
_CHUNK_SCHEME: Final = "v2:200:20:8"
_FILLER_COUNT: Final = 120
_SEED: Final = 20260726
_PROJECTION_REPETITIONS: Final = 12
_LIMITATIONS: Final = (
    "frozen_qrel_axis_vectors_measure_archive_federation_ranking_and_reauthorization_not_production_embedding_quality",
    "private_federation_capture_does_not_measure_execution_kernel_or_model_visible_output",
    "long_form_projection_preserves_current_24_qrels_but_is_synthetic_not_live_owner_corpus",
    "production_embedding_model_quality_not_measured",
    "absence_is_not_claimed_from_the_dense_lane",
)


class MeasurementError(RuntimeError):
    """The closed offline instrument failed."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _source_root() -> Path:
    import friday

    return Path(friday.__file__).resolve().parents[1]


def _git_identity(root: Path) -> dict[str, object]:
    def invoke(*arguments: str) -> bytes:
        completed = subprocess.run(  # noqa: S603
            ("git", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
            timeout=10,
        )
        if completed.stderr:
            raise MeasurementError("source identity emitted unexpected diagnostics")
        return completed.stdout

    commit = invoke("rev-parse", "HEAD").decode("ascii").strip()
    status = invoke("status", "--porcelain=v1", "--untracked-files=all")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise MeasurementError("source commit identity is invalid")
    return {
        "commit": commit,
        "worktree_clean": status == b"",
        "worktree_status_sha256": hashlib.sha256(status).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class _Document:
    ordinal: int
    legacy_id: str
    title: str
    body: str
    vector: tuple[float, ...]
    owner: str = _PRINCIPAL

    @property
    def raw_id(self) -> str:
        return f"raw_{0xC000000000000000 + self.ordinal:016x}"

    @property
    def knowledge_id(self) -> str:
        return f"ko_dense_measure_{self.ordinal:04d}"

    @property
    def projected_body(self) -> str:
        unit = f"{self.title}\n{self.body}"
        return "\n".join(unit for _item in range(_PROJECTION_REPETITIONS))


def _corpus() -> tuple[tuple[_Document, ...], tuple[tuple[str, str, str], ...]]:
    gold = tuple((str(query), str(expected), str(kind)) for query, expected, kind in legacy_corpus.GOLD)
    dimensions = len(gold)
    axes: dict[str, list[int]] = defaultdict(list)
    for axis, (_query, expected, _kind) in enumerate(gold):
        axes[expected].append(axis)

    rows: list[tuple[str, str, str]] = [
        (str(document_id), str(title), str(body))
        for document_id, title, body, _category in legacy_corpus.DOCUMENTS
    ]
    rng = random.Random(_SEED)
    words = (
        "встреча",
        "отчёт",
        "заметка",
        "план",
        "черновик",
        "письмо",
        "счёт",
        "договор",
        "выписка",
        "инструкция",
        "памятка",
        "список",
    )
    for index in range(_FILLER_COUNT):
        topic = str(legacy_corpus.FILLER_TOPICS[index % len(legacy_corpus.FILLER_TOPICS)])
        sample = rng.sample(words, 6)
        rows.append(
            (
                f"filler-{index:03d}",
                f"{topic} {index}",
                f"{' '.join(sample)}. Запись номер {index}.",
            )
        )

    documents: list[_Document] = []
    for ordinal, (legacy_id, title, body) in enumerate(rows, 1):
        vector = [-1.0] * dimensions
        if legacy_id in axes:
            vector = [0.0] * dimensions
            for axis in axes[legacy_id]:
                vector[axis] = 1.0
        documents.append(_Document(ordinal, legacy_id, title, body, tuple(vector)))

    # An equally strong foreign-owner vector proves that ranking is never used as
    # authority.  It is intentionally absent from the corpus/qrel manifests.
    foreign_vector = [0.0] * dimensions
    foreign_vector[0] = 1.0
    documents.append(
        _Document(
            len(rows) + 1,
            "foreign-authority-control",
            "Foreign authority control",
            "This evidence belongs to a different principal.",
            tuple(foreign_vector),
            owner=_FOREIGN,
        )
    )
    return tuple(documents), gold


def _manifests(
    documents: tuple[_Document, ...],
    gold: tuple[tuple[str, str, str], ...],
) -> dict[str, object]:
    public_documents = tuple(item for item in documents if item.owner == _PRINCIPAL)
    corpus_rows = [
        {
            "body": item.body,
            "legacy_id": item.legacy_id,
            "ordinal": item.ordinal,
            "projection_repetitions": _PROJECTION_REPETITIONS,
            "title": item.title,
        }
        for item in public_documents
    ]
    vector_rows = [
        {
            "legacy_id": item.legacy_id,
            "vector": list(item.vector),
        }
        for item in public_documents
    ]
    return {
        "case_count": len(gold),
        "corpus_manifest_sha256": _sha256(corpus_rows),
        "document_count": len(public_documents),
        "legacy_corpus_source_sha256": hashlib.sha256(Path(legacy_corpus.__file__).read_bytes()).hexdigest(),
        "qrel_manifest_sha256": _sha256(gold),
        "vector_fixture_sha256": _sha256(vector_rows),
    }


class _FrozenEmbeddings:
    remote_enabled = True

    def __init__(self, settings: Any, query_vectors: Mapping[str, tuple[float, ...]]) -> None:
        self.settings = replace(
            settings,
            embeddings_model=_MODEL,
            embeddings_chunk_chars=200,
            embeddings_chunk_overlap_chars=20,
            embeddings_chunk_max_per_object=8,
            embeddings_chunk_scan_multiplier=8,
            embeddings_dense_max_objects=256,
            embeddings_recall_candidates=64,
            embeddings_resident_cache=False,
        )
        self._query_vectors = dict(query_vectors)

    async def embed(self, texts: list[str], **_kwargs: object) -> list[list[float]] | None:
        if type(texts) is not list or len(texts) != 1 or texts[0] not in self._query_vectors:
            return None
        return [list(self._query_vectors[texts[0]])]


@contextmanager
def _isolated_environment(home: Path) -> Iterator[None]:
    managed = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("FRIDAY_") or key.startswith("JERICHO_")
    }
    for key in tuple(managed):
        os.environ.pop(key, None)
    overrides = {
        "FRIDAY_API_TOKEN": "A" * 48,
        "FRIDAY_DATABASE_MUST_EXIST": "0",
        "FRIDAY_EMBEDDINGS_ENABLED": "0",
        "FRIDAY_ENV_FILE": str(home / "intentionally-absent.env"),
        "FRIDAY_HOME": str(home),
        "FRIDAY_INGESTION_REVIEW_POLICY": "assessed",
        "FRIDAY_LLM_ENABLED": "0",
        "FRIDAY_MCP_ENABLED": "0",
        "FRIDAY_OBSIDIAN_ENABLED": "0",
        "FRIDAY_SECONDARY_LLM_ENABLED": "0",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "off",
        "FRIDAY_TELEGRAM_BRIDGE_SECRET": "B" * 48,
        "FRIDAY_WORKERS_ENABLED": "0",
    }
    os.environ.update(overrides)
    originals = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise MeasurementError("measurement attempted network access")

    def replace(owner: Any, name: str, value: Any) -> None:
        setattr(owner, name, value)

    replace(socket.socket, "connect", no_network)
    replace(socket.socket, "connect_ex", no_network)
    replace(socket, "create_connection", no_network)
    try:
        yield
    finally:
        replace(socket.socket, "connect", originals[0])
        replace(socket.socket, "connect_ex", originals[1])
        replace(socket, "create_connection", originals[2])
        for key in overrides:
            os.environ.pop(key, None)
        os.environ.update(managed)


def _seed(storage: Any, documents: tuple[_Document, ...]) -> dict[str, int]:
    from friday.retrieval import knowledge_chunk_units, pack_vector
    from friday.storage.models import InboxItem, InboxStatus, KnowledgeObject, RawObject

    storage.ensure_user(_TENANT)
    storage.ensure_user(_PRINCIPAL)
    storage.ensure_user(_FOREIGN)
    object_vectors: list[dict[str, object]] = []
    chunks: dict[str, list[dict[str, object]]] = {}
    for item in documents:
        body = item.projected_body
        timestamp = f"2026-08-28T10:{item.ordinal // 60:02d}:{item.ordinal % 60:02d}+00:00"
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        storage.store_raw_object(
            RawObject(
                id=item.raw_id,
                user_id=_TENANT,
                source="upload",
                source_ref=f"dense-measurement:{item.ordinal:04d}",
                raw_content=body,
                content_type="file",
                metadata_json={
                    "filename": f"dense-measurement-{item.ordinal:04d}.txt",
                    "media_kind": "document",
                    "mime_type": "text/plain",
                    "uploaded_by": item.owner,
                },
                content_hash=digest,
                received_at=timestamp,
                created_at=timestamp,
            )
        )
        storage.store_inbox_item(
            InboxItem(
                id=f"inbox_{0xC100000000000000 + item.ordinal:016x}",
                user_id=_TENANT,
                raw_object_id=item.raw_id,
                status=InboxStatus.CLASSIFIED,
                created_at=timestamp,
                reviewed_at=timestamp,
                reviewed_by=item.owner,
            )
        )
        knowledge = KnowledgeObject(
            id=item.knowledge_id,
            user_id=_TENANT,
            raw_object_id=item.raw_id,
            content=body,
            content_type="document",
            title=item.title,
            summary="",
            knowledge_kind="document",
            lifecycle_stage="active",
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
        storage.store_knowledge_object(knowledge)
        packed = pack_vector(list(item.vector))
        object_vectors.append(
            {
                "knowledge_object_id": item.knowledge_id,
                "user_id": _TENANT,
                "model": _MODEL,
                "dim": len(item.vector),
                "source_version": 1,
                "content_hash": digest,
                "chunk_scheme": _CHUNK_SCHEME,
                "vector": packed,
            }
        )
        units = knowledge_chunk_units(
            {
                "content": body,
                "title": item.title,
                "summary": "",
                "knowledge_kind": "document",
            },
            max_chars=200,
            overlap_chars=20,
            max_chunks=8,
        )
        if len(units) < 2:
            raise MeasurementError("long-form projection did not produce passage vectors")
        chunks[item.knowledge_id] = [
            {
                "chunk_index": index,
                "user_id": _TENANT,
                "model": _MODEL,
                "dim": len(item.vector),
                "source_version": 1,
                "chunk_scheme": _CHUNK_SCHEME,
                "start_char": start,
                "end_char": end,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "vector": packed,
            }
            for index, (start, end, text) in enumerate(units)
        ]
    written = storage.upsert_knowledge_vectors(object_vectors, chunks)
    if written != {
        "objects": len(documents),
        "chunks": sum(len(rows) for rows in chunks.values()),
    }:
        raise MeasurementError("frozen vector fixture was not written exactly")
    return {
        "foreign_authority_control_documents": sum(item.owner == _FOREIGN for item in documents),
        "object_vectors": int(written["objects"]),
        "passage_vectors": int(written["chunks"]),
        "principal_documents": sum(item.owner == _PRINCIPAL for item in documents),
        "seeded_documents": len(documents),
    }


def _actor() -> Any:
    from friday.permissions import ActorContext

    return ActorContext(
        user_id=_TENANT,
        preset_key="user",
        source="document-dense-measurement",
        shared_tenant=True,
        person_id=_PRINCIPAL,
    )


async def _measure(
    storage: Any,
    settings: Any,
    documents: tuple[_Document, ...],
    gold: tuple[tuple[str, str, str], ...],
    *,
    arm: str,
) -> dict[str, object]:
    import friday.retrieval.archive_search_service as service_module
    from friday.permissions import AuthorizationService
    from friday.retrieval.archive_search_authority import (
        abandon_empty_archive_model_batch_ledger,
        create_archive_model_batch_ledger,
    )
    from friday.retrieval.archive_search_contract import ArchiveSearchCorpus, ArchiveSearchRequest
    from friday.retrieval.archive_search_service import prepare_archive_search_in_transaction

    by_legacy_id = {item.legacy_id: item for item in documents}
    query_vectors = {
        query: tuple(1.0 if index == axis else 0.0 for index in range(len(gold)))
        for axis, (query, _expected, _kind) in enumerate(gold)
    }
    searcher: Any = None
    if arm == "dense":
        from friday.retrieval import HybridSearcher

        searcher = HybridSearcher(
            storage,
            cast(Any, _FrozenEmbeddings(settings, query_vectors)),
            dense_evidence_min=0.35,
            record_usage=False,
        )
    authorization = AuthorizationService(storage, shared_tenant=_TENANT)
    ranks: list[int | None] = []
    by_kind: dict[str, list[int | None]] = defaultdict(list)
    opaque_cases: list[dict[str, object]] = []
    foreign_sources_returned = 0
    dense_evidence_cases = 0
    current_revision_cases = 0
    foreign_raw_id = next(item.raw_id for item in documents if item.owner == _FOREIGN)

    for ordinal, (query, expected, kind) in enumerate(gold, 1):
        plan = None
        if searcher is not None:
            plan = await searcher.prepare_archive_dense_query_plan(
                _TENANT,
                query,
                principal_id=_PRINCIPAL,
            )
            if plan is None:
                raise MeasurementError("dense treatment did not produce a sealed query plan")
        request = ArchiveSearchRequest.create(
            query=query,
            corpora=(ArchiveSearchCorpus.DOCUMENTS,),
            limit=20,
        )
        ledger = create_archive_model_batch_ledger(
            tenant_id=_TENANT,
            principal_id=_PRINCIPAL,
            turn_discriminator=f"document-dense-measurement-{arm}-{ordinal:04d}",
        )
        captured: list[Any] = []
        original_collect = service_module._collect_federated_in_transaction

        def capture(
            *args: object,
            _original: Any = original_collect,
            _captured: list[Any] = captured,
            **kwargs: object,
        ) -> Any:
            value = _original(*args, **kwargs)
            _captured.append(value)
            return value

        service_module._collect_federated_in_transaction = capture
        try:
            with storage.transaction() as conn:
                if arm == "dense":
                    prepare_archive_search_in_transaction(
                        conn,
                        authorization=authorization,
                        actor=_actor(),
                        tenant_id=_TENANT,
                        principal_id=_PRINCIPAL,
                        request=request,
                        snapshot_discriminator=f"document-dense-measurement-{arm}-{ordinal:04d}",
                        run_discriminator=f"document-dense-measurement-{arm}-{ordinal:04d}",
                        turn_ledger=ledger,
                        dense_query_plan=plan,
                    )
                else:
                    prepare_archive_search_in_transaction(
                        conn,
                        authorization=authorization,
                        actor=_actor(),
                        tenant_id=_TENANT,
                        principal_id=_PRINCIPAL,
                        request=request,
                        snapshot_discriminator=f"document-dense-measurement-{arm}-{ordinal:04d}",
                        run_discriminator=f"document-dense-measurement-{arm}-{ordinal:04d}",
                        turn_ledger=ledger,
                    )
        finally:
            service_module._collect_federated_in_transaction = original_collect
            abandon_empty_archive_model_batch_ledger(ledger)
        if len(captured) != 2 or not service_module._same_federation(captured[0], captured[1]):
            raise MeasurementError("archive materialization was not stable across authority reads")
        federation = captured[0]
        candidates = (*federation.candidates, *federation.tail_candidates)[:20]
        source_ids = [candidate.resolved_source.source_ref.canonical_object_id for candidate in candidates]
        foreign_sources_returned += source_ids.count(foreign_raw_id)
        target = by_legacy_id[expected]
        rank = source_ids.index(target.raw_id) + 1 if target.raw_id in source_ids else None
        ranks.append(rank)
        by_kind[kind].append(rank)
        case_dense = False
        case_current = False
        if rank is not None:
            candidate = candidates[rank - 1]
            case_dense = any(match.channel.value == "dense" for match in candidate.matches)
            case_current = bool(candidate.passages) and all(
                passage.passage_ref.revision_matches(candidate.resolved_source)
                for passage in candidate.passages
            )
        dense_evidence_cases += int(case_dense)
        current_revision_cases += int(case_current)
        opaque_cases.append(
            {
                "case_id": hashlib.sha256(
                    f"friday.document-dense-measurement.v1\0{kind}\0{query}\0{expected}".encode()
                ).hexdigest(),
                "dense_evidence": case_dense,
                "kind": kind,
                "rank": rank,
                "source_revision_current": case_current,
            }
        )

    def metric(values: Sequence[int | None], *, limit: int) -> dict[str, int]:
        numerator = sum(rank is not None and rank <= limit for rank in values)
        denominator = len(values)
        return {
            "denominator": denominator,
            "numerator": numerator,
            "ppm": round(numerator * 1_000_000 / denominator),
        }

    reciprocal_sum = sum(1.0 / rank for rank in ranks if rank is not None and rank <= 10)
    dcg_sum = sum(1.0 / math.log2(rank + 1) for rank in ranks if rank is not None and rank <= 10)
    return {
        "arm": arm,
        "authorized_foreign_sources_returned": foreign_sources_returned,
        "cases": sorted(opaque_cases, key=lambda item: str(item["case_id"])),
        "current_revision_cases": current_revision_cases,
        "dense_evidence_cases": dense_evidence_cases,
        "metrics": {
            "mrr_at_10_ppm": round(reciprocal_sum * 1_000_000 / len(ranks)),
            "ndcg_at_10_ppm": round(dcg_sum * 1_000_000 / len(ranks)),
            "recall_at_10": metric(ranks, limit=10),
            "recall_at_20": metric(ranks, limit=20),
        },
        "per_kind_recall_at_10": {kind: metric(values, limit=10) for kind, values in sorted(by_kind.items())},
    }


def run_arm(arm: str) -> dict[str, object]:
    """Run one arm and return its canonical body-free envelope."""

    if arm not in {"lexical", "dense"}:
        raise MeasurementError("measurement arm is invalid")
    from friday.config import ensure_runtime_dirs, load_settings
    from friday.retrieval_benchmark.release import archive_search_release_sha256
    from friday.storage import init_storage

    source_root = _source_root()
    instrument_start = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    release_start = archive_search_release_sha256()
    source_start = _git_identity(source_root)
    documents, gold = _corpus()
    manifests = _manifests(documents, gold)
    with tempfile.TemporaryDirectory(prefix="friday-document-dense-measurement-") as directory:
        home = Path(directory) / "home"
        with _isolated_environment(home):
            settings = load_settings()
            ensure_runtime_dirs(settings)
            storage = init_storage(settings)
            try:
                index_fixture = _seed(storage, documents)
                result = asyncio.run(_measure(storage, settings, documents, gold, arm=arm))
            finally:
                storage.close(final=True)
    instrument_end = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    release_end = archive_search_release_sha256()
    source_end = _git_identity(source_root)
    if instrument_start != instrument_end or release_start != release_end or source_start != source_end:
        raise MeasurementError("source identity changed during measurement")
    return {
        "claim": {
            "corpus": "current_code_owned_synthetic_long_form_projection",
            "execution_kernel_path": "not_measured",
            "model_visible_output": "not_measured",
            "production_embedding_model_quality": "not_measured",
            "production_owner_corpus": "not_measured",
            "scope": "synthetic_archive_federation_ranking_and_reauthorization",
        },
        "corpus": manifests,
        "index_fixture": index_fixture,
        "instrument_sha256": instrument_start,
        "limitations": list(_LIMITATIONS),
        "model_fixture": _MODEL,
        "network_forbidden": True,
        "release_sha256": release_start,
        "result": result,
        "schema": _SCHEMA,
        "source": {
            **source_start,
            "guard": {
                "instrument_sha256_end": instrument_end,
                "instrument_sha256_start": instrument_start,
                "release_sha256_end": release_end,
                "release_sha256_start": release_start,
            },
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("lexical", "dense"), required=True)
    args = parser.parse_args(argv)
    payload = run_arm(str(args.arm))
    print(_canonical(payload).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
