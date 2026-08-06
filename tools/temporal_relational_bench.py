#!/usr/bin/env python3
"""Synthetic, privacy-safe gold set for temporal relation retrieval.

The ordinary evaluation table intentionally remains small (query -> expected IDs).
Temporal retrieval needs two independent time axes, forbidden results, and honest
no-answer cases, so forcing it into that table would silently change its meaning.

This harness never opens the operator's database.  It creates one throwaway Friday
home, builds reviewed explicit relations, and runs the same searcher factory as the
production evaluator.  Calibration remains public; holdout dispatch stays sealed
until one committed candidate passes its exact manifest, then compares an archived
base package with that one-file candidate in isolated subprocesses.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

GOLD_CLASSES = (
    "valid_time_handover",
    "known_at_late_end",
    "bitemporal_replacement",
    "two_hop_chain",
    "temporal_gap",
)
GOLD_SPLITS = ("calibration", "holdout")
GOLD_CASES_PER_CLASS_PER_SPLIT = 4
GOLD_TOTAL_CASES = len(GOLD_CLASSES) * len(GOLD_SPLITS) * GOLD_CASES_PER_CLASS_PER_SPLIT
_USER_ID = "synthetic-temporal-bench"

# Post-baseline calibration contract.  The frozen arm contains sixteen positive
# cases and four deliberate no-answer cases.  Before spending the sealed holdout,
# a candidate must recover at least half of the positives, touch every temporal
# class, and preserve every negative.  These are instrument-readiness thresholds,
# not the future holdout comparison from Proposal 31.
CALIBRATION_TOTAL_CASES = 20
CALIBRATION_POSITIVE_CASES = 16
CALIBRATION_NO_ANSWER_CASES = 4
CALIBRATION_MIN_CORRECT_AT_10 = 12
CALIBRATION_MIN_EXPECTED_HITS_AT_10 = 8
CALIBRATION_MAX_FORBIDDEN_HITS_AT_10 = 0
CALIBRATION_MIN_POSITIVE_CORRECT_PER_CLASS = 1

BASELINE_EXIT_CONTRACT_INVALID = 2
BASELINE_EXIT_INFRA_FAILURE = 3
BASELINE_EXIT_QUALITY_REJECTED = 4

_ARM_PROTOCOL = "friday_temporal_arm_v1"
_ARM_NONCE_ENV = "FRIDAY_TEMPORAL_ARM_NONCE"
_ARM_PACKAGE_ROOT_ENV = "FRIDAY_TEMPORAL_ARM_PACKAGE_ROOT"
_ARM_SCRATCH_ROOT_ENV = "FRIDAY_TEMPORAL_ARM_SCRATCH_ROOT"
_ARM_TOOL_ROOT_ENV = "FRIDAY_TEMPORAL_ARM_TOOL_ROOT"
_ARM_TIMEOUT_SECONDS = 900
_ARM_KINDS = frozenset({"exact_base", "candidate"})
_SCRATCH_ENV_PATHS = {
    "FRIDAY_HOME": ".",
    "FRIDAY_DATA_DIR": "data",
    "FRIDAY_CACHE_DIR": "cache",
    "FRIDAY_LOG_DIR": "logs",
    "FRIDAY_MODEL_ROOT": "models",
    "FRIDAY_STATE_DIR": "data/state",
    "FRIDAY_DATABASE_PATH": "data/state/friday.sqlite3",
    "FRIDAY_FILES_DIR": "data/files",
    "FRIDAY_MEMORY_VAULT_DIR": "data/memory-vault",
    "FRIDAY_BACKUPS_DIR": "data/backups",
    "FRIDAY_EXPORTS_DIR": "data/exports",
    "FRIDAY_BACKUP_MIRROR_DIR": "data/backups/mirror",
    "FRIDAY_BACKUP_ENCRYPTION_KEY_FILE": "config/unused-backup.key",
    "FRIDAY_WHISPER_DOWNLOAD_ROOT": "models/whisper",
    "FRIDAY_TTS_DOWNLOAD_ROOT": "models/tts",
}
_MODEL_ENV_ALLOWLIST = frozenset(
    {
        "FRIDAY_PROFILE",
        "FRIDAY_LLM_BASE_URL",
        "FRIDAY_LLM_TIMEOUT_SEC",
        "FRIDAY_LLM_API_KEY",
        "FRIDAY_EMBEDDINGS_ENABLED",
        "FRIDAY_EMBEDDINGS_BASE_URL",
        "FRIDAY_EMBEDDINGS_API_KEY",
        "FRIDAY_EMBEDDINGS_MODEL",
        "FRIDAY_EMBEDDINGS_INDEX_BATCH",
        "FRIDAY_RETRIEVAL_DENSE_QUERY_BUDGET_SEC",
        "FRIDAY_EMBEDDINGS_INDEX_TICK_BUDGET_SEC",
        "FRIDAY_EMBEDDINGS_INDEX_CHAR_BUDGET",
        "FRIDAY_EMBEDDINGS_INDEX_REST_RATIO",
        "FRIDAY_EMBEDDINGS_INDEX_INTERVAL_SEC",
        "FRIDAY_EMBEDDINGS_RECALL_CANDIDATES",
        "FRIDAY_EMBEDDINGS_DENSE_MAX_OBJECTS",
        "FRIDAY_EMBEDDINGS_CHUNK_CHARS",
        "FRIDAY_EMBEDDINGS_CHUNK_OVERLAP_CHARS",
        "FRIDAY_EMBEDDINGS_CHUNK_MAX_PER_OBJECT",
        "FRIDAY_EMBEDDINGS_CHUNK_BLEND",
        "FRIDAY_EMBEDDINGS_CHUNK_SCAN_MULTIPLIER",
        "FRIDAY_EMBEDDINGS_RESIDENT_CACHE",
        "FRIDAY_EMBEDDINGS_MAX_INPUTS_PER_REQUEST",
        "FRIDAY_GRAPH_MAX_DEPTH",
        "FRIDAY_RETRIEVAL_POOL_MAX",
        "FRIDAY_RETRIEVAL_DENSE_EVIDENCE_MIN",
        "FRIDAY_RERANK_BASE_URL",
        "FRIDAY_RERANK_MODEL",
        "FRIDAY_RERANK_API_KEY",
        "FRIDAY_RERANK_TIMEOUT_SEC",
        "FRIDAY_RERANK_TOP",
        "FRIDAY_RERANK_CONFIDENT_MIN",
    }
)
_MODEL_ENV_SOURCE_KEYS = _MODEL_ENV_ALLOWLIST | frozenset(
    "JERICHO_" + key.removeprefix("FRIDAY_") for key in _MODEL_ENV_ALLOWLIST
)
_ARM_REPORT_KEYS = frozenset(
    {
        "fixture_sha256",
        "split",
        "cases",
        "correct",
        "case_correct_at_10",
        "mrr",
        "positive_cases",
        "expected_hits_at_10",
        "no_answer_cases",
        "no_answer_correct",
        "forbidden_hits_at_10",
        "positive_expected_entity_present",
        "forbidden_entity_present",
        "by_class",
        "p50_latency_ms",
        "p95_latency_ms",
        "graph_failures",
        "rerank_applied_cases",
        "reranker_calls",
        "reranker_failures",
        "snapshot_failures",
        "embedding_failures",
        "structure_unchanged",
        "per_case",
    }
)
_PER_CASE_REPORT_KEYS = frozenset(
    {
        "case",
        "class",
        "positive",
        "correct",
        "expected_rank",
        "forbidden_ranks",
        "expected_entity_present",
        "forbidden_entity_present",
        "latency_ms",
        "graph_failed",
        "rerank_applied",
        "reranker_failed",
        "snapshot_failed",
        "embedding_failed",
    }
)

EXACT_BASE_CALIBRATION_TOTAL = 20
EXACT_BASE_CALIBRATION_CORRECT = 4
EXACT_BASE_CALIBRATION_POSITIVE_CASES = 16
EXACT_BASE_CALIBRATION_EXPECTED_HITS_AT_10 = 0
EXACT_BASE_CALIBRATION_NO_ANSWER_CASES = 4
EXACT_BASE_CALIBRATION_NO_ANSWER_CORRECT = 4
EXACT_BASE_CALIBRATION_FORBIDDEN_HITS_AT_10 = 0
EXACT_BASE_CALIBRATION_MRR = 0.0

HOLDOUT_MIN_NET_GAIN = 2
HOLDOUT_MAX_LOSSES = 0
HOLDOUT_MAX_MRR_REGRESSION = 0.01

_REPO_ROOT_ENV = "FRIDAY_TEMPORAL_REPO_ROOT"
_COMMITTED_EVALUATOR_ENV = "FRIDAY_TEMPORAL_COMMITTED_EVALUATOR_SHA256"
_VERIFIED_TOOL_ROOT_ENV = "FRIDAY_TEMPORAL_VERIFIED_TOOL_ROOT"
_VERIFIED_TOOL_CAPABILITY_ENV = "FRIDAY_TEMPORAL_VERIFIED_TOOL_CAPABILITY"
ROOT = Path(os.environ.get(_REPO_ROOT_ENV) or Path(__file__).resolve().parents[1]).resolve()
CANDIDATE_ID = "temporal_explicit_path_bypass_reranker_v1"
CANDIDATE_BASE_COMMIT = "dc69b3f60a88d60b06911534356548764da4e02e"
CANDIDATE_PATH = "friday/retrieval/__init__.py"
CANDIDATE_MANIFEST_REPO_PATH = "tools/temporal_relational_candidate_manifest.json"
CANDIDATE_EVALUATOR_PATH = "tools/temporal_relational_bench.py"
CANDIDATE_HELPER_PATH = "tools/retrieval_bench.py"
CANDIDATE_MANIFEST_PATH = ROOT / CANDIDATE_MANIFEST_REPO_PATH
_HOLDOUT_LATCH_NAME = "friday-temporal-holdout-attempt-v1.json"
_TOOL_CAPABILITY_NAME = ".friday-temporal-capability"
_CANDIDATE_MANIFEST_FIELDS = frozenset(
    {
        "version",
        "candidate_id",
        "base_commit",
        "candidate_path",
        "evaluator_path",
        "helper_path",
        "gold_manifest_sha256",
        "candidate_diff_sha256",
        "evaluator_blob_sha256",
        "helper_blob_sha256",
    }
)
_CANDIDATE_DIFF_OPTIONS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--binary",
    "--full-index",
    "--no-renames",
    "--diff-algorithm=myers",
    "--unified=3",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)

# One alias per world.  These words never appear in the corresponding evidence
# document: the graph, not lexical overlap, must bridge query alias -> canonical
# entity -> reviewed relation -> target Knowledge Object.
_WORLD_ALIASES = (
    "амарант",
    "бархат",
    "вереск",
    "гелиотроп",
    "дельта",
    "ельник",
    "жасмин",
    "зенит",
    "ирис",
    "каскад",
    "лазурь",
    "маяк",
    "нефрит",
    "оникс",
    "парус",
    "кварц",
    "рубин",
    "сапфир",
    "топаз",
    "янтарь",
)


@dataclass(frozen=True)
class WorldSpec:
    id: str
    split: str
    kind: str
    alias: str
    year: int


@dataclass(frozen=True)
class GoldCase:
    id: str
    world_id: str
    split: str
    kind: str
    query: str
    as_of: str
    known_at_checkpoint: str
    expected_knowledge_ids: tuple[str, ...]
    forbidden_knowledge_ids: tuple[str, ...]
    expected_entity_ids: tuple[str, ...]
    forbidden_entity_ids: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeCase:
    spec: GoldCase
    known_at: str


def _slug(value: str) -> str:
    return value.replace("_", "-")


def _worlds() -> tuple[WorldSpec, ...]:
    worlds: list[WorldSpec] = []
    alias_index = 0
    for kind in GOLD_CLASSES:
        for position in range(4):
            split = "calibration" if position < 2 else "holdout"
            worlds.append(
                WorldSpec(
                    id=f"{_slug(kind)}-{position + 1}",
                    split=split,
                    kind=kind,
                    alias=_WORLD_ALIASES[alias_index],
                    year=2020 + position,
                )
            )
            alias_index += 1
    return tuple(worlds)


WORLD_SPECS = _worlds()


def _knowledge_id(world: WorldSpec, role: str) -> str:
    return f"ko-temporal-{world.id}-{role}"


def _entity_id(world: WorldSpec, role: str) -> str:
    return f"ent-temporal-{world.id}-{role}"


def _query(world: WorldSpec, state: str) -> str:
    year = world.year
    if world.kind == "valid_time_handover":
        asked_year = year if state == "before" else year + 1
        return f"кто управлял проектом «{world.alias}» в {asked_year} году"
    if world.kind == "known_at_late_end":
        suffix = "до поздней сверки" if state == "initial" else "после поздней сверки"
        return f"кого Friday считала назначенным для «{world.alias}» {suffix}"
    if world.kind == "bitemporal_replacement":
        suffix = "до исправления" if state == "initial" else "после исправления"
        return f"кто числился за «{world.alias}» в {year} году {suffix}"
    if world.kind == "two_hop_chain":
        asked_year = year if state == "before" else year + 1
        # ``через что`` is intentionally in the already measured relational class;
        # otherwise HybridSearcher quite correctly limits the traversal to one hop.
        return f"через что «{world.alias}» связан с дальним исполнителем в {asked_year} году"
    asked_year = year if state == "gap" else year + 1
    return f"кто управлял проектом «{world.alias}» в {asked_year} году"


def _case_specs() -> tuple[GoldCase, ...]:
    cases: list[GoldCase] = []
    for world in WORLD_SPECS:
        old_ko = _knowledge_id(world, "old")
        new_ko = _knowledge_id(world, "new")
        old_entity = _entity_id(world, "old")
        new_entity = _entity_id(world, "new")
        if world.kind == "valid_time_handover":
            rows = (
                ("before", f"{world.year}-06-15", "", (old_ko,), (new_ko,), (old_entity,), (new_entity,)),
                (
                    "after",
                    f"{world.year + 1}-06-15",
                    "",
                    (new_ko,),
                    (old_ko,),
                    (new_entity,),
                    (old_entity,),
                ),
            )
        elif world.kind == "known_at_late_end":
            rows = (
                ("initial", "", "initial", (old_ko,), (new_ko,), (old_entity,), (new_entity,)),
                ("corrected", "", "corrected", (), (old_ko,), (), (old_entity,)),
            )
        elif world.kind == "bitemporal_replacement":
            rows = (
                (
                    "initial",
                    f"{world.year}-06-15",
                    "initial",
                    (old_ko,),
                    (new_ko,),
                    (old_entity,),
                    (new_entity,),
                ),
                (
                    "corrected",
                    f"{world.year}-06-15",
                    "corrected",
                    (new_ko,),
                    (old_ko,),
                    (new_entity,),
                    (old_entity,),
                ),
            )
        elif world.kind == "two_hop_chain":
            rows = (
                ("before", f"{world.year}-06-15", "", (old_ko,), (new_ko,), (old_entity,), (new_entity,)),
                (
                    "after",
                    f"{world.year + 1}-06-15",
                    "",
                    (new_ko,),
                    (old_ko,),
                    (new_entity,),
                    (old_entity,),
                ),
            )
        else:
            rows = (
                (
                    "gap",
                    f"{world.year}-06-15",
                    "",
                    (),
                    (old_ko, new_ko),
                    (),
                    (old_entity, new_entity),
                ),
                (
                    "after",
                    f"{world.year + 1}-06-15",
                    "",
                    (new_ko,),
                    (old_ko,),
                    (new_entity,),
                    (old_entity,),
                ),
            )
        for state, as_of, checkpoint, expected_ko, forbidden_ko, expected_ent, forbidden_ent in rows:
            cases.append(
                GoldCase(
                    id=f"case-{world.id}-{state}",
                    world_id=world.id,
                    split=world.split,
                    kind=world.kind,
                    query=_query(world, state),
                    as_of=as_of,
                    known_at_checkpoint=checkpoint,
                    expected_knowledge_ids=expected_ko,
                    forbidden_knowledge_ids=forbidden_ko,
                    expected_entity_ids=expected_ent,
                    forbidden_entity_ids=forbidden_ent,
                )
            )
    return tuple(cases)


GOLD_CASES = _case_specs()


def _manifest_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "worlds": [asdict(world) for world in WORLD_SPECS],
        "cases": [asdict(case) for case in GOLD_CASES],
    }


def manifest_sha256() -> str:
    encoded = json.dumps(_manifest_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# Pinned after the contract and cases were written, before the first scoring run.
GOLD_MANIFEST_SHA256 = "f5292d9f9fa4188633f140eb3efd848da14fdbcbe7b2090002801ea10780c3b5"


def _git(*args: str) -> tuple[int, bytes]:
    """Run one closed, read-only Git query from the repository root."""

    try:
        completed = subprocess.run(  # noqa: S603
            ("git", *args),
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
    except OSError:
        return 126, b""
    return completed.returncode, completed.stdout


def _load_candidate_manifest() -> dict[str, Any]:
    payload = json.loads(CANDIDATE_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate manifest must be one JSON object")
    return payload


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _head_blob(repo_path: str) -> bytes | None:
    returncode, payload = _git("show", f"HEAD:{repo_path}")
    return payload if returncode == 0 and payload else None


def candidate_manifest_complaints() -> list[str]:
    """Return closed reasons why the one frozen candidate may not spend holdout.

    The manifest and candidate must already be committed and unchanged.  Hashing
    ``base..HEAD`` rather than the working tree makes staging or local edits unable
    to open the split accidentally, while the explicit status check seals it until
    both paths are clean relative to that same HEAD.
    """

    try:
        manifest = _load_candidate_manifest()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return ["candidate_manifest_unreadable"]

    complaints: list[str] = []
    if frozenset(manifest) != _CANDIDATE_MANIFEST_FIELDS:
        complaints.append("candidate_manifest_fields_mismatch")
    if type(manifest.get("version")) is not int or manifest.get("version") != 2:
        complaints.append("candidate_manifest_version_mismatch")
    if manifest.get("candidate_id") != CANDIDATE_ID:
        complaints.append("candidate_id_mismatch")
    if manifest.get("base_commit") != CANDIDATE_BASE_COMMIT:
        complaints.append("candidate_base_mismatch")
    if manifest.get("candidate_path") != CANDIDATE_PATH:
        complaints.append("candidate_path_mismatch")
    if manifest.get("evaluator_path") != CANDIDATE_EVALUATOR_PATH:
        complaints.append("candidate_evaluator_path_mismatch")
    if manifest.get("helper_path") != CANDIDATE_HELPER_PATH:
        complaints.append("candidate_helper_path_mismatch")
    declared_gold = manifest.get("gold_manifest_sha256")
    if (
        not _is_sha256(declared_gold)
        or declared_gold != GOLD_MANIFEST_SHA256
        or declared_gold != manifest_sha256()
    ):
        complaints.append("candidate_gold_digest_mismatch")
    declared_diff = manifest.get("candidate_diff_sha256")
    if not _is_sha256(declared_diff):
        complaints.append("candidate_diff_digest_invalid")
    declared_evaluator = manifest.get("evaluator_blob_sha256")
    if not _is_sha256(declared_evaluator):
        complaints.append("candidate_evaluator_digest_invalid")
    declared_helper = manifest.get("helper_blob_sha256")
    if not _is_sha256(declared_helper):
        complaints.append("candidate_helper_digest_invalid")
    if complaints:
        return complaints

    for repo_path, complaint in (
        (CANDIDATE_MANIFEST_REPO_PATH, "candidate_manifest_not_in_head"),
        (CANDIDATE_PATH, "candidate_path_not_in_head"),
        (CANDIDATE_EVALUATOR_PATH, "candidate_evaluator_not_in_head"),
        (CANDIDATE_HELPER_PATH, "candidate_helper_not_in_head"),
    ):
        returncode, _ = _git("cat-file", "-e", f"HEAD:{repo_path}")
        if returncode != 0:
            complaints.append(complaint)

    returncode, status = _git(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        CANDIDATE_MANIFEST_REPO_PATH,
        CANDIDATE_PATH,
        CANDIDATE_EVALUATOR_PATH,
        CANDIDATE_HELPER_PATH,
    )
    if returncode != 0:
        complaints.append("candidate_status_unavailable")
    elif status:
        complaints.append("candidate_paths_not_clean")

    returncode, _ = _git("merge-base", "--is-ancestor", CANDIDATE_BASE_COMMIT, "HEAD")
    if returncode != 0:
        complaints.append("candidate_base_not_ancestor")
    if complaints:
        return complaints

    evaluator_blob = _head_blob(CANDIDATE_EVALUATOR_PATH)
    if evaluator_blob is None:
        return ["candidate_evaluator_blob_unavailable"]
    if hashlib.sha256(evaluator_blob).hexdigest() != declared_evaluator:
        return ["candidate_evaluator_digest_mismatch"]
    helper_blob = _head_blob(CANDIDATE_HELPER_PATH)
    if helper_blob is None:
        return ["candidate_helper_blob_unavailable"]
    if hashlib.sha256(helper_blob).hexdigest() != declared_helper:
        return ["candidate_helper_digest_mismatch"]

    returncode, exact_diff = _git(
        "diff",
        *_CANDIDATE_DIFF_OPTIONS,
        CANDIDATE_BASE_COMMIT,
        "HEAD",
        "--",
        CANDIDATE_PATH,
    )
    if returncode != 0:
        return ["candidate_diff_unavailable"]
    if hashlib.sha256(exact_diff).hexdigest() != declared_diff:
        return ["candidate_diff_digest_mismatch"]
    return []


def _opaque_name(world: WorldSpec, role: str) -> str:
    digest = hashlib.sha256(f"{world.id}:{role}".encode()).hexdigest()[:12]
    labels = {
        "root": "сектор",
        "old": "оператор",
        "new": "оператор",
        "bridge": "координатор",
    }
    return f"{labels[role]} синт{digest}"


def _document_text(world: WorldSpec, role: str) -> str:
    root = _opaque_name(world, "root")
    subject = _opaque_name(world, role)
    return (
        f"Синтетическая ведомость назначает {subject} ответственным за {root}. "
        "Запись подтверждает только эту роль и намеренно не содержит поискового "
        "псевдонима. Контрольный материал создан для временного стенда."
    )


def audit_gold_set() -> list[str]:
    """Return every structural complaint; an empty list is the only valid set."""

    from friday.retrieval import _STOPWORDS, tokens_of

    complaints: list[str] = []
    if len(GOLD_CASES) != GOLD_TOTAL_CASES:
        complaints.append(f"expected {GOLD_TOTAL_CASES} cases, got {len(GOLD_CASES)}")
    if len({case.id for case in GOLD_CASES}) != len(GOLD_CASES):
        complaints.append("case ids are not unique")
    if len({world.id for world in WORLD_SPECS}) != len(WORLD_SPECS):
        complaints.append("world ids are not unique")

    counts = Counter((case.split, case.kind) for case in GOLD_CASES)
    for split in GOLD_SPLITS:
        for kind in GOLD_CLASSES:
            actual = counts[(split, kind)]
            if actual != GOLD_CASES_PER_CLASS_PER_SPLIT:
                complaints.append(f"{split}/{kind}: expected 4 cases, got {actual}")

    split_by_world: dict[str, set[str]] = {}
    for case in GOLD_CASES:
        split_by_world.setdefault(case.world_id, set()).add(case.split)
        expected_ko = set(case.expected_knowledge_ids)
        forbidden_ko = set(case.forbidden_knowledge_ids)
        expected_entities = set(case.expected_entity_ids)
        forbidden_entities = set(case.forbidden_entity_ids)
        if expected_ko & forbidden_ko:
            complaints.append(f"{case.id}: expected and forbidden knowledge overlap")
        if expected_entities & forbidden_entities:
            complaints.append(f"{case.id}: expected and forbidden entities overlap")
        if not case.as_of and not case.known_at_checkpoint:
            complaints.append(f"{case.id}: no temporal boundary")
        if not expected_ko:
            if not forbidden_ko:
                complaints.append(f"{case.id}: no-answer case has no forbidden target")
            if case.kind == "temporal_gap" and len(forbidden_ko) != 2:
                complaints.append(f"{case.id}: temporal gap must have two forbidden targets")
        elif len(expected_ko) != 1 or not forbidden_ko:
            complaints.append(f"{case.id}: positive case must have one expected and a forbidden target")

        world = next((item for item in WORLD_SPECS if item.id == case.world_id), None)
        if world is None:
            complaints.append(f"{case.id}: dangling world")
            continue
        query_tokens = {
            token.casefold()
            for token in tokens_of(case.query)
            if len(token) > 2 and token.casefold() not in _STOPWORDS and not token.isdigit()
        }
        for role in ("old", "new"):
            target_tokens = {
                token.casefold()
                for token in tokens_of(_document_text(world, role))
                if len(token) > 2 and token.casefold() not in _STOPWORDS and not token.isdigit()
            }
            shared = query_tokens & target_tokens
            if len(shared) > 1:
                complaints.append(f"{case.id}/{role}: target is lexically easy ({sorted(shared)})")
        if world.alias.casefold() in _document_text(world, "old").casefold() or world.alias.casefold() in (
            _document_text(world, "new").casefold()
        ):
            complaints.append(f"{case.id}: query alias appears in a target document")

    for world_id, splits in split_by_world.items():
        if len(splits) != 1:
            complaints.append(f"{world_id}: one world crosses splits")
    if manifest_sha256() != GOLD_MANIFEST_SHA256:
        complaints.append("gold manifest digest changed")
    return complaints


def _store_document(
    storage: Any,
    *,
    world: WorldSpec,
    role: str,
    document_date: str,
    linked_entity_id: str | None,
) -> str:
    from friday.storage.models import KnowledgeObject, RawObject

    knowledge_id = _knowledge_id(world, role)
    raw_id = f"raw-temporal-{world.id}-{role}"
    content = _document_text(world, role)
    raw = RawObject(
        id=raw_id,
        user_id=_USER_ID,
        source="synthetic-temporal-bench",
        source_ref=f"synthetic:{world.id}:{role}",
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=knowledge_id,
        user_id=_USER_ID,
        raw_object_id=raw_id,
        content=content,
        content_type="text",
        title=f"Синтетическая ведомость {role}",
        summary=content,
        metadata_json={"document_date": document_date, "synthetic_temporal_bench": True},
        importance=0.5,
        quality_score=0.8,
        promotion_score=0.8,
    )
    storage.store_knowledge_object(knowledge)
    if linked_entity_id:
        storage.link_knowledge_entity(
            _USER_ID,
            knowledge_id,
            linked_entity_id,
            status="accepted",
            confidence=1.0,
            reviewed_by=_USER_ID,
        )
    return knowledge_id


def _accept_relation(
    storage: Any,
    *,
    source: str,
    target: str,
    relation_type: str,
    evidence_id: str,
) -> str:
    candidate = storage.store_relation_candidate(
        _USER_ID,
        source,
        target,
        relation_type,
        confidence=1.0,
        evidence={
            "knowledge_object_id": evidence_id,
            "source": "synthetic_temporal_bench",
        },
    )
    candidate_id = str(candidate["id"])
    storage.review_relation_candidate(
        _USER_ID,
        candidate_id,
        "accepted",
        reviewed_by=_USER_ID,
    )
    row = storage.execute(
        """SELECT id FROM relations
             WHERE user_id=? AND json_extract(metadata_json,'$.candidate_id')=?
             ORDER BY created_at DESC, id DESC LIMIT 1""",
        (_USER_ID, candidate_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("reviewed synthetic relation was not created")
    return str(row["id"])


def _revision_boundary(storage: Any, relation_id: str) -> str:
    row = storage.execute(
        """SELECT recorded_at FROM relation_revisions
             WHERE user_id=? AND relation_id=? ORDER BY event_seq DESC LIMIT 1""",
        (_USER_ID, relation_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("synthetic relation revision is missing")
    return str(row["recorded_at"])


def _create_entities(storage: Any, world: WorldSpec) -> None:
    from friday.storage.models import Entity, EntityType

    entities = (
        Entity(
            id=_entity_id(world, "root"),
            user_id=_USER_ID,
            name=_opaque_name(world, "root"),
            entity_type=EntityType.PROJECT,
            aliases_json=[world.alias],
            metadata_json={"synthetic_temporal_bench": True},
        ),
        Entity(
            id=_entity_id(world, "old"),
            user_id=_USER_ID,
            name=_opaque_name(world, "old"),
            entity_type=EntityType.PERSON,
            metadata_json={"synthetic_temporal_bench": True},
        ),
        Entity(
            id=_entity_id(world, "new"),
            user_id=_USER_ID,
            name=_opaque_name(world, "new"),
            entity_type=EntityType.PERSON,
            metadata_json={"synthetic_temporal_bench": True},
        ),
    )
    for entity in entities:
        storage.create_entity(entity)
    if world.kind == "two_hop_chain":
        storage.create_entity(
            Entity(
                id=_entity_id(world, "bridge"),
                user_id=_USER_ID,
                name=_opaque_name(world, "bridge"),
                entity_type=EntityType.ORGANIZATION,
                metadata_json={"synthetic_temporal_bench": True},
            )
        )


def _prepare_world_material(storage: Any, world: WorldSpec) -> None:
    """Create all current identities before the first known-at checkpoint.

    Historical traversal deliberately rejects a current seed entity that did not
    exist at the requested transaction boundary.  Building one whole world at a
    time made the first world's checkpoint predate the second world's entities,
    while the shared lexical pool could still select their documents as seeds.
    Production was right to refuse that torn fixture, so material and revisions are
    built in two explicit phases.
    """

    _create_entities(storage, world)
    old = _entity_id(world, "old")
    new = _entity_id(world, "new")
    old_date = f"{world.year - 2}-01-01"
    _store_document(
        storage,
        world=world,
        role="old",
        document_date=old_date,
        linked_entity_id=old,
    )
    new_start = (
        world.year + 1
        if world.kind in {"valid_time_handover", "two_hop_chain", "temporal_gap"}
        else world.year - 1
    )
    _store_document(
        storage,
        world=world,
        role="new",
        document_date=f"{new_start}-01-01",
        linked_entity_id=new,
    )
    if world.kind == "two_hop_chain":
        _store_document(
            storage,
            world=world,
            role="bridge",
            document_date=f"{world.year - 3}-01-01",
            linked_entity_id=_entity_id(world, "bridge"),
        )


def _build_world(storage: Any, graph: Any, world: WorldSpec) -> dict[str, str]:
    from friday.storage.models import RelationType

    root = _entity_id(world, "root")
    old = _entity_id(world, "old")
    new = _entity_id(world, "new")
    old_ko = _knowledge_id(world, "old")
    new_ko = _knowledge_id(world, "new")
    checkpoints: dict[str, str] = {}

    if world.kind == "two_hop_chain":
        bridge = _entity_id(world, "bridge")
        bridge_ko = _knowledge_id(world, "bridge")
        _accept_relation(
            storage,
            source=root,
            target=bridge,
            relation_type=RelationType.DEPENDS_ON.value,
            evidence_id=bridge_ko,
        )
        old_relation = _accept_relation(
            storage,
            source=old,
            target=bridge,
            relation_type=RelationType.WORKS_ON.value,
            evidence_id=old_ko,
        )
        graph.invalidate_relation(
            _USER_ID,
            old_relation,
            valid_to=f"{world.year + 1}-01-01",
            reason="synthetic handover",
        )
        _accept_relation(
            storage,
            source=new,
            target=bridge,
            relation_type=RelationType.WORKS_ON.value,
            evidence_id=new_ko,
        )
    else:
        old_relation = _accept_relation(
            storage,
            source=old,
            target=root,
            relation_type=RelationType.MANAGES.value,
            evidence_id=old_ko,
        )
        if world.kind in {"known_at_late_end", "bitemporal_replacement"}:
            checkpoints["initial"] = _revision_boundary(storage, old_relation)
            graph.invalidate_relation(
                _USER_ID,
                old_relation,
                valid_to=f"{world.year - 1}-01-01",
                reason="synthetic late correction",
            )
            corrected_relation = old_relation
            if world.kind == "bitemporal_replacement":
                corrected_relation = _accept_relation(
                    storage,
                    source=new,
                    target=root,
                    relation_type=RelationType.MANAGES.value,
                    evidence_id=new_ko,
                )
            checkpoints["corrected"] = _revision_boundary(storage, corrected_relation)
        else:
            end_year = world.year if world.kind == "temporal_gap" else world.year + 1
            graph.invalidate_relation(
                _USER_ID,
                old_relation,
                valid_to=f"{end_year}-01-01",
                reason="synthetic handover",
            )
            _accept_relation(
                storage,
                source=new,
                target=root,
                relation_type=RelationType.MANAGES.value,
                evidence_id=new_ko,
            )
    return checkpoints


def _store_fillers(storage: Any, count: int = 120) -> None:
    from friday.storage.models import KnowledgeObject, RawObject

    for index in range(count):
        content = (
            f"Синтетическая фоновая заметка {index:03d}. План, отчёт, инструкция, "
            "проверка, календарь и резервная копия описаны без отношений между сущностями."
        )
        raw_id = f"raw-temporal-filler-{index:03d}"
        knowledge_id = f"ko-temporal-filler-{index:03d}"
        storage.store_raw_object(
            RawObject(
                id=raw_id,
                user_id=_USER_ID,
                source="synthetic-temporal-bench",
                source_ref=f"synthetic:filler:{index:03d}",
                raw_content=content,
                content_type="text",
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
            )
        )
        storage.store_knowledge_object(
            KnowledgeObject(
                id=knowledge_id,
                user_id=_USER_ID,
                raw_object_id=raw_id,
                content=content,
                content_type="text",
                title=f"Фоновая заметка {index:03d}",
                summary=content,
                metadata_json={"synthetic_temporal_bench": True},
                importance=0.5,
                quality_score=0.8,
                promotion_score=0.8,
            )
        )


def build_runtime_cases(
    storage: Any,
    *,
    split: str,
    include_fillers: bool = True,
) -> tuple[Any, list[RuntimeCase]]:
    """Build one split and resolve symbolic transaction-time checkpoints."""

    from friday.knowledge_graph import KnowledgeGraph

    if split not in GOLD_SPLITS:
        raise ValueError("unknown gold split")
    storage.ensure_user(_USER_ID)
    graph = KnowledgeGraph(storage)
    checkpoints_by_world: dict[str, dict[str, str]] = {}
    for world in WORLD_SPECS:
        if world.split == split:
            _prepare_world_material(storage, world)
    if include_fillers:
        _store_fillers(storage)
    for world in WORLD_SPECS:
        if world.split == split:
            checkpoints_by_world[world.id] = _build_world(storage, graph, world)

    runtime: list[RuntimeCase] = []
    for case in GOLD_CASES:
        if case.split != split:
            continue
        checkpoints = checkpoints_by_world[case.world_id]
        known_at = checkpoints.get(case.known_at_checkpoint, "")
        if case.known_at_checkpoint and not known_at:
            raise RuntimeError("symbolic known_at checkpoint did not resolve")
        runtime.append(RuntimeCase(case, known_at))
    return graph, runtime


def _graph_truth_complaints(graph: Any, runtime_cases: list[RuntimeCase]) -> list[str]:
    complaints: list[str] = []
    for runtime in runtime_cases:
        case = runtime.spec
        context = graph.context_for_query(
            _USER_ID,
            case.query,
            depth=2,
            as_of=case.as_of,
            known_at=runtime.known_at,
        )
        node_ids = {str(item.get("id") or "") for item in context.get("nodes", [])}
        paths = context.get("paths", [])
        relation_rows = context.get("relations", [])
        if any(bool(item.get("implicit")) for item in relation_rows):
            complaints.append(f"{case.id}: temporal truth used an implicit relation")
        for entity_id in case.expected_entity_ids:
            if entity_id not in node_ids:
                complaints.append(f"{case.id}: expected entity is absent from KG truth")
            expected_paths = [path for path in paths if str(path.get("target") or "") == entity_id]
            if not expected_paths:
                complaints.append(f"{case.id}: expected entity has no published path")
            elif not any(
                any(step.get("knowledge_object_id") for step in path.get("edges", []))
                for path in expected_paths
            ):
                complaints.append(f"{case.id}: expected path has no reviewed KO provenance")
        leaked = set(case.forbidden_entity_ids) & node_ids
        if leaked:
            complaints.append(f"{case.id}: forbidden entity entered KG truth")
    return complaints


def _structural_digest(storage: Any) -> str:
    statements = (
        "SELECT * FROM relations WHERE user_id=? ORDER BY id",
        "SELECT * FROM relation_revisions WHERE user_id=? ORDER BY event_seq",
        "SELECT * FROM knowledge_entity_links WHERE user_id=? ORDER BY id",
        "SELECT * FROM knowledge_usage WHERE user_id=? ORDER BY knowledge_object_id",
    )
    payload: list[list[list[Any]]] = []
    for statement in statements:
        rows = storage.execute(statement, (_USER_ID,)).fetchall()
        payload.append([list(row) for row in rows])
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class _RerankObserver:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.calls = 0
        self.failures = 0

    async def __call__(self, query: str, documents: list[dict[str, Any]]) -> Any:
        self.calls += 1
        result = await self.callback(query, documents)
        if result is None or len(result) != len(documents):
            self.failures += 1
        return result


def _case_outcome(case: GoldCase, result_ids: list[str]) -> tuple[bool, int | None, list[int | None]]:
    expected_ranks = [
        result_ids.index(item) + 1 for item in case.expected_knowledge_ids if item in result_ids
    ]
    expected_rank = min(expected_ranks) if expected_ranks else None
    forbidden_ranks = [
        result_ids.index(item) + 1 if item in result_ids else None for item in case.forbidden_knowledge_ids
    ]
    if not case.expected_knowledge_ids:
        correct = all(rank is None for rank in forbidden_ranks)
    else:
        correct = expected_rank is not None and all(
            rank is None or expected_rank < rank for rank in forbidden_ranks
        )
    return correct, expected_rank, forbidden_ranks


async def measure_baseline(
    storage: Any,
    graph: Any,
    searcher: Any,
    runtime_cases: list[RuntimeCase],
    *,
    embeddings_required: bool,
) -> dict[str, Any]:
    """Measure one frozen split without writing any retrieval feedback."""

    splits = {runtime.spec.split for runtime in runtime_cases}
    if len(splits) != 1:
        raise ValueError("measurement requires exactly one non-empty split")
    split = next(iter(splits))

    observer: _RerankObserver | None = None
    original_reranker = getattr(searcher, "_reranker", None)
    if original_reranker is not None:
        observer = _RerankObserver(original_reranker)
        searcher._reranker = observer  # noqa: SLF001 - measurement-only observer

    before = _structural_digest(storage)
    per_case: list[dict[str, Any]] = []
    snapshot_failures = graph_failures = embedding_failures = 0
    for runtime in runtime_cases:
        case = runtime.spec
        started = time.monotonic()
        rerank_before = observer.failures if observer else 0
        try:
            result = await searcher.search(
                _USER_ID,
                case.query,
                limit=10,
                kg=graph,
                graph_expansion=True,
                as_of=case.as_of,
                known_at=runtime.known_at,
                record_usage=False,
            )
        except Exception:
            snapshot_failures += 1
            per_case.append(
                {
                    "case": case.id,
                    "class": case.kind,
                    "positive": bool(case.expected_knowledge_ids),
                    "correct": False,
                    "expected_rank": None,
                    "forbidden_ranks": [None for _ in case.forbidden_knowledge_ids],
                    "expected_entity_present": False,
                    "forbidden_entity_present": False,
                    "latency_ms": round((time.monotonic() - started) * 1000, 3),
                    "graph_failed": True,
                    "rerank_applied": False,
                    "reranker_failed": False,
                    "snapshot_failed": True,
                    "embedding_failed": False,
                }
            )
            continue

        graph_context = result.get("graph_context") if isinstance(result, dict) else None
        if not isinstance(graph_context, dict):
            graph_context = {}
        echoed = (
            graph_context.get("as_of") == case.as_of and graph_context.get("known_at") == runtime.known_at
        )
        expanded = graph_context.get("expanded") is True
        graph_failed = not (echoed and expanded)
        graph_failures += int(graph_failed)
        strategy = result.get("strategy") if isinstance(result.get("strategy"), dict) else {}
        embedding_failed = embeddings_required and strategy.get("embeddings") is not True
        embedding_failures += int(embedding_failed)
        rerank_applied = type(strategy.get("reranked")) is int and int(strategy["reranked"]) > 0
        reranker_failed = bool(observer and observer.failures > rerank_before)
        results = result.get("results") if isinstance(result.get("results"), list) else []
        result_ids = [str(item.get("id") or "") for item in results if isinstance(item, dict)]
        correct, expected_rank, forbidden_ranks = _case_outcome(case, result_ids[:10])
        node_ids = {
            str(item.get("id") or "") for item in graph_context.get("nodes", []) if isinstance(item, dict)
        }
        per_case.append(
            {
                "case": case.id,
                "class": case.kind,
                "positive": bool(case.expected_knowledge_ids),
                "correct": bool(
                    correct and not graph_failed and not reranker_failed and not embedding_failed
                ),
                "expected_rank": expected_rank,
                "forbidden_ranks": forbidden_ranks,
                "expected_entity_present": set(case.expected_entity_ids).issubset(node_ids),
                "forbidden_entity_present": bool(set(case.forbidden_entity_ids) & node_ids),
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
                "graph_failed": graph_failed,
                "rerank_applied": rerank_applied,
                "reranker_failed": reranker_failed,
                "snapshot_failed": False,
                "embedding_failed": embedding_failed,
            }
        )
    after = _structural_digest(storage)
    correct_total = sum(bool(item["correct"]) for item in per_case)
    positive_cases = [item for item in per_case if bool(item["positive"])]
    no_answer_cases = [item for item in per_case if not bool(item["positive"])]
    expected_hits_at_10 = sum(item["expected_rank"] is not None for item in positive_cases)
    forbidden_hits_at_10 = sum(any(rank is not None for rank in item["forbidden_ranks"]) for item in per_case)
    latencies = [float(item["latency_ms"]) for item in per_case]
    by_class = {
        kind: {
            "cases": sum(item["class"] == kind for item in per_case),
            "correct": sum(item["class"] == kind and bool(item["correct"]) for item in per_case),
            "positive_cases": sum(item["class"] == kind and bool(item["positive"]) for item in per_case),
            "positive_correct": sum(
                item["class"] == kind and bool(item["positive"]) and bool(item["correct"])
                for item in per_case
            ),
        }
        for kind in GOLD_CLASSES
    }
    return {
        "fixture_sha256": GOLD_MANIFEST_SHA256,
        "split": split,
        "cases": len(per_case),
        "correct": correct_total,
        "case_correct_at_10": round(correct_total / len(per_case), 4) if per_case else None,
        # Standard MRR: every positive query is in the denominator and a miss
        # contributes zero.  Dividing only by found ranks reports conditional mean
        # reciprocal rank and hides recall loss.
        "mrr": round(
            sum(
                1.0 / int(item["expected_rank"])
                for item in positive_cases
                if item["expected_rank"] is not None
            )
            / len(positive_cases),
            4,
        )
        if positive_cases
        else 0.0,
        "positive_cases": len(positive_cases),
        "expected_hits_at_10": expected_hits_at_10,
        "no_answer_cases": len(no_answer_cases),
        "no_answer_correct": sum(bool(item["correct"]) for item in no_answer_cases),
        "forbidden_hits_at_10": forbidden_hits_at_10,
        "positive_expected_entity_present": sum(
            bool(item["expected_entity_present"]) for item in positive_cases
        ),
        "forbidden_entity_present": sum(bool(item["forbidden_entity_present"]) for item in per_case),
        "by_class": by_class,
        "p50_latency_ms": round(statistics.median(latencies), 3) if latencies else None,
        "p95_latency_ms": (
            round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 3)
            if latencies
            else None
        ),
        "graph_failures": graph_failures,
        "rerank_applied_cases": sum(bool(item["rerank_applied"]) for item in per_case),
        "reranker_calls": observer.calls if observer else 0,
        "reranker_failures": observer.failures if observer else 0,
        "snapshot_failures": snapshot_failures,
        "embedding_failures": embedding_failures,
        "structure_unchanged": before == after,
        "per_case": per_case,
    }


async def measure_non_temporal_control(
    storage: Any,
    graph: Any,
    searcher: Any,
    runtime_cases: list[RuntimeCase],
) -> dict[str, Any]:
    """Serialize only deterministic result ordering for boundary-free queries.

    The two process-isolated arms have separate SQLite files and transaction
    timestamps.  Comparing public result IDs in exact rank order deliberately
    excludes those dynamic fields while retaining the observable retrieval
    decision that the temporal-only candidate must leave byte-identical.
    """

    splits = {runtime.spec.split for runtime in runtime_cases}
    if len(splits) != 1:
        raise ValueError("control requires exactly one non-empty split")
    split = next(iter(splits))

    observer: _RerankObserver | None = None
    original_reranker = getattr(searcher, "_reranker", None)
    if original_reranker is not None:
        observer = _RerankObserver(original_reranker)
        searcher._reranker = observer  # noqa: SLF001 - measurement-only observer

    before = _structural_digest(storage)
    failures = 0
    projection: list[dict[str, Any]] = []
    try:
        for runtime in runtime_cases:
            case = runtime.spec
            try:
                result = await searcher.search(
                    _USER_ID,
                    case.query,
                    limit=10,
                    kg=graph,
                    graph_expansion=True,
                    as_of=None,
                    known_at=None,
                    record_usage=False,
                )
            except Exception:
                failures += 1
                projection.append({"case": case.id, "result_ids": []})
                continue
            results = result.get("results") if isinstance(result, dict) else None
            if not isinstance(results, list):
                failures += 1
                projection.append({"case": case.id, "result_ids": []})
                continue
            result_ids = [str(item.get("id") or "") for item in results[:10] if isinstance(item, dict)]
            if len(result_ids) != len(results[:10]) or any(not item for item in result_ids):
                failures += 1
            projection.append({"case": case.id, "result_ids": result_ids})
    finally:
        if observer is not None:
            searcher._reranker = original_reranker  # noqa: SLF001 - restore measured searcher

    after = _structural_digest(storage)
    canonical = json.dumps(
        {
            "contract": "non_temporal_ranking_projection_v1",
            "fixture_sha256": GOLD_MANIFEST_SHA256,
            "split": split,
            "projection": projection,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "contract": "non_temporal_ranking_projection_v1",
        "fixture_sha256": GOLD_MANIFEST_SHA256,
        "split": split,
        "cases": len(projection),
        "failures": failures,
        "reranker_calls": observer.calls if observer else 0,
        "reranker_failures": observer.failures if observer else 0,
        "structure_unchanged": before == after,
        "projection_sha256": hashlib.sha256(canonical).hexdigest(),
        "projection": projection,
    }


def calibration_acceptance(report: dict[str, Any]) -> dict[str, Any]:
    """Evaluate instrument readiness without opening or comparing the holdout."""

    by_class = report.get("by_class") if isinstance(report.get("by_class"), dict) else {}
    uncovered_classes = [
        kind
        for kind in GOLD_CLASSES
        if int((by_class.get(kind) or {}).get("positive_correct") or 0)
        < CALIBRATION_MIN_POSITIVE_CORRECT_PER_CLASS
    ]
    no_answer_cases = int(report.get("no_answer_cases") or 0)
    checks = {
        "frozen_case_counts": int(report.get("cases") or 0) == CALIBRATION_TOTAL_CASES
        and int(report.get("positive_cases") or 0) == CALIBRATION_POSITIVE_CASES
        and no_answer_cases == CALIBRATION_NO_ANSWER_CASES,
        "minimum_correct_at_10": int(report.get("correct") or 0) >= CALIBRATION_MIN_CORRECT_AT_10,
        "minimum_expected_hits_at_10": int(report.get("expected_hits_at_10") or 0)
        >= CALIBRATION_MIN_EXPECTED_HITS_AT_10,
        "all_no_answer_cases_correct": no_answer_cases > 0
        and int(report.get("no_answer_correct") or 0) == no_answer_cases,
        "no_forbidden_hits_at_10": int(report.get("forbidden_hits_at_10") or 0)
        <= CALIBRATION_MAX_FORBIDDEN_HITS_AT_10,
        "every_positive_class_covered": not uncovered_classes,
    }
    return {
        "contract": "post_baseline_v1",
        "accepted": all(checks.values()),
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "uncovered_positive_classes": uncovered_classes,
        "thresholds": {
            "total_cases": CALIBRATION_TOTAL_CASES,
            "positive_cases": CALIBRATION_POSITIVE_CASES,
            "no_answer_cases": CALIBRATION_NO_ANSWER_CASES,
            "minimum_correct_at_10": CALIBRATION_MIN_CORRECT_AT_10,
            "minimum_expected_hits_at_10": CALIBRATION_MIN_EXPECTED_HITS_AT_10,
            "maximum_forbidden_hits_at_10": CALIBRATION_MAX_FORBIDDEN_HITS_AT_10,
            "minimum_positive_correct_per_class": CALIBRATION_MIN_POSITIVE_CORRECT_PER_CLASS,
        },
    }


def _measurement_report_complaints(report: object, split: str) -> list[str]:
    """Validate and recompute an arm report before trusting subprocess output."""

    if not isinstance(report, dict):
        return ["arm_report_not_object"]
    complaints: list[str] = []
    if frozenset(report) != _ARM_REPORT_KEYS:
        complaints.append("arm_report_fields_mismatch")
    if report.get("fixture_sha256") != GOLD_MANIFEST_SHA256:
        complaints.append("arm_report_fixture_mismatch")
    if report.get("split") != split:
        complaints.append("arm_report_split_mismatch")

    expected_specs = [case for case in GOLD_CASES if case.split == split]
    per_case = report.get("per_case")
    if not isinstance(per_case, list):
        return [*complaints, "arm_report_cases_not_list"]
    if len(per_case) != len(expected_specs):
        complaints.append("arm_report_case_count_mismatch")
        return complaints

    expected_ids = [case.id for case in expected_specs]
    actual_ids = [item.get("case") if isinstance(item, dict) else None for item in per_case]
    if actual_ids != expected_ids:
        complaints.append("arm_report_case_order_mismatch")

    structurally_valid = True
    for row, case in zip(per_case, expected_specs, strict=True):
        if not isinstance(row, dict) or frozenset(row) != _PER_CASE_REPORT_KEYS:
            structurally_valid = False
            continue
        if row.get("case") != case.id or row.get("class") != case.kind:
            structurally_valid = False
        if type(row.get("positive")) is not bool or row.get("positive") is not bool(
            case.expected_knowledge_ids
        ):
            structurally_valid = False
        for key in (
            "correct",
            "expected_entity_present",
            "forbidden_entity_present",
            "graph_failed",
            "rerank_applied",
            "reranker_failed",
            "snapshot_failed",
            "embedding_failed",
        ):
            if type(row.get(key)) is not bool:
                structurally_valid = False
        expected_rank = row.get("expected_rank")
        if expected_rank is not None and (type(expected_rank) is not int or expected_rank < 1):
            structurally_valid = False
        forbidden_ranks = row.get("forbidden_ranks")
        if (
            not isinstance(forbidden_ranks, list)
            or len(forbidden_ranks) != len(case.forbidden_knowledge_ids)
            or any(rank is not None and (type(rank) is not int or rank < 1) for rank in forbidden_ranks)
        ):
            structurally_valid = False
        latency = row.get("latency_ms")
        if type(latency) not in (int, float) or latency < 0:
            structurally_valid = False
    if not structurally_valid:
        complaints.append("arm_report_case_shape_invalid")
        return complaints

    positive = [row for row in per_case if row["positive"]]
    no_answer = [row for row in per_case if not row["positive"]]
    scalar_expectations = {
        "cases": len(per_case),
        "correct": sum(row["correct"] for row in per_case),
        "positive_cases": len(positive),
        "expected_hits_at_10": sum(row["expected_rank"] is not None for row in positive),
        "no_answer_cases": len(no_answer),
        "no_answer_correct": sum(row["correct"] for row in no_answer),
        "forbidden_hits_at_10": sum(
            any(rank is not None for rank in row["forbidden_ranks"]) for row in per_case
        ),
        "positive_expected_entity_present": sum(row["expected_entity_present"] for row in positive),
        "forbidden_entity_present": sum(row["forbidden_entity_present"] for row in per_case),
        "graph_failures": sum(row["graph_failed"] for row in per_case),
        "rerank_applied_cases": sum(row["rerank_applied"] for row in per_case),
        "snapshot_failures": sum(row["snapshot_failed"] for row in per_case),
        "embedding_failures": sum(row["embedding_failed"] for row in per_case),
    }
    if any(
        type(report.get(key)) is not int or report.get(key) != value
        for key, value in scalar_expectations.items()
    ):
        complaints.append("arm_report_aggregate_mismatch")
    if type(report.get("reranker_calls")) is not int or int(report["reranker_calls"]) < 0:
        complaints.append("arm_report_reranker_calls_invalid")
    if type(report.get("reranker_failures")) is not int or int(report["reranker_failures"]) < 0:
        complaints.append("arm_report_reranker_failures_invalid")
    expected_ratio = round(scalar_expectations["correct"] / len(per_case), 4)
    if (
        type(report.get("case_correct_at_10")) not in (int, float)
        or report.get("case_correct_at_10") != expected_ratio
    ):
        complaints.append("arm_report_correct_ratio_mismatch")
    expected_mrr = (
        round(
            sum(1.0 / row["expected_rank"] for row in positive if row["expected_rank"] is not None)
            / len(positive),
            4,
        )
        if positive
        else 0.0
    )
    if type(report.get("mrr")) not in (int, float) or report.get("mrr") != expected_mrr:
        complaints.append("arm_report_mrr_mismatch")
    if type(report.get("structure_unchanged")) is not bool:
        complaints.append("arm_report_structure_flag_invalid")
    for key in ("p50_latency_ms", "p95_latency_ms"):
        value = report.get(key)
        if type(value) not in (int, float) or value < 0:
            complaints.append("arm_report_latency_invalid")
            break

    expected_by_class = {
        kind: {
            "cases": sum(row["class"] == kind for row in per_case),
            "correct": sum(row["class"] == kind and row["correct"] for row in per_case),
            "positive_cases": sum(row["class"] == kind and row["positive"] for row in per_case),
            "positive_correct": sum(
                row["class"] == kind and row["positive"] and row["correct"] for row in per_case
            ),
        }
        for kind in GOLD_CLASSES
    }
    if report.get("by_class") != expected_by_class:
        complaints.append("arm_report_class_aggregate_mismatch")
    return complaints


def _control_projection_bytes(control: dict[str, Any]) -> bytes:
    payload = {
        "contract": control.get("contract"),
        "fixture_sha256": control.get("fixture_sha256"),
        "split": control.get("split"),
        "projection": control.get("projection"),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _control_report_complaints(control: object, split: str) -> list[str]:
    if not isinstance(control, dict):
        return ["control_report_not_object"]
    if frozenset(control) != {
        "contract",
        "fixture_sha256",
        "split",
        "cases",
        "failures",
        "reranker_calls",
        "reranker_failures",
        "structure_unchanged",
        "projection_sha256",
        "projection",
    }:
        return ["control_report_fields_mismatch"]
    complaints: list[str] = []
    if control.get("contract") != "non_temporal_ranking_projection_v1":
        complaints.append("control_contract_mismatch")
    if control.get("fixture_sha256") != GOLD_MANIFEST_SHA256:
        complaints.append("control_fixture_mismatch")
    if control.get("split") != split:
        complaints.append("control_split_mismatch")
    expected_ids = [case.id for case in GOLD_CASES if case.split == split]
    projection = control.get("projection")
    if not isinstance(projection, list) or len(projection) != len(expected_ids):
        complaints.append("control_projection_count_mismatch")
    else:
        actual_ids: list[object] = []
        invalid_rows = False
        for row in projection:
            if not isinstance(row, dict) or frozenset(row) != {"case", "result_ids"}:
                invalid_rows = True
                continue
            actual_ids.append(row.get("case"))
            result_ids = row.get("result_ids")
            if (
                not isinstance(result_ids, list)
                or len(result_ids) > 10
                or any(
                    not isinstance(item, str) or not item.startswith("ko-temporal-") for item in result_ids
                )
            ):
                invalid_rows = True
        if actual_ids != expected_ids:
            complaints.append("control_case_order_mismatch")
        if invalid_rows:
            complaints.append("control_projection_shape_invalid")
    for key in ("cases", "failures", "reranker_calls", "reranker_failures"):
        if type(control.get(key)) is not int or int(control[key]) < 0:
            complaints.append("control_counter_invalid")
            break
    if control.get("cases") != len(expected_ids):
        complaints.append("control_case_count_mismatch")
    if type(control.get("structure_unchanged")) is not bool:
        complaints.append("control_structure_flag_invalid")
    digest = hashlib.sha256(_control_projection_bytes(control)).hexdigest()
    if not _is_sha256(control.get("projection_sha256")) or control.get("projection_sha256") != digest:
        complaints.append("control_projection_digest_mismatch")
    return complaints


def exact_base_calibration_acceptance(
    report: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove that the archived base reproduces the already published defect."""

    runtime = runtime or {}
    provenance = provenance or {}
    checks = {
        "exact_case_counts": int(report.get("cases") or 0) == EXACT_BASE_CALIBRATION_TOTAL
        and int(report.get("positive_cases") or 0) == EXACT_BASE_CALIBRATION_POSITIVE_CASES,
        "exact_correct_at_10": int(report.get("correct") or 0) == EXACT_BASE_CALIBRATION_CORRECT,
        "exact_expected_hits_at_10": int(report.get("expected_hits_at_10") or 0)
        == EXACT_BASE_CALIBRATION_EXPECTED_HITS_AT_10,
        "exact_no_answer": int(report.get("no_answer_cases") or 0) == EXACT_BASE_CALIBRATION_NO_ANSWER_CASES
        and int(report.get("no_answer_correct") or 0) == EXACT_BASE_CALIBRATION_NO_ANSWER_CORRECT,
        "exact_forbidden_hits_at_10": int(report.get("forbidden_hits_at_10") or 0)
        == EXACT_BASE_CALIBRATION_FORBIDDEN_HITS_AT_10,
        "exact_mrr": float(report.get("mrr") or 0.0) == EXACT_BASE_CALIBRATION_MRR,
        "zero_infrastructure_failures": _infrastructure_failure_count(report) == 0,
        "production_reranker_proven": runtime.get("reranker_configured") is True
        and int(runtime.get("rerank_top") or 0) == 40
        and float(runtime.get("rerank_confident_min") or 0.0) == 0.10
        and int(report.get("reranker_calls") or 0) >= 16
        and int(report.get("rerank_applied_cases") or 0) >= 16,
        "embedding_index_complete": runtime.get("embeddings_remote_enabled") is True
        and runtime.get("embedding_index_complete") is True
        and int(runtime.get("embedding_object_vectors") or 0) > 0,
        "archive_only_provenance": provenance
        == {
            "contract": "git_object_package_v1",
            "base_commit": CANDIDATE_BASE_COMMIT,
            "package": "base_archive",
            "friday_modules_confined": True,
        },
    }
    return {
        "contract": "exact_base_calibration_v1",
        "accepted": all(checks.values()),
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "expected": {
            "cases": EXACT_BASE_CALIBRATION_TOTAL,
            "correct": EXACT_BASE_CALIBRATION_CORRECT,
            "positive_cases": EXACT_BASE_CALIBRATION_POSITIVE_CASES,
            "expected_hits_at_10": EXACT_BASE_CALIBRATION_EXPECTED_HITS_AT_10,
            "no_answer_cases": EXACT_BASE_CALIBRATION_NO_ANSWER_CASES,
            "no_answer_correct": EXACT_BASE_CALIBRATION_NO_ANSWER_CORRECT,
            "forbidden_hits_at_10": EXACT_BASE_CALIBRATION_FORBIDDEN_HITS_AT_10,
            "mrr": EXACT_BASE_CALIBRATION_MRR,
        },
    }


