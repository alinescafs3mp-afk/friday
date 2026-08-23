"""Frozen difficult queries and the product-facing search-explain contract."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.retrieval import HybridSearcher
from friday.retrieval.search_explain import build_search_explain_projection
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "retrieval_difficult_query_v1.json"
FIXTURE_SHA256 = "05d04b4e8f9563ef11cb14d85d5f0c27e2bc7efe1245b2771545a82c07057fbe"
REQUIRED_CLASSES = {
    "approximate_content",
    "approximate_date",
    "old_file",
    "pending_file",
    "unhelpful_filename",
    "typo",
    "person_plus_topic",
    "topic_plus_month",
    "message_history_paraphrase",
    "unknown_corpus",
}
REQUIRED_CASE_FIELDS = {
    "id",
    "class",
    "query",
    "requested_corpora",
    "expected_corpus",
    "expected_object_or_message",
    "expected_passage",
    "expected_date_role",
    "acceptable_alternatives",
    "since",
    "until",
}


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_difficult_query_fixture_is_frozen_synthetic_and_complete() -> None:
    fixture = _fixture()
    cases = fixture["cases"]

    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert fixture["$schema"] == "friday.retrieval_difficult_query.v1"
    assert fixture["schema_version"] == 1 and fixture["synthetic_only"] is True
    assert set(fixture["privacy"].values()) == {False}
    assert len(cases) == 10
    assert Counter(case["class"] for case in cases) == Counter({name: 1 for name in REQUIRED_CLASSES})
    assert all(set(case) == REQUIRED_CASE_FIELDS for case in cases)
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({" ".join(case["query"].casefold().split()) for case in cases}) == len(cases)
    assert all(case["expected_passage"].strip() for case in cases)


def _seed_frozen_corpus(storage) -> None:
    storage.ensure_user("alice", source="test")
    for item in _fixture()["corpus"]:
        raw_id = f"raw_{item['id']}"
        content = item["content"]
        storage.store_raw_object(
            RawObject(
                id=raw_id,
                user_id="alice",
                source="test",
                source_ref=f"fixture:{item['id']}",
                raw_content=content,
                content_type="text/plain",
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                metadata_json=item["metadata"],
                received_at=item["received_at"],
                created_at=item["received_at"],
            )
        )
        storage.store_knowledge_object(
            KnowledgeObject(
                id=item["id"],
                user_id="alice",
                raw_object_id=raw_id,
                title=item["title"],
                summary=content[:160],
                content=content,
                content_type="text/plain",
                metadata_json=item["metadata"],
                created_at=item["received_at"],
                updated_at=item["received_at"],
            )
        )


@pytest.mark.asyncio
async def test_frozen_knowledge_cases_recall_the_declared_object(storage) -> None:
    _seed_frozen_corpus(storage)
    searcher = HybridSearcher(storage, None, record_usage=False)
    cases = [case for case in _fixture()["cases"] if "knowledge" in case["requested_corpora"]]
    recalled = 0

    for case in cases:
        result = await searcher.search(
            "alice",
            case["query"],
            limit=5,
            since=case["since"],
            until=case["until"],
            explain=True,
            record_usage=False,
        )
        ids = {str(item["id"]) for item in result["results"]}
        recalled += case["expected_object_or_message"] in ids
        assert all("recalled_by" in row for row in result.get("trace", []))

    assert recalled / len(cases) >= _fixture()["acceptance"]["knowledge_recall_at_5"]


def test_search_explain_api_is_privacy_safe_and_reports_unavailable_corpora(settings) -> None:
    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        content = "СЕКРЕТНЫЙ-МАРКЕР регламент поверки весов и контрольная гиря М1"
        ingested = client.post(
            "/api/ingest",
            json={"content": content, "force_knowledge": True},
            headers=headers,
        )
        assert ingested.status_code == 200, ingested.text

        response = client.get(
            "/api/admin/retrieval/explain",
            params={
                "q": "СЕКРЕТНЫЙ-МАРКЕР поверка",
                "user_id": LEGACY_OWNER_USER_ID,
                "corpora": "knowledge,documents,messages",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        projection = body["search_explain"]
        serialized = json.dumps(projection, ensure_ascii=False)
        assert projection["$schema"] == "friday.search_explain.v1"
        assert projection["privacy"] == {
            "contains_query": False,
            "contains_object_ids": False,
            "contains_titles_or_passages": False,
        }
        assert "СЕКРЕТНЫЙ-МАРКЕР" not in serialized
        assert body["trace"][0]["id"] not in serialized
        assert {row["name"] for row in projection["corpora"] if row["selected"]} == {
            "knowledge",
            "documents",
            "messages",
        }
        assert projection["completeness"] == {
            "status": "incomplete",
            "reasons": [
                "documents_not_available_in_current_contour",
                "messages_not_available_in_current_contour",
            ],
        }

        unsupported_only = client.get(
            "/api/admin/retrieval/explain",
            params={"q": "anything", "corpora": "messages"},
            headers=headers,
        )
        assert unsupported_only.status_code == 200, unsupported_only.text
        assert unsupported_only.json()["search_explain"]["channels"][0]["status"] == "not_selected"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"selected_corpora": ("secrets",)}, "corpus_unknown"),
        ({"date_role": "invented"}, "date_role_unknown"),
        ({"date_role": "none", "since": "2025-01-01"}, "date_role_required_for_range"),
        ({"date_role": "document_or_mentioned_date", "since": "not-a-date"}, "date_range_invalid"),
        ({"result_limit": 0}, "result_limit_invalid"),
        ({"embedding_index": {"status": "disabled", "diagnostic": "private"}}, "embedding_index_invalid"),
    ],
)
def test_projection_rejects_unowned_scope_and_contract_values(kwargs, reason) -> None:
    arguments = {
        "selected_corpora": ("knowledge",),
        "authorized_knowledge_objects": 0,
        "date_role": "none",
        "since": None,
        "until": None,
        "graph_selected": False,
        "fts_available": True,
        "embedding_index": {"status": "disabled"},
        "result_limit": 5,
        "dense_object_cap": 0,
        **kwargs,
    }
    with pytest.raises(ValueError, match=reason):
        build_search_explain_projection({"count": 0, "trace": [], "strategy": {}}, **arguments)


def test_projection_fails_closed_on_unowned_trace_and_strategy_values() -> None:
    arguments = {
        "selected_corpora": ("knowledge",),
        "authorized_knowledge_objects": 0,
        "date_role": "none",
        "since": None,
        "until": None,
        "graph_selected": False,
        "fts_available": True,
        "embedding_index": {"status": "disabled"},
        "result_limit": 5,
        "dense_object_cap": 0,
    }
    with pytest.raises(ValueError, match="recall_channel_invalid"):
        build_search_explain_projection(
            {
                "count": 0,
                "trace": [{"recalled_by": ["private-channel"]}],
                "strategy": {},
            },
            **arguments,
        )
    with pytest.raises(ValueError, match="strategy_invalid"):
        build_search_explain_projection(
            {"count": 0, "trace": [], "strategy": {"lexical_pool_scanned": "private"}},
            **arguments,
        )


def test_projection_reports_caps_exclusions_and_stale_index_without_payload() -> None:
    projection = build_search_explain_projection(
        {
            "count": 1,
            "matched_at_least": 4,
            "strategy": {"lexical_pool_capped": True, "lexical_pool_scanned": 100},
            "trace": [
                {
                    "id": "ko_private_marker",
                    "title": "PRIVATE TITLE",
                    "status": "discarded",
                    "reason": "insufficient_evidence",
                    "recalled_by": ["fts", "recent_pool"],
                }
            ],
        },
        selected_corpora=("knowledge",),
        authorized_knowledge_objects=14,
        date_role="none",
        since=None,
        until=None,
        graph_selected=False,
        fts_available=True,
        embedding_index={
            "status": "incomplete",
            "missing_objects": 2,
            "stale_objects": 3,
            "freshness": "measured_from_source_version_and_chunk_scheme",
        },
        result_limit=1,
        dense_object_cap=5000,
    )

    assert projection["exclusions"] == {"total": 1, "by_reason": {"insufficient_evidence": 1}}
    assert projection["caps"]["result_page"]["applied"] is True
    assert projection["caps"]["lexical_pool"]["applied"] is True
    assert projection["indexes"]["knowledge_embeddings"]["stale_objects"] == 3
    assert projection["completeness"] == {
        "status": "incomplete",
        "reasons": ["embedding_index_incomplete", "lexical_pool_capped"],
    }
    serialized = json.dumps(projection)
    assert "ko_private_marker" not in serialized and "PRIVATE TITLE" not in serialized


def test_search_explain_api_rejects_unknown_corpus_and_date_role(settings) -> None:
    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        unknown = client.get(
            "/api/admin/retrieval/explain",
            params={"q": "x", "corpora": "secrets"},
            headers=headers,
        )
        bad_date = client.get(
            "/api/admin/retrieval/explain",
            params={"q": "x", "date_role": "received_at"},
            headers=headers,
        )
    assert unknown.status_code == 400 and unknown.json()["detail"] == "corpus_unknown"
    assert bad_date.status_code == 400 and bad_date.json()["detail"] == "date_role_unknown"
