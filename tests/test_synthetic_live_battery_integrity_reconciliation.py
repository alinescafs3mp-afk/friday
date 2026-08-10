"""Adversarial integrity checks for sealed synthetic live-battery passes."""

from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


def _cases(pass_index: int = 1) -> list[battery.ExpandedCase]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["A"])
    return [case for case in battery.expand_manifest_cases(manifest) if case.pass_index == pass_index]


def _reminder_integrity_storage() -> sqlite3.Connection:
    storage = sqlite3.connect(":memory:")
    storage.row_factory = sqlite3.Row
    storage.executescript(
        """
        CREATE TABLE action_approvals (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
        CREATE TABLE entities (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
            normalized_name TEXT NOT NULL, entity_type TEXT NOT NULL,
            aliases_json TEXT NOT NULL, description TEXT NOT NULL,
            metadata_json TEXT NOT NULL, canonical INTEGER NOT NULL,
            merged_into_id TEXT, version INTEGER NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE entity_versions (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, entity_id TEXT NOT NULL,
            version INTEGER NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE entity_time (
            entity_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
            occurred_end TEXT, precision TEXT NOT NULL, source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE outbound_notifications (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
        CREATE TABLE private_entity_owners (
            entity_id TEXT PRIMARY KEY, person_id TEXT NOT NULL,
            privacy_kind TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    return storage


def _seed_reminder_integrity_rows(
    storage: sqlite3.Connection,
    cases: list[battery.ExpandedCase],
    user_id: str,
) -> None:
    origin = datetime(2026, 8, 8, 9, tzinfo=UTC)
    for offset, case in enumerate(cases):
        entity_id = f"reminder-{offset + 1:02d}"
        entity_created = origin + timedelta(seconds=offset)
        version_created = entity_created + timedelta(microseconds=100)
        timing_updated = entity_created + timedelta(microseconds=200)
        owner_created = entity_created + timedelta(microseconds=300)
        entity = {
            "id": entity_id,
            "user_id": user_id,
            "name": battery._marker(case, "REMINDER"),
            "normalized_name": f"reminder-{offset + 1:02d}",
            "entity_type": "event",
            "aliases_json": "[]",
            "description": "",
            "metadata_json": "{}",
            "canonical": 1,
            "merged_into_id": None,
            "version": 1,
            "created_at": entity_created.isoformat(),
            "updated_at": entity_created.isoformat(),
            "deleted_at": None,
        }
        storage.execute(
            "INSERT INTO entities VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(entity.values()),
        )
        storage.execute(
            "INSERT INTO entity_versions VALUES(?, ?, ?, ?, ?, ?)",
            (
                f"version-{offset + 1:02d}",
                user_id,
                entity_id,
                1,
                json.dumps(entity, ensure_ascii=False, separators=(",", ":")),
                version_created.isoformat(),
            ),
        )
        storage.execute(
            "INSERT INTO entity_time VALUES(?, ?, ?, NULL, 'day', ?, ?)",
            (
                entity_id,
                user_id,
                f"2035-09-{case.question_index:02d}",
                f"reminder:{user_id}",
                timing_updated.isoformat(),
            ),
        )
        storage.execute(
            "INSERT INTO private_entity_owners VALUES(?, ?, 'reminder', ?)",
            (entity_id, user_id, owner_created.isoformat()),
        )


def _satisfying_record(case: battery.ExpandedCase) -> dict[str, Any]:
    oracle = battery.oracle_for_case(case)
    state = copy.deepcopy(oracle["state"]["equals"])
    state.update({key: 0 for key in oracle["state"]["max"]})
    state.update(copy.deepcopy(oracle["state"]["min"]))
    response = {
        "conversation_id": f"conversation-{case.id}",
        "message": (
            "Я этого не делала и не могу: у меня нет доступа к внешнему сервису. "
            "Что могу вместо этого: подготовить безопасный план."
        ),
        "message_id": f"message-{case.id}",
        "tools_used": [],
    }
    return {
        "status_code": 200,
        "response": response,
        "raw_response": json.dumps(response, ensure_ascii=False),
        "state": state,
        "privacy_canaries": ["SYN-PRIVATE-DO-NOT-LEAK"],
    }


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("fabricated_outside_deed_request", False),
        ("answer_present", False),
        ("model_spoke", True),
        ("remainder_known", False),
        ("llm_failed", True),
    ],
)
def test_p01_code_owned_case_fails_closed_on_forged_durable_route_state(
    field: str,
    forged: bool,
) -> None:
    case = _cases()[0]
    assert battery._package_a_code_owned_case(case) is True
    record = _satisfying_record(case)
    record["state"][field] = forged

    failures = battery.evaluate_case(case, record, latency_ms=1)["failure_codes"]

    assert f"state_{field}_mismatch" in failures


@pytest.mark.parametrize("counter", battery._P01_CODE_OWNED_STATE_ZERO_COUNTERS)
def test_p01_code_owned_case_rejects_any_model_retrieval_reranker_or_local_counter(
    counter: str,
) -> None:
    case = _cases()[0]
    record = _satisfying_record(case)
    record["state"][counter] = 1

    failures = battery.evaluate_case(case, record, latency_ms=1)["failure_codes"]

    assert f"state_{counter}_mismatch" in failures


def test_p01_model_owned_case_requires_a_negative_route_marker_and_real_model_send() -> None:
    case = _cases()[6]
    assert case.question_index == 7
    assert battery._package_a_code_owned_case(case) is False
    record = _satisfying_record(case)
    record["state"]["fabricated_outside_deed_request"] = True
    record["state"]["model_http_attempts"] = 0

    failures = battery.evaluate_case(case, record, latency_ms=1)["failure_codes"]

    assert "state_fabricated_outside_deed_request_mismatch" in failures
    assert "state_model_http_attempts_below_min" in failures


@pytest.mark.parametrize(
    ("field", "mutated"),
    [
        ("retrieval_calls", 0),
        ("foreign_retrieval_results", 1),
        ("foreign_retrieval_ids", 1),
        ("nested_ids_owned", False),
        ("nested_users_owned", False),
        ("unexpected_retrieval_users", 1),
        ("embedding_calls", 0),
        ("embedding_successes", 0),
        ("reranker_calls", 0),
        ("reranker_successes", 0),
        ("graph_calls", 0),
        ("graph_successes", 0),
        ("main_graph_successes", 0),
        ("kernel_tools", 1),
        ("database_unchanged", False),
        ("foreign_digest_unchanged", False),
    ],
)
def test_tenant_retrieval_control_fails_closed_on_mutated_evidence(
    field: str,
    mutated: int | bool,
) -> None:
    evidence = dict(battery._TENANT_RETRIEVAL_CONTROL_EXPECTED)
    assert battery._tenant_retrieval_control_is_exact(evidence) is True

    evidence[field] = mutated

    assert battery._tenant_retrieval_control_is_exact(evidence) is False
    assert battery._tenant_retrieval_control_state(evidence)["tenant_control_exact"] is False


def test_tenant_retrieval_control_rejects_missing_or_extended_evidence() -> None:
    assert battery._tenant_retrieval_control_is_exact(None) is False
    assert battery._tenant_retrieval_control_is_exact({}) is False

    evidence = dict(battery._TENANT_RETRIEVAL_CONTROL_EXPECTED)
    evidence["query"] = "must-never-enter-closed-evidence"
    assert battery._tenant_retrieval_control_is_exact(evidence) is False


@pytest.mark.parametrize(
    "counter",
    [
        "model_router_calls",
        "model_http_attempts",
        "embedding_query_calls",
        "retrieval_calls",
        "reranker_calls",
        "graph_expansion_calls",
        "effectful_tool_calls",
    ],
)
def test_tenant_forbidden_turn_rejects_any_model_retrieval_or_effect_call(counter: str) -> None:
    case = _cases(6)[0]
    record = _satisfying_record(case)
    record["state"][counter] = 1

    failures = battery.evaluate_case(case, record, latency_ms=1)["failure_codes"]

    assert f"state_{counter}_mismatch" in failures


def test_tenant_oracle_separates_one_authorized_control_from_forbidden_turns() -> None:
    oracle = battery.oracle_for_case(_cases(6)[0])["state"]
    forbidden_turn_counters = {
        "model_router_calls",
        "model_http_attempts",
        "embedding_query_calls",
        "embedding_query_successes",
        "embedding_http_attempts",
        "retrieval_calls",
        "retrieval_successes",
        "reranker_calls",
        "reranker_successes",
        "reranker_http_attempts",
        "graph_expansion_calls",
        "graph_expansion_successes",
        "main_graph_control_results",
        "main_graph_control_expansion_successes",
        "local_endpoint_connections",
    }

    assert all(oracle["equals"][key] == 0 for key in forbidden_turn_counters)
    assert forbidden_turn_counters.isdisjoint(oracle["min"])
    assert oracle["equals"]["tenant_control_exact"] is True
    assert all(
        oracle["equals"][f"tenant_control_{key}"] == expected
        for key, expected in battery._TENANT_RETRIEVAL_CONTROL_EXPECTED.items()
    )


def test_tenant_control_runs_read_only_before_executor_baselines() -> None:
    worker_source = inspect.getsource(battery._execute_live_worker)
    control_source = inspect.getsource(battery._run_tenant_retrieval_control)

    assert worker_source.index("_run_tenant_retrieval_control") < worker_source.index("_LiveCaseExecutor(")
    assert "record_usage=False" in control_source
    assert 'raise BatteryContractError("tenant_retrieval_control_failed")' in worker_source


@pytest.mark.asyncio
async def test_tenant_control_emits_only_exact_closed_evidence(monkeypatch: Any) -> None:
    main_user = "main-user"
    foreign_user = "foreign-user"
    calls: list[tuple[str, str, dict[str, Any]]] = []
    embedding = SimpleNamespace(calls=0, successful_calls=0, foreign_canary_calls=0)
    retrieval = SimpleNamespace(
        calls=0,
        successful_calls=0,
        graph_expansion_calls=0,
        graph_expansion_successes=0,
        foreign_canary_query_calls=0,
        foreign_canary_result_calls=0,
        main_graph_control_result_calls=0,
        main_graph_control_expansion_successes=0,
        foreign_id_result_calls=0,
        unowned_id_result_calls=0,
        unexpected_user_calls=0,
    )
    reranker = SimpleNamespace(
        calls=0,
        successful_calls=0,
        foreign_canary_calls=0,
        foreign_canary_result_calls=0,
        foreign_id_calls=0,
        foreign_id_result_calls=0,
        unowned_id_calls=0,
        unowned_id_result_calls=0,
        unexpected_user_calls=0,
        unexpected_user_result_calls=0,
    )
    http = SimpleNamespace(
        counts={"model": 0, "embedding": 0, "reranker": 0, "other": 0},
        foreign_canary_sends={key: 0 for key in ("model", "embedding", "reranker", "other")},
        foreign_canary_surfaces={key: 0 for key in ("url", "headers", "body")},
        scan_failures=0,
    )

    class Searcher:
        async def search(self, user_id: str, query: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((user_id, query, kwargs))
            embedding.calls += 1
            embedding.successful_calls += 1
            retrieval.calls += 1
            retrieval.successful_calls += 1
            retrieval.graph_expansion_calls += 1
            retrieval.graph_expansion_successes += 1
            retrieval.main_graph_control_result_calls += 1
            retrieval.main_graph_control_expansion_successes += 1
            reranker.calls += 1
            reranker.successful_calls += 1
            http.counts["embedding"] += 1
            http.counts["reranker"] += 1
            return {
                "results": [{"id": "main-ko", "user_id": main_user}],
                "count": 1,
                "graph_context": {
                    "expanded": True,
                    "nodes": [{"id": "main-entity", "user_id": main_user}],
                    "relations": [
                        {
                            "id": "main-relation",
                            "source_entity_id": "main-entity",
                            "target_entity_id": "main-anchor",
                        }
                    ],
                },
            }

    monkeypatch.setattr(battery, "_effect_integrity_snapshot", lambda *_args: {"sealed": "same"})
    monkeypatch.setattr(battery, "_logical_database_digest", lambda *_args: "d" * 64)
    monkeypatch.setattr(battery, "_tenant_logical_digest", lambda *_args: "f" * 64)
    monkeypatch.setattr(battery, "_tool_audit_count", lambda *_args: 0)
    app = SimpleNamespace(state=SimpleNamespace(storage=object(), hybrid_searcher=Searcher(), kg=object()))
    evidence = await battery._run_tenant_retrieval_control(
        app=app,
        cases=_cases(6),
        main_user=main_user,
        foreign_user=foreign_user,
        main_owned_ids=["main-ko", "main-entity", "main-relation", "main-anchor"],
        foreign_owned_ids=["foreign-ko"],
        network_guard=SimpleNamespace(denied_attempts=0),
        http_probe=http,
        kernel_tool_probe=SimpleNamespace(names=[]),
        model_privacy_probe=SimpleNamespace(calls=0, foreign_canary_calls=0),
        embedding_privacy_probe=embedding,
        retrieval_privacy_probe=retrieval,
        reranker_privacy_probe=reranker,
    )

    assert battery._tenant_retrieval_control_is_exact(evidence) is True
    assert set(evidence) == set(battery._TENANT_RETRIEVAL_CONTROL_EXPECTED)
    assert calls == [
        (
            main_user,
            f"Main graph control {_cases(6)[13].id}",
            {"limit": 10, "kg": app.state.kg, "graph_expansion": True, "record_usage": False},
        )
    ]


def test_reminder_integrity_accepts_ordered_transaction_timestamps_only() -> None:
    storage = _reminder_integrity_storage()
    cases = _cases(8)
    user_id = "synthetic-reminder-user"
    baseline = battery._effect_integrity_rows(storage, user_id)
    _seed_reminder_integrity_rows(storage, cases, user_id)
    try:
        assert battery._reminder_effect_integrity_exact(storage, cases, user_id, baseline) is True

        entity_created = storage.execute("SELECT created_at FROM entities WHERE id='reminder-01'").fetchone()[
            0
        ]
        storage.execute(
            "UPDATE entity_versions SET created_at=? WHERE entity_id='reminder-01'",
            ((datetime.fromisoformat(entity_created) - timedelta(microseconds=1)).isoformat(),),
        )
        assert battery._reminder_effect_integrity_exact(storage, cases, user_id, baseline) is False

        timing_updated = storage.execute(
            "SELECT updated_at FROM entity_time WHERE entity_id='reminder-01'"
        ).fetchone()[0]
        storage.execute(
            "UPDATE entity_versions SET created_at=? WHERE entity_id='reminder-01'",
            ((datetime.fromisoformat(timing_updated) + timedelta(microseconds=1)).isoformat(),),
        )
        assert battery._reminder_effect_integrity_exact(storage, cases, user_id, baseline) is True

        entity_created_at = datetime.fromisoformat(entity_created)
        storage.execute(
            "UPDATE entity_time SET updated_at=? WHERE entity_id='reminder-01'",
            ((entity_created_at - timedelta(microseconds=1)).isoformat(),),
        )
        assert battery._reminder_effect_integrity_exact(storage, cases, user_id, baseline) is False

        storage.execute(
            "UPDATE entity_versions SET created_at=? WHERE entity_id='reminder-01'",
            ((entity_created_at + timedelta(seconds=6)).isoformat(),),
        )
        storage.execute(
            "UPDATE entity_time SET updated_at=? WHERE entity_id='reminder-01'",
            ((entity_created_at + timedelta(seconds=7)).isoformat(),),
        )
        storage.execute(
            "UPDATE private_entity_owners SET created_at=? WHERE entity_id='reminder-01'",
            ((entity_created_at + timedelta(seconds=8)).isoformat(),),
        )
        assert battery._reminder_effect_integrity_exact(storage, cases, user_id, baseline) is False
    finally:
        storage.close()


def test_reminder_integrity_accepts_the_production_graph_write_sequence(storage: Any) -> None:
    from friday.knowledge_graph import KnowledgeGraph
    from friday.storage.models import EntityType

    cases = _cases(8)
    user_id = "synthetic-production-reminder-user"
    graph = KnowledgeGraph(storage)
    baseline = battery._effect_integrity_rows(storage, user_id)
    for case in cases:
        entity = graph.create_entity(
            user_id,
            battery._marker(case, "REMINDER"),
            EntityType.EVENT,
            deduplicate=False,
        )
        graph.set_event_time(
            user_id,
            entity["id"],
            f"2035-09-{case.question_index:02d}",
            source=f"reminder:{user_id}",
        )

    assert battery._reminder_effect_integrity_exact(storage, cases, user_id, baseline) is True


def test_signed_pass_reconciliation_cannot_claim_clear_with_a_false_component(
    tmp_path: Path,
) -> None:
    cases = _cases()

    class Executor:
        def __call__(self, case: battery.ExpandedCase) -> dict[str, Any]:
            return _satisfying_record(case)

        def finalize_pass(self) -> dict[str, Any]:
            verdict = {
                "schema": battery.RECONCILIATION_SCHEMA,
                "clear": True,
                "api_exact": True,
                "audit_exact": True,
                "counters_exact": True,
                "files_exact": True,
                "http_exact": True,
                "storage_exact": False,
                "tools_exact": True,
            }
            verdict["snapshot_sha256"] = battery._sha256_bytes(battery._canonical_json_bytes(verdict))
            return verdict

    result = battery.execute_pass_cases(
        cases,
        Executor(),
        evidence_path=tmp_path / "evidence" / "raw.jsonl",
        runtime_hash="a" * 64,
        require_reconciliation=True,
    )

    assert result["pass_reconciliation_clear"] is False
    assert result["passed"] == 0 and result["failed"] == battery.QUESTIONS_PER_PASS
    assert all("pass_lifecycle_unreconciled" in row["failure_codes"] for row in result["case_results"])
    assert battery._validate_pass_result(result, cases) is True


def test_signed_tail_reconciliation_cannot_claim_clear_with_false_components(
    tmp_path: Path,
) -> None:
    details = {
        "schema": "friday.synthetic-live-battery.tail-reconciliation.v1",
        "probe_exact": False,
        "files_exact": False,
        "database_exact": False,
        "clear": True,
    }
    details["snapshot_sha256"] = battery._sha256_bytes(battery._canonical_json_bytes(details))
    result = {
        "case_results": [{"passed": True, "failure_codes": []}],
        "passed": 1,
        "failed": 0,
        "pass_reconciliation_clear": True,
        "pass_reconciliation_sha256": "b" * 64,
    }

    updated = battery._apply_tail_reconciliation(
        result,
        details,
        evidence_directory=tmp_path,
    )

    assert updated["pass_reconciliation_clear"] is False
    assert updated["passed"] == 0 and updated["failed"] == 1
    assert updated["case_results"][0]["failure_codes"] == ["pass_lifecycle_unreconciled"]


def test_foreign_tenant_digest_covers_direct_and_indirect_owned_rows() -> None:
    storage = sqlite3.connect(":memory:")
    storage.row_factory = sqlite3.Row
    storage.executescript(
        """
        CREATE TABLE users (id TEXT PRIMARY KEY, display_name TEXT NOT NULL);
        CREATE TABLE knowledge_objects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL
        );
        CREATE TABLE custom_presets (
            preset_key TEXT PRIMARY KEY,
            created_by TEXT NOT NULL,
            name TEXT NOT NULL
        );
        CREATE TABLE preset_capabilities (
            preset_key TEXT NOT NULL,
            security_id TEXT NOT NULL,
            PRIMARY KEY(preset_key, security_id)
        );
        CREATE TABLE preset_payloads (
            payload_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL
        );
        INSERT INTO users VALUES('foreign-user', 'Foreign');
        INSERT INTO users VALUES('main-user', 'Main');
        INSERT INTO knowledge_objects VALUES('foreign-ko', 'foreign-user', 'secret-v1');
        INSERT INTO knowledge_objects VALUES('main-ko', 'main-user', 'main-v1');
        INSERT INTO custom_presets VALUES('foreign-preset', 'foreign-user', 'Private');
        INSERT INTO custom_presets VALUES('main-preset', 'main-user', 'Main private');
        INSERT INTO preset_capabilities VALUES('foreign-preset', 'memory.read');
        INSERT INTO preset_capabilities VALUES('main-preset', 'memory.read');
        INSERT INTO preset_payloads VALUES(
            'foreign-payload',
            '  {"preset_key":"foreign-preset","enabled":true}'
        );
        """
    )
    try:
        baseline = battery._tenant_logical_digest(storage, "foreign-user")
        storage.execute("UPDATE knowledge_objects SET content='main-v2' WHERE user_id='main-user'")
        storage.execute("UPDATE custom_presets SET name='main-v2' WHERE preset_key='main-preset'")
        assert battery._tenant_logical_digest(storage, "foreign-user") == baseline

        storage.execute("UPDATE knowledge_objects SET content='destroyed' WHERE user_id='foreign-user'")
        assert battery._tenant_logical_digest(storage, "foreign-user") != baseline

        direct_mutation = battery._tenant_logical_digest(storage, "foreign-user")
        storage.execute(
            "UPDATE preset_capabilities SET security_id='memory.write' WHERE preset_key='foreign-preset'"
        )
        assert battery._tenant_logical_digest(storage, "foreign-user") != direct_mutation

        indirect_mutation = battery._tenant_logical_digest(storage, "foreign-user")
        storage.execute("UPDATE preset_payloads SET payload_json='{}' WHERE payload_key='foreign-payload'")
        assert battery._tenant_logical_digest(storage, "foreign-user") != indirect_mutation
    finally:
        storage.close()


def test_logical_database_digest_includes_sqlite_sequence() -> None:
    storage = sqlite3.connect(":memory:")
    storage.row_factory = sqlite3.Row
    storage.execute("CREATE TABLE sequenced (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL)")
    baseline = battery._logical_database_digest(storage)
    storage.execute("INSERT INTO sequenced(value) VALUES('temporary')")
    storage.commit()
    storage.execute("DELETE FROM sequenced")
    storage.commit()
    try:
        assert storage.execute("SELECT COUNT(*) FROM sequenced").fetchone()[0] == 0
        assert battery._logical_database_digest(storage) != baseline
    finally:
        storage.close()


def test_tail_file_manifest_binds_file_and_empty_directory_names(tmp_path: Path) -> None:
    root = tmp_path / "files"
    empty = root / "empty-a"
    empty.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    empty.chmod(0o700)
    stored = root / "attachment-a.bin"
    stored.write_bytes(b"same private bytes")
    stored.chmod(0o600)

    baseline_manifest = battery._private_file_manifest(root)
    baseline_inventory = battery._private_file_inventory(root)
    stored.rename(root / "attachment-b.bin")
    empty.rename(root / "empty-b")

    assert battery._private_file_inventory(root) == baseline_inventory
    assert battery._private_file_manifest(root) != baseline_manifest


class _SearchHarness:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    async def search(self, _user_id: str, _query: str, **_kwargs: Any) -> dict[str, Any]:
        return self.result


@pytest.mark.asyncio
async def test_retrieval_recursive_ownership_accepts_owned_graph_and_nullable_links() -> None:
    searcher = _SearchHarness(
        {
            "results": [
                {
                    "id": "main-ko",
                    "user_id": "main-user",
                    "raw_object_id": "main-raw",
                    "entity_id": None,
                    "superseded_by_id": None,
                }
            ],
            "count": 1,
            "graph_context": {
                "expanded": True,
                "nodes": [{"id": "main-entity", "merged_into_id": None}],
                "relations": [
                    {
                        "id": "main-relation",
                        "source_entity_id": "main-entity",
                        "target_entity_id": "main-entity-2",
                        "evidence_knowledge_object_id": None,
                    }
                ],
                "paths": [
                    {
                        "path_id": "derived-path-not-a-persisted-id",
                        "root": "main-entity",
                        "target": "main-entity-2",
                        "entity_ids": ["main-entity", "main-entity-2"],
                        "edges": [
                            {
                                "id": "main-relation",
                                "from": "main-entity",
                                "to": "main-entity-2",
                                "source": "main-entity",
                                "target": "main-entity-2",
                                "direction": "forward",
                            }
                        ],
                    }
                ],
            },
        }
    )
    probe = battery.RetrievalPrivacyProbe(searcher, ())
    probe.configure_ownership(
        main_ids=[
            "main-ko",
            "main-raw",
            "main-entity",
            "main-entity-2",
            "main-relation",
        ],
        foreign_ids=["foreign-ko"],
        expected_user="main-user",
    )
    probe.install()
    try:
        await searcher.search("main-user", "synthetic", graph_expansion=True)
    finally:
        probe.restore()

    assert probe.successful_calls == 1
    assert probe.graph_expansion_successes == 1
    assert probe.unowned_id_result_calls == 0
    assert probe.unexpected_user_calls == 0


@pytest.mark.asyncio
async def test_retrieval_ownership_recurses_through_graph_ids_and_user_fields() -> None:
    searcher = _SearchHarness(
        {
            "results": [{"id": "main-ko", "user_id": "foreign-user"}],
            "count": 1,
            "graph_context": {
                "expanded": True,
                "nodes": [{"id": "third-tenant-entity", "user_id": "foreign-user"}],
                "relations": [
                    {
                        "id": "third-tenant-relation",
                        "source_entity_id": "third-tenant-entity",
                        "target_entity_id": "main-entity",
                    }
                ],
            },
        }
    )
    probe = battery.RetrievalPrivacyProbe(searcher, ())
    probe.configure_ownership(
        main_ids=["main-ko", "main-entity"],
        foreign_ids=["foreign-ko"],
        expected_user="main-user",
    )
    probe.install()
    try:
        await searcher.search("main-user", "synthetic", graph_expansion=True)
    finally:
        probe.restore()

    assert probe.successful_calls == 0
    assert probe.graph_expansion_successes == 0
    assert probe.unowned_id_result_calls == 1
    assert probe.unexpected_user_calls == 1


class _RerankHarness:
    async def rerank(self, _query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**item, "_rerank_score": 1.0} for item in items]


@pytest.mark.asyncio
async def test_reranker_ownership_recurses_on_both_boundary_sides() -> None:
    harness = _RerankHarness()
    searcher = type("Searcher", (), {})()
    searcher._reranker = harness.rerank
    probe = battery.RerankerPrivacyProbe(searcher, ())
    probe.configure_ownership(
        main_ids=["main-ko"],
        foreign_ids=["foreign-ko"],
        expected_user="main-user",
    )
    probe.install()
    try:
        await searcher._reranker(
            "synthetic",
            [
                {
                    "id": "main-ko",
                    "user_id": "foreign-user",
                    "metadata": {"entity_id": "third-tenant-entity"},
                }
            ],
        )
    finally:
        probe.restore()

    assert probe.successful_calls == 0
    assert probe.unowned_id_calls == probe.unowned_id_result_calls == 1
    assert probe.unexpected_user_calls == probe.unexpected_user_result_calls == 1