def candidate_calibration_acceptance(
    report: dict[str, Any],
    *,
    runtime: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Require both frozen quality and the production retrieval dependencies."""

    quality = calibration_acceptance(report)
    runtime_checks = {
        "remote_embeddings_proven": runtime.get("embeddings_remote_enabled") is True,
        "embedding_index_complete": runtime.get("embedding_index_complete") is True
        and int(runtime.get("embedding_object_vectors") or 0) > 0,
        "production_reranker_proven": runtime.get("reranker_configured") is True
        and int(runtime.get("rerank_top") or 0) == 40
        and float(runtime.get("rerank_confident_min") or 0.0) == 0.10,
        "usage_disabled": runtime.get("record_usage") is False,
        "zero_infrastructure_failures": _infrastructure_failure_count(report) == 0,
        "structure_unchanged": report.get("structure_unchanged") is True,
        "candidate_archive_provenance": provenance
        == {
            "contract": "git_object_package_v1",
            "base_commit": CANDIDATE_BASE_COMMIT,
            "package": "base_archive_plus_head_retrieval",
            "friday_modules_confined": True,
        },
    }
    checks = {**quality["checks"], **runtime_checks}
    return {
        "contract": "sealed_candidate_calibration_v1",
        "accepted": all(checks.values()),
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "quality": quality,
    }


def _infrastructure_failure_count(report: dict[str, Any]) -> int:
    failures = sum(
        int(report.get(key) or 0)
        for key in (
            "graph_failures",
            "reranker_failures",
            "snapshot_failures",
            "embedding_failures",
        )
    )
    return failures + int(report.get("structure_unchanged") is not True)


class _ClosedArmError(RuntimeError):
    """Carry one public enum while deliberately discarding exception contents."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def _extract_exact_base_friday(destination: Path) -> None:
    """Materialize only ``friday/`` from the pinned Git object, never a worktree."""

    returncode, archive = _git(
        "archive",
        "--format=tar",
        CANDIDATE_BASE_COMMIT,
        "--",
        "friday",
    )
    if returncode != 0 or not archive:
        raise _ClosedArmError("base_archive_failed")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as payload:
            members = payload.getmembers()
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or not path.parts
                    or path.parts[0] != "friday"
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or not (member.isfile() or member.isdir())
                ):
                    raise _ClosedArmError("base_archive_shape_invalid")
            for member in members:
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = payload.extractfile(member)
                if source is None:
                    raise _ClosedArmError("base_archive_shape_invalid")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
    except _ClosedArmError:
        raise
    except (OSError, tarfile.TarError):
        raise _ClosedArmError("base_archive_unreadable") from None
    if not (destination / "friday" / "__init__.py").is_file():
        raise _ClosedArmError("base_package_missing")
    try:
        if {item.name for item in destination.iterdir()} != {"friday"}:
            raise _ClosedArmError("base_archive_scope_invalid")
    except OSError:
        raise _ClosedArmError("base_archive_scope_unreadable") from None


