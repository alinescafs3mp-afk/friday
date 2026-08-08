"""Adversarial integrity checks for sealed synthetic live-battery passes."""

from __future__ import annotations

import copy
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


def _cases() -> list[battery.ExpandedCase]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["A"])
    return [case for case in battery.expand_manifest_cases(manifest) if case.pass_index == 1]


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
        INSERT INTO preset_capabilities VALUES('foreign-preset', 'memory.read');
        INSERT INTO preset_payloads VALUES(
            'foreign-payload',
            '  {"preset_key":"foreign-preset","enabled":true}'
        );
        """
    )
    try:
        baseline = battery._tenant_logical_digest(storage, "foreign-user")
        storage.execute("UPDATE knowledge_objects SET content='main-v2' WHERE user_id='main-user'")
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