def _prepare_paired_package_trees(root: Path) -> tuple[Path, Path]:
    """Create base and one-file candidate trees entirely from committed Git objects."""

    base_root = root / "base"
    candidate_root = root / "candidate"
    base_root.mkdir(parents=True)
    _extract_exact_base_friday(base_root)
    try:
        shutil.copytree(base_root / "friday", candidate_root / "friday")
    except OSError:
        raise _ClosedArmError("candidate_base_copy_failed") from None
    returncode, candidate_blob = _git("show", f"HEAD:{CANDIDATE_PATH}")
    if returncode != 0 or not candidate_blob:
        raise _ClosedArmError("candidate_blob_unavailable")
    candidate_path = candidate_root / CANDIDATE_PATH
    try:
        candidate_path.write_bytes(candidate_blob)
    except OSError:
        raise _ClosedArmError("candidate_overlay_failed") from None
    if {item.name for item in candidate_root.iterdir()} != {"friday"}:
        raise _ClosedArmError("candidate_overlay_scope_invalid")
    return base_root, candidate_root


def _path_is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _friday_modules_confined(package_root: Path) -> bool:
    seen = False
    for name, module in tuple(sys.modules.items()):
        if name != "friday" and not name.startswith("friday."):
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        seen = True
        if not _path_is_inside(Path(origin), package_root / "friday"):
            return False
    return seen


def _assert_scratch_settings(settings: Any, scratch_root: Path) -> None:
    """Refuse an arm if any data-bearing setting escaped its throwaway root."""

    env_file = Path(os.environ.get("FRIDAY_ENV_FILE", ""))
    if not _path_is_inside(env_file, scratch_root) or env_file.exists():
        raise _ClosedArmError("scratch_env_file_unsafe")
    for environment_name, relative in _SCRATCH_ENV_PATHS.items():
        configured = os.environ.get(environment_name)
        if not configured or Path(configured).resolve() != (scratch_root / relative).resolve():
            raise _ClosedArmError("scratch_environment_mismatch")
    for attribute in (
        "home",
        "data_dir",
        "cache_dir",
        "log_dir",
        "model_root",
        "model_dir",
        "state_dir",
        "database_path",
        "files_dir",
        "memory_vault_dir",
        "backups_dir",
        "exports_dir",
        "backup_mirror_dir",
        "backup_encryption_key_file",
        "whisper_download_root",
        "tts_download_root",
    ):
        value = getattr(settings, attribute, None)
        if value in (None, ""):
            continue
        if not _path_is_inside(Path(value), scratch_root):
            raise _ClosedArmError("scratch_settings_escape")


def _execute_synthetic_arm(split: str, *, include_control: bool) -> dict[str, Any]:
    """Execute one arm after the child has bound imports and scratch paths."""

    try:
        from retrieval_bench import index_embeddings

        from friday.config import ensure_runtime_dirs, load_settings
        from friday.eval import _searcher_like_production
        from friday.retrieval import EmbeddingBackend
        from friday.storage import init_storage
    except Exception:
        raise _ClosedArmError("arm_import_incompatible") from None

    scratch_raw = os.environ.get(_ARM_SCRATCH_ROOT_ENV)
    if not scratch_raw:
        raise _ClosedArmError("scratch_root_missing")
    scratch_root = Path(scratch_raw).resolve()
    try:
        settings = load_settings()
        _assert_scratch_settings(settings, scratch_root)
        ensure_runtime_dirs(settings)
    except _ClosedArmError:
        raise
    except Exception:
        raise _ClosedArmError("arm_settings_incompatible") from None

    try:
        storage = init_storage(settings)
    except Exception:
        raise _ClosedArmError("arm_storage_incompatible") from None
    try:
        try:
            graph, runtime_cases = build_runtime_cases(storage, split=split)
        except Exception:
            raise _ClosedArmError("arm_fixture_incompatible") from None
        if _graph_truth_complaints(graph, runtime_cases):
            raise _ClosedArmError("arm_graph_truth_invalid")

        embeddings = EmbeddingBackend(settings) if settings.embeddings_enabled else None
        index_report: dict[str, Any] = {}
        try:
            if embeddings is not None:
                index_report = asyncio.run(index_embeddings(settings, storage, graph, embeddings))
            embedding_index_complete = bool(
                embeddings
                and embeddings.remote_enabled
                and not storage.list_knowledge_missing_embedding(
                    settings.embeddings_model,
                    limit=1,
                    chunk_scheme=None,
                    chunk_threshold=settings.embeddings_chunk_chars,
                )
            )
        except Exception:
            raise _ClosedArmError("arm_embedding_index_failed") from None

        try:
            searcher = _searcher_like_production(storage, embeddings, settings)
            reranker_enabled = getattr(searcher, "_reranker", None) is not None
            report = asyncio.run(
                measure_baseline(
                    storage,
                    graph,
                    searcher,
                    runtime_cases,
                    embeddings_required=bool(embeddings and embeddings.remote_enabled),
                )
            )
        except Exception:
            raise _ClosedArmError("arm_measurement_failed") from None

        result: dict[str, Any] = {
            "runtime": {
                "embeddings_remote_enabled": bool(embeddings and embeddings.remote_enabled),
                "reranker_configured": reranker_enabled,
                "rerank_top": int(getattr(searcher, "_rerank_top", 0) or 0),
                "rerank_confident_min": float(getattr(searcher, "_confident_min", 0.0) or 0.0),
                "embedding_index_complete": embedding_index_complete,
                "embedding_object_vectors": int(index_report.get("object_vectors") or 0),
                "embedding_chunk_vectors": int(index_report.get("chunk_vectors") or 0),
                "record_usage": False,
            },
            "report": report,
        }
        if include_control:
            try:
                control_searcher = _searcher_like_production(storage, embeddings, settings)
                result["control"] = asyncio.run(
                    measure_non_temporal_control(storage, graph, control_searcher, runtime_cases)
                )
            except Exception:
                raise _ClosedArmError("arm_control_failed") from None
        return result
    finally:
        storage.close()


def _internal_arm_process() -> int:
    """Nonce-bound child entry point; intentionally absent from the public CLI."""

    bound_nonce = os.environ.pop(_ARM_NONCE_ENV, "")
    try:
        raw_request = sys.stdin.buffer.read(131_073)
        request = json.loads(raw_request.decode("utf-8")) if len(raw_request) <= 131_072 else None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        request = None
    request_id = hashlib.sha256(bound_nonce.encode("utf-8")).hexdigest()
    arm = request.get("arm") if isinstance(request, dict) else None
    split = request.get("split") if isinstance(request, dict) else None
    include_control = request.get("include_control") if isinstance(request, dict) else None
    model_settings = request.get("settings") if isinstance(request, dict) else None
    valid_request = (
        isinstance(request, dict)
        and frozenset(request) == {"protocol", "nonce", "arm", "split", "include_control", "settings"}
        and request.get("protocol") == _ARM_PROTOCOL
        and isinstance(bound_nonce, str)
        and len(bound_nonce) == 64
        and secrets.compare_digest(str(request.get("nonce") or ""), bound_nonce)
        and arm in _ARM_KINDS
        and split in GOLD_SPLITS
        and type(include_control) is bool
        and include_control is (split == "holdout")
        and isinstance(model_settings, dict)
        and set(model_settings).issubset(_MODEL_ENV_ALLOWLIST | {"FRIDAY_LLM_ENABLED"})
        and model_settings.get("FRIDAY_LLM_ENABLED") == "0"
        and all(
            isinstance(key, str) and isinstance(value, str) and len(key) <= 100 and len(value) <= 8192
            for key, value in model_settings.items()
        )
    )
    if not valid_request:
        print(
            json.dumps(
                {
                    "protocol": _ARM_PROTOCOL,
                    "request_id": request_id,
                    "ok": False,
                    "stage": "internal_request_invalid",
                },
                sort_keys=True,
            )
        )
        return BASELINE_EXIT_INFRA_FAILURE

    protected_environment = {
        *_SCRATCH_ENV_PATHS,
        "FRIDAY_ENV_FILE",
        _ARM_PACKAGE_ROOT_ENV,
        _ARM_SCRATCH_ROOT_ENV,
        _ARM_TOOL_ROOT_ENV,
        _REPO_ROOT_ENV,
    }
    for key in tuple(os.environ):
        if key.startswith(("FRIDAY_", "JERICHO_")) and key not in protected_environment:
            os.environ.pop(key, None)
    os.environ.update(model_settings)

    if split == "holdout" and (audit_gold_set() or candidate_manifest_complaints()):
        print(
            json.dumps(
                {
                    "protocol": _ARM_PROTOCOL,
                    "request_id": request_id,
                    "ok": False,
                    "arm": arm,
                    "split": split,
                    "stage": "holdout_seal_closed",
                },
                sort_keys=True,
            )
        )
        return BASELINE_EXIT_CONTRACT_INVALID

    package_root_raw = os.environ.get(_ARM_PACKAGE_ROOT_ENV, "")
    try:
        package_root = Path(package_root_raw).resolve(strict=True)
        import friday

        friday_file = Path(str(friday.__file__)).resolve(strict=True)
        if not _path_is_inside(friday_file, package_root / "friday"):
            raise _ClosedArmError("arm_package_binding_mismatch")
    except _ClosedArmError as exc:
        stage = exc.stage
    except Exception:
        stage = "arm_package_binding_failed"
    else:
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
                payload = _execute_synthetic_arm(str(split), include_control=bool(include_control))
                if not _friday_modules_confined(package_root):
                    raise _ClosedArmError("arm_module_provenance_mismatch")
            if captured_stdout.getvalue():
                raise _ClosedArmError("arm_stdout_contaminated")
            if captured_stderr.getvalue():
                raise _ClosedArmError("arm_stderr_contaminated")
        except _ClosedArmError as exc:
            stage = exc.stage
        except Exception:
            stage = "arm_unclassified_failure"
        else:
            envelope = {
                "protocol": _ARM_PROTOCOL,
                "request_id": request_id,
                "ok": True,
                "arm": arm,
                "split": split,
                "provenance": {
                    "contract": "git_object_package_v1",
                    "base_commit": CANDIDATE_BASE_COMMIT,
                    "package": "base_archive" if arm == "exact_base" else "base_archive_plus_head_retrieval",
                    "friday_modules_confined": True,
                },
                **payload,
            }
            print(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0

    print(
        json.dumps(
            {
                "protocol": _ARM_PROTOCOL,
                "request_id": request_id,
                "ok": False,
                "arm": arm,
                "split": split,
                "stage": stage,
            },
            sort_keys=True,
        )
    )
    return BASELINE_EXIT_INFRA_FAILURE


def _parse_local_env(path: Path) -> dict[str, str]:
    """Read the same simple KEY=value grammar as config without exporting it."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise _ClosedArmError("model_env_unreadable") from None
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in _MODEL_ENV_SOURCE_KEYS:
            values[key] = value
    return values


def _model_runtime_settings() -> dict[str, str]:
    """Resolve only retrieval/model settings; never forward the operator env."""

    source: dict[str, str] = {}
    configured_file = os.environ.get("FRIDAY_ENV_FILE") or os.environ.get("JERICHO_ENV_FILE")
    if configured_file:
        source.update(_parse_local_env(Path(configured_file).expanduser().resolve()))
    for key, value in os.environ.items():
        if key in _MODEL_ENV_SOURCE_KEYS:
            source[key] = value

    selected: dict[str, str] = {}
    for canonical in _MODEL_ENV_ALLOWLIST:
        legacy = "JERICHO_" + canonical.removeprefix("FRIDAY_")
        value = source.get(canonical)
        if value is None:
            value = source.get(legacy)
        if value is not None:
            selected[canonical] = value
    llm_key = selected.get("FRIDAY_LLM_API_KEY", "")
    if not selected.get("FRIDAY_EMBEDDINGS_API_KEY") and llm_key:
        selected["FRIDAY_EMBEDDINGS_API_KEY"] = llm_key
    if not selected.get("FRIDAY_RERANK_API_KEY") and llm_key:
        selected["FRIDAY_RERANK_API_KEY"] = llm_key
    selected["FRIDAY_LLM_ENABLED"] = "0"
    if any(len(key) > 100 or len(value) > 8192 for key, value in selected.items()):
        raise _ClosedArmError("model_env_value_oversized")
    return selected


def _private_owned_entry(path: Path, *, directory: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    expected_kind = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    expected_owner = not hasattr(os, "getuid") or metadata.st_uid == os.getuid()
    return expected_kind and expected_owner and not path.is_symlink() and metadata.st_mode & 0o077 == 0


def _verified_tool_root() -> Path:
    """Return one private, capability-bound HEAD tool projection outside the repo."""

    try:
        manifest = _load_candidate_manifest()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise _ClosedArmError("committed_evaluator_manifest_unreadable") from None
    evaluator_digest = manifest.get("evaluator_blob_sha256")
    helper_digest = manifest.get("helper_blob_sha256")
    raw_root = os.environ.get(_VERIFIED_TOOL_ROOT_ENV, "")
    capability = os.environ.get(_VERIFIED_TOOL_CAPABILITY_ENV, "")
    if (
        not _is_sha256(evaluator_digest)
        or not _is_sha256(helper_digest)
        or os.environ.get(_COMMITTED_EVALUATOR_ENV) != evaluator_digest
        or not _is_sha256(capability)
        or not raw_root
    ):
        raise _ClosedArmError("committed_evaluator_binding_missing")
    unresolved_root = Path(raw_root)
    try:
        tool_root = unresolved_root.resolve(strict=True)
        common_dir = _git_common_dir()
    except (OSError, _ClosedArmError):
        raise _ClosedArmError("committed_tool_root_untrusted") from None
    if (
        unresolved_root.is_symlink()
        or _path_is_inside(tool_root, ROOT)
        or _path_is_inside(tool_root, common_dir)
        or tool_root.name != "tools"
        or not tool_root.parent.name.startswith("friday-temporal-committed-tools-")
        or not _private_owned_entry(tool_root.parent, directory=True)
        or not _private_owned_entry(tool_root, directory=True)
    ):
        raise _ClosedArmError("committed_tool_root_untrusted")
    capability_path = tool_root.parent / _TOOL_CAPABILITY_NAME
    expected_names = {
        Path(CANDIDATE_EVALUATOR_PATH).name,
        Path(CANDIDATE_HELPER_PATH).name,
    }
    try:
        if {item.name for item in tool_root.iterdir()} != expected_names:
            raise _ClosedArmError("committed_tool_root_shape_invalid")
        if {item.name for item in tool_root.parent.iterdir()} != {"tools", _TOOL_CAPABILITY_NAME}:
            raise _ClosedArmError("committed_tool_root_shape_invalid")
        if not _private_owned_entry(capability_path, directory=False):
            raise _ClosedArmError("committed_tool_capability_invalid")
        stored_capability_digest = capability_path.read_text(encoding="ascii").strip()
        if not secrets.compare_digest(
            stored_capability_digest, hashlib.sha256(capability.encode()).hexdigest()
        ):
            raise _ClosedArmError("committed_tool_capability_invalid")
    except (OSError, UnicodeError):
        raise _ClosedArmError("committed_tool_root_unreadable") from None
    evaluator_path = tool_root / Path(CANDIDATE_EVALUATOR_PATH).name
    helper_path = tool_root / Path(CANDIDATE_HELPER_PATH).name
    if not _private_owned_entry(evaluator_path, directory=False) or not _private_owned_entry(
        helper_path, directory=False
    ):
        raise _ClosedArmError("committed_tool_root_shape_invalid")
    try:
        running_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        materialized_evaluator_digest = hashlib.sha256(evaluator_path.read_bytes()).hexdigest()
        materialized_helper_digest = hashlib.sha256(helper_path.read_bytes()).hexdigest()
    except OSError:
        raise _ClosedArmError("committed_evaluator_blob_unreadable") from None
    if running_digest != evaluator_digest or materialized_evaluator_digest != evaluator_digest:
        raise _ClosedArmError("committed_evaluator_digest_mismatch")
    if materialized_helper_digest != helper_digest:
        raise _ClosedArmError("committed_helper_digest_mismatch")
    return tool_root


def _materialize_committed_tools(destination: Path, *, capability: str) -> Path:
    """Copy exactly two verified HEAD blobs into one private tool projection."""

    if not _is_sha256(capability):
        raise _ClosedArmError("committed_tool_capability_invalid")
    manifest = _load_candidate_manifest()
    try:
        destination.chmod(0o700)
        tool_root = destination / "tools"
        tool_root.mkdir(mode=0o700)
        capability_path = destination / _TOOL_CAPABILITY_NAME
        descriptor = os.open(capability_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(hashlib.sha256(capability.encode()).hexdigest())
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise _ClosedArmError("committed_tool_materialization_failed") from None
    for repo_path, digest_key, failure in (
        (CANDIDATE_EVALUATOR_PATH, "evaluator_blob_sha256", "candidate_evaluator_blob_unavailable"),
        (CANDIDATE_HELPER_PATH, "helper_blob_sha256", "candidate_helper_blob_unavailable"),
    ):
        blob = _head_blob(repo_path)
        expected = manifest.get(digest_key)
        if blob is None or not _is_sha256(expected):
            raise _ClosedArmError(failure)
        if hashlib.sha256(blob).hexdigest() != expected:
            raise _ClosedArmError(failure.replace("unavailable", "digest_mismatch"))
        try:
            target = tool_root / Path(repo_path).name
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise _ClosedArmError("committed_tool_materialization_failed") from None
    return tool_root


def _run_committed_evaluator_subprocess(
    argv: tuple[str, ...],
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=ROOT, env=environment, check=False)  # noqa: S603


def _run_through_committed_evaluator(split: str) -> int:
    """Re-exec calibration/holdout from verified HEAD blobs, never live ROOT/tools."""

    try:
        manifest = _load_candidate_manifest()
        with tempfile.TemporaryDirectory(prefix="friday-temporal-committed-tools-") as temporary_root:
            capability = secrets.token_hex(32)
            tool_root = _materialize_committed_tools(Path(temporary_root), capability=capability)
            evaluator_digest = str(manifest["evaluator_blob_sha256"])
            environment = dict(os.environ)
            environment[_REPO_ROOT_ENV] = str(ROOT)
            environment[_COMMITTED_EVALUATOR_ENV] = evaluator_digest
            environment[_VERIFIED_TOOL_ROOT_ENV] = str(tool_root)
            environment[_VERIFIED_TOOL_CAPABILITY_ENV] = capability
            argv = (
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import os,sys;"
                f"sys.path.insert(0,os.environ[{_VERIFIED_TOOL_ROOT_ENV!r}]);"
                "import temporal_relational_bench as bench;"
                "raise SystemExit(bench.main())",
                "baseline",
                "--split",
                split,
            )
            completed = _run_committed_evaluator_subprocess(argv, environment=environment)
    except (OSError, KeyError, ValueError, _ClosedArmError) as exc:
        stage = exc.stage if isinstance(exc, _ClosedArmError) else "committed_evaluator_spawn_failed"
        print(json.dumps(_closed_arm_result(stage), sort_keys=True), file=sys.stderr)
        return BASELINE_EXIT_INFRA_FAILURE
    return int(completed.returncode)


def _arm_environment(
    *,
    package_root: Path,
    scratch_root: Path,
    tool_root: Path,
    nonce: str,
) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in (
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "TZ",
            "VIRTUAL_ENV",
        )
        if key in os.environ
    }
    for name, relative in _SCRATCH_ENV_PATHS.items():
        environment[name] = str((scratch_root / relative).resolve())
    environment["FRIDAY_ENV_FILE"] = str((scratch_root / "config" / "no-env.local").resolve())
    environment[_ARM_SCRATCH_ROOT_ENV] = str(scratch_root.resolve())
    environment[_ARM_PACKAGE_ROOT_ENV] = str(package_root.resolve())
    environment[_ARM_TOOL_ROOT_ENV] = str(tool_root.resolve())
    environment[_REPO_ROOT_ENV] = str(ROOT)
    environment[_ARM_NONCE_ENV] = nonce
    environment["NO_PROXY"] = "*"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run_arm_subprocess(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    input_data: bytes,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        input=input_data,
        timeout=timeout,
    )


def _safe_child_stage(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    if any(not (character.islower() or character.isdigit() or character == "_") for character in value):
        return None
    return value


def _closed_arm_result(stage: str, *, child_stage: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "stage": stage}
    if child_stage is not None:
        result["child_stage"] = child_stage
    return result


def _invoke_isolated_arm(
    package_root: Path,
    *,
    arm: str,
    split: str,
    include_control: bool,
    model_settings: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run and validate one package arm without exposing child output contents."""

    if arm not in _ARM_KINDS or split not in GOLD_SPLITS or include_control is not (split == "holdout"):
        return _closed_arm_result("arm_parent_request_invalid")
    try:
        selected_settings = dict(model_settings) if model_settings is not None else _model_runtime_settings()
    except _ClosedArmError as exc:
        return _closed_arm_result(exc.stage)
    if (
        not set(selected_settings).issubset(_MODEL_ENV_ALLOWLIST | {"FRIDAY_LLM_ENABLED"})
        or selected_settings.get("FRIDAY_LLM_ENABLED") != "0"
        or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in selected_settings.items()
        )
    ):
        return _closed_arm_result("model_env_selection_invalid")
    nonce = secrets.token_hex(32)
    request_id = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    request_data = json.dumps(
        {
            "protocol": _ARM_PROTOCOL,
            "nonce": nonce,
            "arm": arm,
            "split": split,
            "include_control": include_control,
            "settings": selected_settings,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix=f"friday-temporal-{arm}-") as temporary_root:
        scratch_root = Path(temporary_root).resolve()
        try:
            tool_root = _verified_tool_root()
        except _ClosedArmError as exc:
            return _closed_arm_result(exc.stage)
        environment = _arm_environment(
            package_root=package_root,
            scratch_root=scratch_root,
            tool_root=tool_root,
            nonce=nonce,
        )
        argv = (
            sys.executable,
            "-I",
            "-B",
            "-c",
            f"import os,sys;sys.path[:0]=[os.environ[{_ARM_PACKAGE_ROOT_ENV!r}],"
            f"os.environ[{_ARM_TOOL_ROOT_ENV!r}]];import temporal_relational_bench as bench;"
            "raise SystemExit(bench._internal_arm_process())",
        )
        try:
            completed = _run_arm_subprocess(
                argv,
                cwd=scratch_root,
                environment=environment,
                input_data=request_data,
                timeout=_ARM_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return _closed_arm_result("arm_timeout")
        except OSError:
            return _closed_arm_result("arm_spawn_failed")

    if completed.stderr:
        return _closed_arm_result("arm_stderr_nonempty")
    try:
        envelope = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _closed_arm_result("arm_stdout_invalid")
    child_stage = _safe_child_stage(envelope.get("stage")) if isinstance(envelope, dict) else None
    if completed.returncode != 0:
        return _closed_arm_result("arm_exit_nonzero", child_stage=child_stage)
    expected_fields = {
        "protocol",
        "request_id",
        "ok",
        "arm",
        "split",
        "provenance",
        "runtime",
        "report",
    } | ({"control"} if include_control else set())
    if not isinstance(envelope, dict) or frozenset(envelope) != expected_fields:
        return _closed_arm_result("arm_envelope_fields_mismatch")
    if (
        envelope.get("protocol") != _ARM_PROTOCOL
        or envelope.get("request_id") != request_id
        or envelope.get("ok") is not True
        or envelope.get("arm") != arm
        or envelope.get("split") != split
    ):
        return _closed_arm_result("arm_envelope_binding_mismatch")
    runtime = envelope.get("runtime")
    if (
        not isinstance(runtime, dict)
        or frozenset(runtime)
        != {
            "embeddings_remote_enabled",
            "reranker_configured",
            "rerank_top",
            "rerank_confident_min",
            "embedding_index_complete",
            "embedding_object_vectors",
            "embedding_chunk_vectors",
            "record_usage",
        }
        or type(runtime.get("embeddings_remote_enabled")) is not bool
        or type(runtime.get("reranker_configured")) is not bool
        or type(runtime.get("rerank_top")) is not int
        or int(runtime["rerank_top"]) < 0
        or type(runtime.get("rerank_confident_min")) not in (int, float)
        or float(runtime["rerank_confident_min"]) < 0.0
        or type(runtime.get("embedding_index_complete")) is not bool
        or type(runtime.get("embedding_object_vectors")) is not int
        or int(runtime["embedding_object_vectors"]) < 0
        or type(runtime.get("embedding_chunk_vectors")) is not int
        or int(runtime["embedding_chunk_vectors"]) < 0
        or runtime.get("record_usage") is not False
    ):
        return _closed_arm_result("arm_runtime_shape_invalid")
    provenance = envelope.get("provenance")
    expected_package = "base_archive" if arm == "exact_base" else "base_archive_plus_head_retrieval"
    if (
        not isinstance(provenance, dict)
        or frozenset(provenance) != {"contract", "base_commit", "package", "friday_modules_confined"}
        or provenance.get("contract") != "git_object_package_v1"
        or provenance.get("base_commit") != CANDIDATE_BASE_COMMIT
        or provenance.get("package") != expected_package
        or provenance.get("friday_modules_confined") is not True
    ):
        return _closed_arm_result("arm_provenance_invalid")
    report_complaints = _measurement_report_complaints(envelope.get("report"), split)
    if report_complaints:
        return _closed_arm_result(report_complaints[0])
    if include_control:
        control_complaints = _control_report_complaints(envelope.get("control"), split)
        if control_complaints:
            return _closed_arm_result(control_complaints[0])
    return envelope


def _run_exact_base_calibration() -> dict[str, Any]:
    """Run the one provenance-backed calibration arm; never opens holdout."""

    if audit_gold_set():
        return _closed_arm_result("gold_contract_invalid")
    with tempfile.TemporaryDirectory(prefix="friday-temporal-base-tree-") as temporary_tree:
        package_root = Path(temporary_tree).resolve()
        try:
            _extract_exact_base_friday(package_root)
        except _ClosedArmError as exc:
            return _closed_arm_result(exc.stage)
        outcome = _invoke_isolated_arm(
            package_root,
            arm="exact_base",
            split="calibration",
            include_control=False,
        )
    if outcome.get("ok") is not True:
        return outcome
    report = outcome["report"]
    acceptance = exact_base_calibration_acceptance(
        report,
        runtime=outcome["runtime"],
        provenance=outcome["provenance"],
    )
    return {
        "ok": acceptance["accepted"],
        "stage": "exact_base_calibration_proven"
        if acceptance["accepted"]
        else "exact_base_calibration_mismatch",
        "provenance": outcome["provenance"],
        "runtime": outcome["runtime"],
        "report": report,
        "acceptance": acceptance,
    }


def _run_candidate_calibration() -> int:
    """Measure the exact committed candidate in the same isolated arm as holdout."""

    if audit_gold_set() or candidate_manifest_complaints():
        print(json.dumps({"valid": False, "sealed": True, "stage": "candidate_calibration_closed"}))
        return BASELINE_EXIT_CONTRACT_INVALID
    try:
        model_settings = _model_runtime_settings()
        with tempfile.TemporaryDirectory(prefix="friday-temporal-calibration-trees-") as temporary_tree:
            _baseline_root, candidate_root = _prepare_paired_package_trees(Path(temporary_tree))
            outcome = _invoke_isolated_arm(
                candidate_root,
                arm="candidate",
                split="calibration",
                include_control=False,
                model_settings=model_settings,
            )
    except _ClosedArmError as exc:
        print(json.dumps(_closed_arm_result(exc.stage), sort_keys=True))
        return BASELINE_EXIT_INFRA_FAILURE
    if outcome.get("ok") is not True:
        print(json.dumps(outcome, sort_keys=True))
        return BASELINE_EXIT_INFRA_FAILURE
    acceptance = candidate_calibration_acceptance(
        outcome["report"],
        runtime=outcome["runtime"],
        provenance=outcome["provenance"],
    )
    result = {**outcome, "acceptance": acceptance}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    runtime_failures = {
        "remote_embeddings_proven",
        "embedding_index_complete",
        "production_reranker_proven",
        "usage_disabled",
        "zero_infrastructure_failures",
        "structure_unchanged",
        "candidate_archive_provenance",
    }.intersection(acceptance["failed_checks"])
    if runtime_failures:
        return BASELINE_EXIT_INFRA_FAILURE
    return 0 if acceptance["accepted"] is True else BASELINE_EXIT_QUALITY_REJECTED


def _arm_holdout_infrastructure_failures(outcome: dict[str, Any], *, baseline: bool) -> int:
    report = outcome["report"]
    control = outcome["control"]
    runtime = outcome["runtime"]
    minimum_main_reranks = 16 if baseline else 0
    failures = _infrastructure_failure_count(report)
    failures += int(control.get("failures") or 0)
    failures += int(control.get("reranker_failures") or 0)
    failures += int(control.get("structure_unchanged") is not True)
    failures += int(runtime.get("embeddings_remote_enabled") is not True)
    failures += int(runtime.get("embedding_index_complete") is not True)
    failures += int(int(runtime.get("embedding_object_vectors") or 0) <= 0)
    failures += int(runtime.get("reranker_configured") is not True)
    failures += int(int(runtime.get("rerank_top") or 0) != 40)
    failures += int(float(runtime.get("rerank_confident_min") or 0.0) != 0.10)
    failures += int(int(report.get("reranker_calls") or 0) < minimum_main_reranks)
    failures += int(int(report.get("rerank_applied_cases") or 0) < minimum_main_reranks)
    failures += int(int(control.get("reranker_calls") or 0) < 1)
    return failures


def compare_holdout_arms(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Pair exact case IDs and produce the frozen Proposal 31 comparison."""

    if baseline.get("ok") is not True or candidate.get("ok") is not True:
        raise _ClosedArmError("paired_arm_not_validated")
    if baseline.get("split") != "holdout" or candidate.get("split") != "holdout":
        raise _ClosedArmError("paired_split_mismatch")
    baseline_rows = baseline["report"]["per_case"]
    candidate_rows = candidate["report"]["per_case"]
    baseline_by_id = {str(row["case"]): row for row in baseline_rows}
    candidate_by_id = {str(row["case"]): row for row in candidate_rows}
    expected_ids = [case.id for case in GOLD_CASES if case.split == "holdout"]
    if list(baseline_by_id) != expected_ids or list(candidate_by_id) != expected_ids:
        raise _ClosedArmError("paired_case_alignment_failed")

    win_case_ids = [
        case_id
        for case_id in expected_ids
        if not baseline_by_id[case_id]["correct"] and candidate_by_id[case_id]["correct"]
    ]
    loss_case_ids = [
        case_id
        for case_id in expected_ids
        if baseline_by_id[case_id]["correct"] and not candidate_by_id[case_id]["correct"]
    ]
    baseline_control = baseline["control"]
    candidate_control = candidate["control"]
    baseline_control_by_id = {str(row["case"]): row["result_ids"] for row in baseline_control["projection"]}
    candidate_control_by_id = {str(row["case"]): row["result_ids"] for row in candidate_control["projection"]}
    mismatch_case_ids = [
        case_id
        for case_id in expected_ids
        if baseline_control_by_id.get(case_id) != candidate_control_by_id.get(case_id)
    ]
    controls_identical = _control_projection_bytes(baseline_control) == _control_projection_bytes(
        candidate_control
    )

    baseline_report = baseline["report"]
    candidate_report = candidate["report"]
    baseline_infra = _arm_holdout_infrastructure_failures(baseline, baseline=True)
    candidate_infra = _arm_holdout_infrastructure_failures(candidate, baseline=False)
    result = {
        "contract": "paired_temporal_holdout_v1",
        "fixture_sha256": GOLD_MANIFEST_SHA256,
        "candidate_id": CANDIDATE_ID,
        "base_commit": CANDIDATE_BASE_COMMIT,
        "baseline": {
            "provenance": baseline["provenance"],
            "runtime": baseline["runtime"],
            "summary": {key: value for key, value in baseline_report.items() if key != "per_case"},
            "per_case": baseline_rows,
        },
        "candidate": {
            "provenance": candidate["provenance"],
            "runtime": candidate["runtime"],
            "summary": {key: value for key, value in candidate_report.items() if key != "per_case"},
            "per_case": candidate_rows,
        },
        "comparison": {
            "wins": len(win_case_ids),
            "losses": len(loss_case_ids),
            "net": len(win_case_ids) - len(loss_case_ids),
            "win_case_ids": win_case_ids,
            "loss_case_ids": loss_case_ids,
            "expected_hits_at_10_delta": int(candidate_report["expected_hits_at_10"])
            - int(baseline_report["expected_hits_at_10"]),
            "forbidden_hits_at_10_delta": int(candidate_report["forbidden_hits_at_10"])
            - int(baseline_report["forbidden_hits_at_10"]),
            "mrr_delta": round(float(candidate_report["mrr"]) - float(baseline_report["mrr"]), 4),
        },
        "infrastructure": {
            "baseline_failure_count": baseline_infra,
            "candidate_failure_count": candidate_infra,
            "baseline_valid": baseline_infra == 0,
            "candidate_valid": candidate_infra == 0,
            "both_valid": baseline_infra == 0 and candidate_infra == 0,
            "baseline_structure_unchanged": baseline_report["structure_unchanged"] is True
            and baseline_control["structure_unchanged"] is True,
            "candidate_structure_unchanged": candidate_report["structure_unchanged"] is True
            and candidate_control["structure_unchanged"] is True,
        },
        "non_temporal_control": {
            "contract": "byte_identical_ranking_projection_v1",
            "byte_identical": controls_identical,
            "baseline_sha256": baseline_control["projection_sha256"],
            "candidate_sha256": candidate_control["projection_sha256"],
            "mismatch_case_ids": mismatch_case_ids,
            "baseline_failures": int(baseline_control["failures"]),
            "candidate_failures": int(candidate_control["failures"]),
            "baseline_structure_unchanged": baseline_control["structure_unchanged"] is True,
            "candidate_structure_unchanged": candidate_control["structure_unchanged"] is True,
        },
    }
    result["acceptance"] = holdout_acceptance(result)
    return result


def holdout_acceptance(report: dict[str, Any]) -> dict[str, Any]:
    comparison = report.get("comparison") if isinstance(report.get("comparison"), dict) else {}
    baseline = report.get("baseline") if isinstance(report.get("baseline"), dict) else {}
    candidate = report.get("candidate") if isinstance(report.get("candidate"), dict) else {}
    baseline_summary = baseline.get("summary") if isinstance(baseline.get("summary"), dict) else {}
    candidate_summary = candidate.get("summary") if isinstance(candidate.get("summary"), dict) else {}
    infrastructure = report.get("infrastructure") if isinstance(report.get("infrastructure"), dict) else {}
    control = (
        report.get("non_temporal_control") if isinstance(report.get("non_temporal_control"), dict) else {}
    )
    checks = {
        "minimum_net_gain": int(comparison.get("net") or 0) >= HOLDOUT_MIN_NET_GAIN,
        "zero_losses": int(comparison.get("losses") or 0) <= HOLDOUT_MAX_LOSSES,
        "expected_hits_not_lower": int(candidate_summary.get("expected_hits_at_10") or 0)
        >= int(baseline_summary.get("expected_hits_at_10") or 0),
        "forbidden_hits_not_higher": int(candidate_summary.get("forbidden_hits_at_10") or 0)
        <= int(baseline_summary.get("forbidden_hits_at_10") or 0),
        "mrr_within_floor": float(candidate_summary.get("mrr") or 0.0)
        >= float(baseline_summary.get("mrr") or 0.0) - HOLDOUT_MAX_MRR_REGRESSION,
        "zero_infrastructure_failures": infrastructure.get("both_valid") is True,
        "structures_unchanged": infrastructure.get("baseline_structure_unchanged") is True
        and infrastructure.get("candidate_structure_unchanged") is True,
        "non_temporal_control_byte_identical": control.get("byte_identical") is True,
    }
    return {
        "contract": "proposal_31_holdout_acceptance_v1",
        "accepted": all(checks.values()),
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "thresholds": {
            "minimum_net_gain": HOLDOUT_MIN_NET_GAIN,
            "maximum_losses": HOLDOUT_MAX_LOSSES,
            "maximum_mrr_regression": HOLDOUT_MAX_MRR_REGRESSION,
        },
    }


def _holdout_exit_code(report: dict[str, Any]) -> int:
    infrastructure = report.get("infrastructure")
    control = report.get("non_temporal_control")
    if not isinstance(infrastructure, dict) or not isinstance(control, dict):
        return BASELINE_EXIT_INFRA_FAILURE
    if (
        infrastructure.get("both_valid") is not True
        or infrastructure.get("baseline_structure_unchanged") is not True
        or infrastructure.get("candidate_structure_unchanged") is not True
        or control.get("baseline_structure_unchanged") is not True
        or control.get("candidate_structure_unchanged") is not True
    ):
        return BASELINE_EXIT_INFRA_FAILURE
    if int(control.get("baseline_failures") or 0) or int(control.get("candidate_failures") or 0):
        return BASELINE_EXIT_INFRA_FAILURE
    acceptance = holdout_acceptance(report)
    return 0 if acceptance["accepted"] is True else BASELINE_EXIT_QUALITY_REJECTED


def _git_common_dir() -> Path:
    returncode, raw = _git("rev-parse", "--git-common-dir")
    if returncode != 0 or not raw:
        raise _ClosedArmError("holdout_latch_git_dir_unavailable")
    try:
        configured = Path(raw.decode("utf-8").strip())
    except UnicodeError:
        raise _ClosedArmError("holdout_latch_git_dir_unavailable") from None
    path = configured if configured.is_absolute() else ROOT / configured
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise _ClosedArmError("holdout_latch_git_dir_unavailable") from None
    if not resolved.is_dir():
        raise _ClosedArmError("holdout_latch_git_dir_unavailable")
    return resolved


def _holdout_attempt_binding() -> dict[str, Any]:
    manifest = _load_candidate_manifest()
    return {
        "contract": "temporal_holdout_attempt_v1",
        "candidate_id": manifest["candidate_id"],
        "base_commit": manifest["base_commit"],
        "gold_manifest_sha256": manifest["gold_manifest_sha256"],
        "candidate_diff_sha256": manifest["candidate_diff_sha256"],
        "evaluator_blob_sha256": manifest["evaluator_blob_sha256"],
        "helper_blob_sha256": manifest["helper_blob_sha256"],
    }


def _consume_holdout_attempt() -> str | None:
    """Atomically consume the one holdout attempt before either arm starts."""

    try:
        common_dir = _git_common_dir()
        marker = common_dir / _HOLDOUT_LATCH_NAME
        payload = json.dumps(
            _holdout_attempt_binding(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return "holdout_attempt_already_consumed"
    except (OSError, KeyError, TypeError, ValueError, _ClosedArmError):
        return "holdout_latch_unavailable"
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory_descriptor = os.open(common_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError:
        # The exclusive create already consumed the attempt. Never remove or retry it.
        return "holdout_latch_persistence_uncertain"
    return None


def _run_paired_holdout() -> int:
    """Spend the sealed split once, using exact committed package projections."""

    if audit_gold_set() or candidate_manifest_complaints():
        print(json.dumps({"valid": False, "sealed": True, "stage": "holdout_seal_closed"}))
        return BASELINE_EXIT_CONTRACT_INVALID
    latch_failure = _consume_holdout_attempt()
    if latch_failure is not None:
        print(json.dumps(_closed_arm_result(latch_failure), sort_keys=True))
        return BASELINE_EXIT_CONTRACT_INVALID
    try:
        model_settings = _model_runtime_settings()
    except _ClosedArmError as exc:
        print(json.dumps(_closed_arm_result(exc.stage), sort_keys=True))
        return BASELINE_EXIT_INFRA_FAILURE
    with tempfile.TemporaryDirectory(prefix="friday-temporal-paired-trees-") as temporary_tree:
        try:
            baseline_root, candidate_root = _prepare_paired_package_trees(Path(temporary_tree))
        except _ClosedArmError as exc:
            print(json.dumps(_closed_arm_result(exc.stage), sort_keys=True))
            return BASELINE_EXIT_INFRA_FAILURE
        baseline = _invoke_isolated_arm(
            baseline_root,
            arm="exact_base",
            split="holdout",
            include_control=True,
            model_settings=model_settings,
        )
        if baseline.get("ok") is not True:
            print(json.dumps(baseline, sort_keys=True))
            return BASELINE_EXIT_INFRA_FAILURE
        candidate = _invoke_isolated_arm(
            candidate_root,
            arm="candidate",
            split="holdout",
            include_control=True,
            model_settings=model_settings,
        )
        if candidate.get("ok") is not True:
            print(json.dumps(candidate, sort_keys=True))
            return BASELINE_EXIT_INFRA_FAILURE
    try:
        report = compare_holdout_arms(baseline, candidate)
    except _ClosedArmError as exc:
        print(json.dumps(_closed_arm_result(exc.stage), sort_keys=True))
        return BASELINE_EXIT_INFRA_FAILURE
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return _holdout_exit_code(report)


def _audit_command() -> int:
    complaints = audit_gold_set()
    report = {
        "fixture_sha256": GOLD_MANIFEST_SHA256,
        "cases": len(GOLD_CASES),
        "calibration": sum(case.split == "calibration" for case in GOLD_CASES),
        "holdout": sum(case.split == "holdout" for case in GOLD_CASES),
        "valid": not complaints,
        "complaints": complaints,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not complaints else BASELINE_EXIT_CONTRACT_INVALID


def _baseline_command(split: str) -> int:
    complaints = audit_gold_set()
    if split not in GOLD_SPLITS:
        print("unknown split", file=sys.stderr)
        return BASELINE_EXIT_CONTRACT_INVALID
    seal_complaints = candidate_manifest_complaints()
    if complaints or seal_complaints:
        print(
            json.dumps(
                {
                    "valid": False,
                    "sealed": True,
                    "complaints": [*complaints, *seal_complaints],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return BASELINE_EXIT_CONTRACT_INVALID
    try:
        _verified_tool_root()
    except _ClosedArmError as exc:
        if os.environ.get(_COMMITTED_EVALUATOR_ENV):
            print(json.dumps(_closed_arm_result(exc.stage), sort_keys=True), file=sys.stderr)
            return BASELINE_EXIT_INFRA_FAILURE
        return _run_through_committed_evaluator(split)
    if split == "holdout":
        return _run_paired_holdout()
    return _run_candidate_calibration()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("audit", help="Validate the frozen manifest without model calls")
    baseline = subcommands.add_parser("baseline", help="Run calibration or the sealed paired holdout")
    baseline.add_argument("--split", choices=GOLD_SPLITS, default="calibration")
    args = parser.parse_args()
    if args.command == "audit":
        return _audit_command()
    return _baseline_command(str(args.split))


if __name__ == "__main__":
    sys.exit(main())
