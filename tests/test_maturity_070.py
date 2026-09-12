from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image

from friday.config import PROFILES
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.storage import SCHEMA_VERSION
from friday.storage.models import (
    FeedbackItem,
    FeedbackType,
    InboxStatus,
    KnowledgeObject,
    RawObject,
    new_id,
)


def _store_knowledge(
    storage,
    user_id: str,
    content: str,
    *,
    title: str,
    importance: float = 0.2,
    quality: float = 0.25,
    promotion: float = 0.25,
    metadata: dict | None = None,
) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
        importance=importance,
        quality_score=quality,
        promotion_score=promotion,
        metadata_json=metadata or {},
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


@pytest.mark.asyncio
async def test_graph_evolution_is_useful_but_review_only(settings, storage):
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)

    first = await pipeline.ingest_text(
        "alice",
        "Запомни: проект Orion использует PostgreSQL 16.",
        source_ref="relation:orion-postgres",
    )
    assert first["promoted"] is True
    candidates = storage.list_relation_candidates("alice", status="suggested")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["relation_type"] == "uses"
    assert candidate["source_name"] == "Orion"
    assert candidate["target_name"] == "PostgreSQL"
    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM relations WHERE user_id=? AND relation_type='uses'",
            ("alice",),
        ).fetchone()["count"]
        == 0
    )

    reviewed = graph.review_relation_candidate(
        "alice",
        candidate["id"],
        "accepted",
        reviewed_by="alice",
    )
    assert reviewed and reviewed["status"] == "accepted"
    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM relations WHERE user_id=? AND relation_type='uses'",
            ("alice",),
        ).fetchone()["count"]
        == 1
    )

    old = await pipeline.ingest_text(
        "alice",
        "Запомни: сервер Atlas имеет IP 10.0.0.5.",
        source_ref="conflict:atlas:old",
    )
    new = await pipeline.ingest_text(
        "alice",
        "Запомни: сервер Atlas имеет IP 10.0.0.7.",
        source_ref="conflict:atlas:new",
    )
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["conflict_type"] == "address_mismatch"
    assert storage.get_knowledge_object(old["knowledge_object"]["id"], "alice")["deleted_at"] is None
    assert storage.get_knowledge_object(new["knowledge_object"]["id"], "alice")["deleted_at"] is None

    reviewed_conflict = graph.review_conflict(
        "alice",
        conflict["id"],
        "confirmed",
        reviewed_by="alice",
        resolution_note="Needs human verification",
    )
    assert reviewed_conflict and reviewed_conflict["status"] == "confirmed"
    with pytest.raises(ValueError, match="only confirmed conflicts may advance to resolved"):
        graph.review_conflict(
            "alice",
            conflict["id"],
            "dismissed",
            reviewed_by="alice",
        )
    resolved_conflict = graph.review_conflict(
        "alice",
        conflict["id"],
        "resolved",
        reviewed_by="alice",
        resolution_note="The current source was corrected explicitly",
    )
    assert resolved_conflict and resolved_conflict["status"] == "resolved"
    with pytest.raises(ValueError, match="only confirmed conflicts may advance to resolved"):
        graph.review_conflict(
            "alice",
            conflict["id"],
            "confirmed",
            reviewed_by="alice",
        )
    # Reviewing a contradiction records the decision but never rewrites either source claim.
    assert "10.0.0.5" in storage.get_knowledge_object(old["knowledge_object"]["id"], "alice")["content"]
    assert "10.0.0.7" in storage.get_knowledge_object(new["knowledge_object"]["id"], "alice")["content"]


def test_feedback_state_replaces_rating_and_updates_usage_attribution(storage):
    import re

    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app
    from friday.storage.models import AuditEntry

    person_p = "person-feedback-p"
    person_q = "person-feedback-q"
    person_d = "person-feedback-d"
    secret_p = "scoped-p-secret-" + "P" * 32
    secret_q = "scoped-q-secret-" + "Q" * 32
    secret_d = "scoped-d-secret-" + "D" * 32
    spoof_user = "spoof-user-id-canary"
    spoof_mode = "spoof-interaction-mode"
    spoof_cite_label = "spoof-citation-label"
    spoof_ko = "ko_aaaabbbbccccdddd"
    missing_id = "msg_0123456789abcdef"
    fb_id_re = re.compile(r"^fb_[0-9a-f]{16}$")
    ko_id_re = re.compile(r"^ko_[0-9a-f]{16}$")
    msg_id_re = re.compile(r"^msg_[0-9a-f]{16}$")
    settings = storage.settings
    assert person_p != LEGACY_OWNER_USER_ID
    assert person_q != person_p
    assert person_d != person_p
    assert isinstance(settings.api_token, str) and settings.api_token

    def _second_stamp(value, *, audit=False):
        assert isinstance(value, str)
        pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}" + (r"\.000000\+00:00" if audit else r"\+00:00")
        assert re.fullmatch(pattern, value), value
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        assert parsed.microsecond == 0
        return parsed

    def _headers(store, user_id: str, secret: str, *, preset: str = "user") -> dict[str, str]:
        store.ensure_user(user_id, preset_key=preset)
        store.create_api_token(
            user_id,
            hashlib.sha256(secret.encode()).hexdigest(),
            label="feedback-http",
            created_by="test",
        )
        return {"Authorization": f"Bearer {secret}"}

    def _rows(store, table: str) -> list[dict]:
        allowed = {
            "feedback": "feedback",
            "feedback_state": "feedback_state",
            "knowledge_usage": "knowledge_usage",
            "knowledge_objects": "knowledge_objects",
            "raw_objects": "raw_objects",
            "messages": "messages",
            "eval_cases": "eval_cases",
            "api_tokens": "api_tokens",
        }
        found = store.execute(f"SELECT * FROM {allowed[table]} ORDER BY rowid ASC").fetchall()
        return [dict(row) for row in found]

    def _business(store) -> dict:
        tokens = []
        for row in _rows(store, "api_tokens"):
            tokens.append({key: value for key, value in row.items() if key != "last_used_at"})
        return {
            "feedback": _rows(store, "feedback"),
            "feedback_state": _rows(store, "feedback_state"),
            "knowledge_usage": _rows(store, "knowledge_usage"),
            "knowledge_objects": _rows(store, "knowledge_objects"),
            "raw_objects": _rows(store, "raw_objects"),
            "messages": _rows(store, "messages"),
            "eval_cases": _rows(store, "eval_cases"),
            "api_tokens": tokens,
        }

    def _audit(store) -> list[dict]:
        found = store.execute(
            "SELECT rowid, id, user_id, action, target_type, target_id, before_json, "
            "after_json, ip_address, request_id, created_at FROM audit_log ORDER BY rowid ASC"
        ).fetchall()
        return [dict(row) for row in found]

    def _needles(*extra: str) -> tuple[str, ...]:
        return (
            secret_p,
            secret_q,
            secret_d,
            settings.api_token,
            spoof_user,
            spoof_mode,
            spoof_cite_label,
            spoof_ko,
            *extra,
        )

    def _scan(payload, *extra: str) -> None:
        text = None
        decoded = payload
        if (
            not isinstance(payload, (str, dict, list))
            and hasattr(payload, "status_code")
            and hasattr(payload, "text")
        ):
            decoded = payload.json()
            text = payload.text
        blob = decoded if isinstance(decoded, str) else json.dumps(decoded, ensure_ascii=False, default=str)
        for needle in _needles(*extra):
            assert needle not in blob, needle
            if text is not None:
                assert needle not in text, needle

    def _observe(store, call, *, audit_delta=0, business=None):
        before_business = _business(store) if business is None else business
        before_audit = _audit(store)
        response = call()
        after_audit = _audit(store)
        assert after_audit[: len(before_audit)] == before_audit
        delta = after_audit[len(before_audit) :]
        assert len(delta) == audit_delta, [row["action"] for row in delta]
        assert _business(store) == before_business
        return response, before_audit, after_audit, delta

    def _closed_item(item: dict, *, score: float, context_json: str, user_id: str, target_id: str) -> dict:
        assert set(item) == {
            "id",
            "user_id",
            "target_type",
            "target_id",
            "feedback_type",
            "score",
            "comment",
            "context_json",
            "created_at",
        }
        assert fb_id_re.fullmatch(str(item["id"]))
        assert item["user_id"] == user_id
        assert item["target_type"] == "answer"
        assert item["target_id"] == target_id
        assert item["feedback_type"] == FeedbackType.ANSWER_USEFULNESS.value
        assert isinstance(item["score"], (int, float)) and not isinstance(item["score"], bool)
        assert float(item["score"]) == score
        assert item["comment"] == ""
        assert isinstance(item["context_json"], str)
        assert item["context_json"] == context_json
        parsed = _second_stamp(item["created_at"])
        assert parsed.tzinfo is not None and parsed.utcoffset() is not None
        return {
            "id": item["id"],
            "user_id": user_id,
            "target_type": "answer",
            "target_id": target_id,
            "feedback_type": FeedbackType.ANSWER_USEFULNESS.value,
            "score": score,
            "comment": "",
            "context_json": context_json,
            "created_at": item["created_at"],
        }

    with TestClient(create_app(replace(settings, shared_archive=True))) as client:
        store = client.app.state.storage
        headers_p = _headers(store, person_p, secret_p)
        _headers(store, person_q, secret_q)
        headers_d = _headers(store, person_d, secret_d)
        store.set_permission_override(person_d, "feedback.write", "deny")
        store.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
        store.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=LEGACY_OWNER_USER_ID,
                action="inbox.classify",
                target_type="inbox",
                target_id="inbox_prior_nonvacuous",
                after_json={"status": "classified"},
            )
        )

        cited = _store_knowledge(
            store,
            LEGACY_OWNER_USER_ID,
            "Atlas uses PostgreSQL.",
            title="Atlas stack",
        )
        uncited = _store_knowledge(
            store,
            LEGACY_OWNER_USER_ID,
            "Unrelated archive note.",
            title="Uncited note",
        )
        cited_id = str(cited["id"])
        uncited_id = str(uncited["id"])
        assert ko_id_re.fullmatch(cited_id)
        assert ko_id_re.fullmatch(uncited_id)
        assert cited_id != uncited_id

        conversation = store.create_conversation(person_p, "feedback target")
        message = store.store_message(
            conversation["id"],
            person_p,
            "assistant",
            "Atlas answer",
            metadata={
                "knowledge_object_ids": [cited_id],
                "knowledge_citations": {"Atlas stack": cited_id},
                "interaction_mode": "knowledge_work",
            },
        )
        message_id = str(message["id"])
        assert msg_id_re.fullmatch(message_id)
        meta = json.loads(str(message.get("metadata_json") or "{}"))
        assert meta["knowledge_object_ids"] == [cited_id]
        assert str(meta.get("search_query") or "").strip() == ""

        foreign_convo = store.create_conversation(person_q, "foreign feedback")
        foreign_msg = store.store_message(
            foreign_convo["id"],
            person_q,
            "assistant",
            "FOREIGN-CANARY-ANSWER",
        )
        foreign_id = str(foreign_msg["id"])

        expected_context = {
            "channel": "lab-http",
            "interaction_mode": "knowledge_work",
            "knowledge_citations": {"Atlas stack": cited_id},
            "knowledge_object_ids": [cited_id],
        }
        expected_context_json = json.dumps(expected_context, ensure_ascii=False, sort_keys=True)
        payload = {
            "target_type": "answer",
            "target_id": message_id,
            "feedback_type": FeedbackType.ANSWER_USEFULNESS.value,
            "score": 1.0,
            "comment": "",
            "user_id": spoof_user,
            "own_id": spoof_user,
            "actor": {"user_id": spoof_user, "own_id": spoof_user},
            "context": {
                "channel": "lab-http",
                "knowledge_object_ids": [uncited_id, spoof_ko],
                "knowledge_citations": {spoof_cite_label: spoof_ko},
                "interaction_mode": spoof_mode,
            },
        }

        setup_business = _business(store)
        setup_audit = _audit(store)
        assert setup_audit, "prior audit row required"

        foreign, *_ = _observe(
            store,
            lambda: client.post(
                "/api/feedback",
                json={**payload, "target_id": foreign_id},
                headers=headers_p,
            ),
            audit_delta=0,
            business=setup_business,
        )
        assert foreign.status_code == 404, foreign.text
        assert foreign.json() == {"detail": "Assistant answer not found"}
        _scan(foreign)

        missing, *_ = _observe(
            store,
            lambda: client.post(
                "/api/feedback",
                json={**payload, "target_id": missing_id},
                headers=headers_p,
            ),
            audit_delta=0,
            business=setup_business,
        )
        assert missing.status_code == 404, missing.text
        assert missing.json() == {"detail": "Assistant answer not found"}
        _scan(missing)

        denied, *_ = _observe(
            store,
            lambda: client.post("/api/feedback", json=payload, headers=headers_d),
            audit_delta=0,
            business=setup_business,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json() == {"detail": "Access denied for feedback.write (explicit_deny)"}
        _scan(denied)

        bad_type, *_ = _observe(
            store,
            lambda: client.post(
                "/api/feedback",
                json={**payload, "feedback_type": "not-a-type"},
                headers=headers_p,
            ),
            audit_delta=0,
            business=setup_business,
        )
        assert bad_type.status_code == 400, bad_type.text
        assert bad_type.json() == {"detail": "Invalid feedback type"}
        _scan(bad_type)

        bad_score, *_ = _observe(
            store,
            lambda: client.post(
                "/api/feedback",
                json={**payload, "score": 2.0},
                headers=headers_p,
            ),
            audit_delta=0,
            business=setup_business,
        )
        assert bad_score.status_code == 400, bad_score.text
        assert bad_score.json() == {"detail": "score: значение от -1 до 1"}
        _scan(bad_score)

        before_anon_business = _business(store)
        before_anon_audit = _audit(store)
        anon_t0 = datetime.now(UTC)
        anonymous = client.post("/api/feedback", json=payload)
        anon_t1 = datetime.now(UTC)
        after_anon_audit = _audit(store)
        assert anonymous.status_code == 401, anonymous.text
        assert anonymous.json() == {"detail": "Missing authentication"}
        assert _business(store) == before_anon_business
        assert after_anon_audit[: len(before_anon_audit)] == before_anon_audit
        anon_delta = after_anon_audit[len(before_anon_audit) :]
        assert len(anon_delta) == 1
        anon_row = anon_delta[0]
        assert re.fullmatch(r"audit_[0-9a-f]{16}", anon_row["id"])
        assert anon_row["id"] not in {row["id"] for row in before_anon_audit}
        anon_request_id = anonymous.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{24}", anon_request_id)
        assert anon_request_id not in {row["request_id"] for row in before_anon_audit}
        anon_created = _second_stamp(anon_row["created_at"], audit=True)
        assert anon_t0.replace(microsecond=0) <= anon_created <= anon_t1
        assert anon_row == {
            "rowid": before_anon_audit[-1]["rowid"] + 1,
            "id": anon_row["id"],
            "user_id": "anonymous",
            "action": "auth.failed",
            "target_type": "auth",
            "target_id": "invalid_credentials",
            "before_json": None,
            "after_json": json.dumps(
                {
                    "method_chars": 4,
                    "path_chars": 13,
                    "reason": "invalid_credentials",
                    "status_present": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "ip_address": "",
            "request_id": anon_request_id,
            "created_at": anon_row["created_at"],
        }
        assert anon_delta[0]["action"] == "auth.failed"
        assert anon_delta[0]["user_id"] == "anonymous"
        assert anon_delta[0]["target_type"] == "auth"
        _scan(anonymous)

        def _in_window(value, t0, t1) -> str:
            parsed = _second_stamp(value)
            assert parsed.tzinfo is not None and parsed.utcoffset() is not None
            # Product utc_now() is timespec=seconds; floor t0 so truncated
            # created_at still sits in the actual before/after POST UTC window.
            assert t0.replace(microsecond=0) <= parsed <= t1, (value, t0, t1)
            return str(value)

        def _expected_feedback(*, item_id: str, score: float, created_at: str) -> dict:
            return {
                "id": item_id,
                "user_id": person_p,
                "target_type": "answer",
                "target_id": message_id,
                "feedback_type": FeedbackType.ANSWER_USEFULNESS.value,
                "score": score,
                "comment": "",
                "context_json": expected_context_json,
                "created_at": created_at,
            }

        def _expected_state(*, feedback_id: str, score: float, updated_at: str) -> dict:
            return {
                "user_id": person_p,
                "target_type": "answer",
                "target_id": message_id,
                "feedback_type": FeedbackType.ANSWER_USEFULNESS.value,
                "score": score,
                "comment": "",
                "context_json": expected_context_json,
                "feedback_id": feedback_id,
                "updated_at": updated_at,
            }

        def _expected_usage(*, score: float, stamped_at: str) -> dict:
            return {
                "user_id": LEGACY_OWNER_USER_ID,
                "knowledge_object_id": cited_id,
                "retrieval_count": 0,
                "answer_count": 0,
                "positive_feedback_count": 1 if score > 0 else 0,
                "negative_feedback_count": 1 if score < 0 else 0,
                "last_retrieved_at": None,
                "last_used_at": None,
                "last_feedback_at": stamped_at,
                "updated_at": stamped_at,
            }

        before_plus_audit = _audit(store)
        plus_t0 = datetime.now(UTC)
        plus = client.post("/api/feedback", json=payload, headers=headers_p)
        plus_t1 = datetime.now(UTC)
        assert plus.status_code == 200, plus.text
        plus_item = plus.json()["feedback"]
        closed_plus = _closed_item(
            plus_item,
            score=1.0,
            context_json=expected_context_json,
            user_id=person_p,
            target_id=message_id,
        )
        _in_window(closed_plus["created_at"], plus_t0, plus_t1)
        assert plus.json() == {"feedback": closed_plus}
        assert plus_item["user_id"] == person_p
        assert plus_item["user_id"] != LEGACY_OWNER_USER_ID
        _scan(plus, uncited_id)
        assert _audit(store) == before_plus_audit
        expected_plus_feedback = _expected_feedback(
            item_id=closed_plus["id"],
            score=1.0,
            created_at=closed_plus["created_at"],
        )
        plus_feedback_rows = _rows(store, "feedback")
        assert plus_feedback_rows[: len(setup_business["feedback"])] == setup_business["feedback"]
        assert plus_feedback_rows == [*setup_business["feedback"], expected_plus_feedback]
        expected_plus_state = _expected_state(
            feedback_id=closed_plus["id"],
            score=1.0,
            updated_at=closed_plus["created_at"],
        )
        plus_state_rows = _rows(store, "feedback_state")
        assert plus_state_rows == [*setup_business["feedback_state"], expected_plus_state]
        plus_state = store.get_feedback_state(
            person_p,
            target_type="answer",
            target_id=message_id,
            feedback_type=FeedbackType.ANSWER_USEFULNESS.value,
        )
        assert plus_state == [expected_plus_state]
        expected_plus_usage = _expected_usage(score=1.0, stamped_at=closed_plus["created_at"])
        plus_usage_rows = _rows(store, "knowledge_usage")
        assert plus_usage_rows == [*setup_business["knowledge_usage"], expected_plus_usage]
        assert uncited_id not in {row["knowledge_object_id"] for row in plus_usage_rows}
        assert _rows(store, "messages") == setup_business["messages"]
        assert _rows(store, "eval_cases") == setup_business["eval_cases"]
        assert _rows(store, "knowledge_objects") == setup_business["knowledge_objects"]
        assert _rows(store, "raw_objects") == setup_business["raw_objects"]
        assert store.get_feedback_for_target(person_q, "answer", foreign_id) == []
        assert [row for row in plus_feedback_rows if row["user_id"] == person_q] == []
        assert [row for row in plus_feedback_rows if row["user_id"] == LEGACY_OWNER_USER_ID] == []
        assert [row for row in plus_state_rows if row["user_id"] != person_p] == [
            row for row in setup_business["feedback_state"] if row["user_id"] != person_p
        ]

        minus_payload = dict(payload)
        minus_payload["score"] = -1.0
        before_minus_audit = _audit(store)
        minus_t0 = datetime.now(UTC)
        minus = client.post("/api/feedback", json=minus_payload, headers=headers_p)
        minus_t1 = datetime.now(UTC)
        assert minus.status_code == 200, minus.text
        minus_item = minus.json()["feedback"]
        closed_minus = _closed_item(
            minus_item,
            score=-1.0,
            context_json=expected_context_json,
            user_id=person_p,
            target_id=message_id,
        )
        _in_window(closed_minus["created_at"], minus_t0, minus_t1)
        assert minus.json() == {"feedback": closed_minus}
        assert closed_minus["id"] != closed_plus["id"]
        _scan(minus, uncited_id)
        assert _audit(store) == before_minus_audit
        expected_minus_feedback = _expected_feedback(
            item_id=closed_minus["id"],
            score=-1.0,
            created_at=closed_minus["created_at"],
        )
        minus_feedback_rows = _rows(store, "feedback")
        assert minus_feedback_rows[: len(plus_feedback_rows)] == plus_feedback_rows
        assert minus_feedback_rows == [
            *setup_business["feedback"],
            expected_plus_feedback,
            expected_minus_feedback,
        ]
        expected_minus_state = _expected_state(
            feedback_id=closed_minus["id"],
            score=-1.0,
            updated_at=closed_minus["created_at"],
        )
        minus_state_rows = _rows(store, "feedback_state")
        assert minus_state_rows == [*setup_business["feedback_state"], expected_minus_state]
        state = store.get_feedback_state(
            person_p,
            target_type="answer",
            target_id=message_id,
            feedback_type=FeedbackType.ANSWER_USEFULNESS.value,
        )
        assert state == [expected_minus_state]
        expected_minus_usage = _expected_usage(score=-1.0, stamped_at=closed_minus["created_at"])
        minus_usage_rows = _rows(store, "knowledge_usage")
        assert minus_usage_rows == [*setup_business["knowledge_usage"], expected_minus_usage]
        assert uncited_id not in {row["knowledge_object_id"] for row in minus_usage_rows}
        assert _rows(store, "messages") == setup_business["messages"]
        assert _rows(store, "eval_cases") == setup_business["eval_cases"]
        assert _rows(store, "knowledge_objects") == setup_business["knowledge_objects"]
        assert _rows(store, "raw_objects") == setup_business["raw_objects"]
        q_messages = [row for row in _rows(store, "messages") if row["user_id"] == person_q]
        assert len(q_messages) == 1
        assert q_messages[0]["id"] == foreign_id
        assert q_messages[0]["content"] == "FOREIGN-CANARY-ANSWER"
        assert store.get_feedback_for_target(person_q, "answer", foreign_id) == []
        assert [row for row in minus_feedback_rows if row["user_id"] == person_q] == []
        archive_feedback = [row for row in minus_feedback_rows if row["user_id"] == LEGACY_OWNER_USER_ID]
        assert archive_feedback == []
        p_messages = [row for row in _rows(store, "messages") if row["user_id"] == person_p]
        setup_p_messages = [row for row in setup_business["messages"] if row["user_id"] == person_p]
        assert p_messages == setup_p_messages


@pytest.mark.asyncio
async def test_repeated_rejections_only_downgrade_future_automatic_promotion(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    sample = "Сервер Atlas работает на Ubuntu 24.04."
    baseline = pipeline.assess_text(sample)
    assert baseline.action == "promote"
    for index in range(3):
        raw_id = f"raw-rejected-{index}"
        storage.store_raw_object(
            RawObject(
                id=raw_id,
                user_id="alice",
                source="test",
                source_ref=f"feedback-calibration-{index}",
                raw_content=f"rejected calibration sample {index}",
                content_type="text",
            )
        )
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id="alice",
                target_type="classification",
                target_id=raw_id,
                feedback_type=FeedbackType.CLASSIFICATION,
                score=-1.0,
                context_json={
                    "knowledge_kind": baseline.knowledge_kind,
                    "signals": baseline.signals,
                    "status": "ignored",
                },
            )
        )

    calibrated = await pipeline.ingest_text(
        "alice",
        "Сервер Borealis работает на Ubuntu 24.04.",
        source_ref="calibration:auto",
    )
    assert calibrated["action"] == "review"
    assert calibrated["queued_for_review"] is True
    assert (
        "feedback_calibration_review"
        in (storage.get_raw_object(calibrated["raw_object_id"], "alice")["metadata_json"])
    )

    explicit = await pipeline.ingest_text(
        "alice",
        "Запомни: сервер Borealis работает на Ubuntu 24.04.",
        source_ref="calibration:explicit",
    )
    assert explicit["action"] == "promote"
    no_save = await pipeline.ingest_text(
        "alice",
        "Не запоминай: сервер Borealis работает на Ubuntu 24.04.",
        source_ref="calibration:no-save",
        force_knowledge=True,
    )
    assert no_save["action"] == "transient"
    assert no_save["raw_object_id"] is None


@pytest.mark.asyncio
async def test_research_synthesis_crosses_only_the_inbox_boundary(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.queue_research_candidate(
        "alice",
        "Исследование: PostgreSQL 16 улучшает logical replication; перед применением сверить источники.",
        source_ref="research-answer:msg-1",
        metadata={"assistant_message_id": "msg-1"},
    )
    assert result["promoted"] is False
    assert result["queued_for_review"] is True
    inbox = storage.get_inbox_item(result["inbox_id"], "alice")
    assert inbox and inbox["status"] == InboxStatus.PENDING.value
    assert storage.get_knowledge_by_raw(result["raw_object_id"], "alice") is None

    replay = await pipeline.queue_research_candidate(
        "alice",
        "Исследование: PostgreSQL 16 улучшает logical replication; перед применением сверить источники.",
        source_ref="research-answer:msg-1",
    )
    assert replay["idempotent_replay"] is True
    assert replay["inbox_id"] == result["inbox_id"]


def test_conversation_modes_and_lifecycle_candidates_are_persistent_and_safe(storage):
    # Растёт вместе со схемой — это ЗАМОК, а не справка: он заставляет
    # каждого, кто меняет схему, назвать номер вслух. Забытый номер стоил
    # 2026-08-04 пятиминутной поломки живого маршрута: столбец добавили, а
    # миграция без нового номера не запускается.
    assert SCHEMA_VERSION == 50
    conversation = storage.create_conversation("alice", "Research", mode="research")
    assert conversation["mode"] == "research"
    storage.set_channel_conversation(
        "alice",
        "telegram",
        "42",
        conversation["id"],
        mode="research",
    )
    assert storage.get_channel_session("alice", "telegram", "42")["mode"] == "research"
    assert storage.set_channel_mode("alice", "telegram", "42", "knowledge_work")["mode"] == "knowledge_work"

    stale = _store_knowledge(storage, "alice", "Old low-value note", title="Old note")
    reviewed = _store_knowledge(
        storage,
        "alice",
        "Manually reviewed old note",
        title="Reviewed note",
        metadata={"promotion_assessment": {"reason": "human review"}},
    )
    old_timestamp = (datetime.now(UTC) - timedelta(days=400)).isoformat(timespec="seconds")
    storage.execute(
        "UPDATE knowledge_objects SET updated_at=? WHERE id IN (?, ?)",
        (old_timestamp, stale["id"], reviewed["id"]),
    )
    candidates = storage.list_lifecycle_candidates("alice", days_threshold=90)
    assert [item["knowledge_object"]["id"] for item in candidates] == [stale["id"]]
    assert storage.get_knowledge_object(stale["id"], "alice")["lifecycle_stage"] == "active"

    storage.record_knowledge_usage("alice", [stale["id"]], used_in_answer=True)
    assert storage.list_lifecycle_candidates("alice", days_threshold=90) == []


@pytest.mark.asyncio
async def test_bounded_local_vision_creates_advisory_inbox_item(settings, storage):
    class FakeVisionLLM:
        enabled = True
        model = "fake-qwen-vision"

        async def chat(self, messages, **kwargs):
            assert kwargs["temperature"] == 0.0
            assert kwargs["priority"] == "foreground"
            user_content = messages[-1]["content"]
            assert isinstance(user_content, list)
            assert any(item.get("type") == "image_url" for item in user_content)
            assert any("ASSET A1" in str(item.get("text") or "") for item in user_content)
            return {
                "content": json.dumps(
                    {
                        "text": "",
                        "pages": [
                            {
                                "asset_id": "A1",
                                "text": "Схема проекта Orion: PostgreSQL 16",
                            }
                        ],
                        "title": "Схема Orion",
                        "summary": "На изображении указана база PostgreSQL 16 проекта Orion.",
                        "entities": [
                            {
                                "name": "Orion",
                                "entity_type": "project",
                                "confidence": 0.98,
                                "asset_id": "A1",
                                "evidence": "проекта Orion",
                            },
                            {
                                "name": "PostgreSQL",
                                "entity_type": "concept",
                                "confidence": 0.97,
                                "asset_id": "A1",
                                "evidence": "PostgreSQL 16",
                            },
                        ],
                        "evidence": [
                            {
                                "asset_id": "A1",
                                "quote": "проекта Orion: PostgreSQL 16",
                                "claim": "Orion использует PostgreSQL 16",
                            }
                        ],
                        "warnings": [],
                        "confidence": 0.87,
                    },
                    ensure_ascii=False,
                )
            }

    image = Image.new("RGB", (640, 360), "white")
    data = BytesIO()
    image.save(data, format="PNG")
    pipeline = IngestionPipeline(
        replace(settings, profile=PROFILES["qwen36-vl"]),
        storage,
        KnowledgeGraph(storage),
        FakeVisionLLM(),
    )
    result = await pipeline.ingest_file(
        "alice",
        None,
        data.getvalue(),
        filename="orion-scan.png",
        mime_type="image/png",
        source_ref="vision:orion",
    )
    # Review-gated invariant (§24): vision output is model-generated, so no
    # Knowledge Object exists until a human confirms the pending inbox item.
    assert result["promoted"] is False
    assert result["queued_for_review"] is True
    assert result["knowledge_object"] is None
    assert storage.get_knowledge_by_raw(result["raw_object_id"], "alice") is None
    assert result["extraction"]["vision"]["confidence"] == 0.87
    assert result["extraction"]["vision"]["grounded_evidence_count"] == 1
    assert result["extraction"]["vision"]["asset_coverage"] == 1.0
    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    assert inbox and inbox["status"] == "pending"
    assert inbox["knowledge_object_id"] is None
    # Uncertain visual entities remain Inbox suggestions instead of polluting
    # the graph with new nodes; the caps survive into deferred promotion.
    suggestions = json.loads(inbox["suggestions_json"])
    assert {item["name"] for item in suggestions["entities"]} >= {"Orion", "PostgreSQL"}
    assert all(float(item["confidence"]) <= 0.79 for item in suggestions["entities"])


@pytest.mark.asyncio
async def test_vision_exception_text_is_not_persisted_or_returned(settings, storage, monkeypatch, tmp_path):
    from tests.test_local_ocr_fallback import _fake_tesseract

    # This oracle requires the enabled OCR branch. Bind its provider explicitly;
    # a developer host need not have the optional native Tesseract installed.
    for name in (
        "FRIDAY_TESSDATA_DIR",
        "JERICHO_TESSDATA_DIR",
        "FRIDAY_TESSERACT_LIBRARY_PATH",
        "JERICHO_TESSERACT_LIBRARY_PATH",
        "TESSDATA_PREFIX",
        "LD_LIBRARY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    executable = _fake_tesseract(tmp_path / "synthetic-blank-ocr", ocr_action="pass")
    monkeypatch.setenv("FRIDAY_TESSERACT_PATH", str(executable))
    monkeypatch.setenv("FRIDAY_TESSERACT_LANGUAGES", "rus+eng")
    private = "VISION-EXCEPTION-SENTINEL-51ec2a"

    class FailingVisionLLM:
        enabled = True
        model = "synthetic-vision"

        async def chat(self, _messages, **_kwargs):
            raise RuntimeError(f"private model detail {private}")

    image = Image.new("RGB", (64, 64), "white")
    data = BytesIO()
    image.save(data, format="PNG")
    pipeline = IngestionPipeline(
        replace(settings, profile=PROFILES["qwen36-vl"]),
        storage,
        KnowledgeGraph(storage),
        FailingVisionLLM(),
    )
    result = await pipeline.ingest_file(
        "alice",
        None,
        data.getvalue(),
        filename="synthetic-private-error.png",
        mime_type="image/png",
        source_ref="vision:synthetic-private-error",
    )

    encoded_result = json.dumps(result, ensure_ascii=False)
    assert private not in encoded_result
    vision = result["extraction"]["vision"]
    # Model vision failure is followed by bounded local OCR. An empty OCR page
    # is the final physical outcome; private exception text remains absent.
    assert vision["error"] == "local_ocr_page_text_empty"
    raw = storage.get_raw_object(result["raw_object_id"], "alice")
    assert private not in json.dumps(raw, ensure_ascii=False)


def test_current_feedback_stats_replace_superseded_signal(storage):
    import re

    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app
    from friday.storage.models import AuditEntry

    person_p = "person-feedback-p"
    person_q = "person-feedback-q"
    person_a = "person-feedback-a"
    secret_p = "scoped-p-secret-" + "P" * 32
    secret_q = "scoped-q-secret-" + "Q" * 32
    secret_a = "scoped-a-secret-" + "A" * 32
    foreign_comment = "FOREIGN-CANARY-COMMENT"
    settings = storage.settings
    fb_id_re = re.compile(r"^fb_[0-9a-f]{16}$")
    assert person_p != LEGACY_OWNER_USER_ID
    assert person_a != person_p
    assert person_q != person_p
    assert isinstance(settings.api_token, str) and settings.api_token

    def _second_stamp(value, *, audit=False):
        assert isinstance(value, str)
        pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}" + (r"\.000000\+00:00" if audit else r"\+00:00")
        assert re.fullmatch(pattern, value), value
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        assert parsed.microsecond == 0
        return parsed

    def _headers(store, user_id: str, secret: str, *, preset: str = "user") -> dict[str, str]:
        store.ensure_user(user_id, preset_key=preset)
        store.create_api_token(
            user_id,
            hashlib.sha256(secret.encode()).hexdigest(),
            label="feedback-admin-http",
            created_by="test",
        )
        return {"Authorization": f"Bearer {secret}"}

    def _rows(store, table: str) -> list[dict]:
        allowed = {
            "feedback": "feedback",
            "feedback_state": "feedback_state",
            "knowledge_usage": "knowledge_usage",
            "messages": "messages",
            "eval_cases": "eval_cases",
            "api_tokens": "api_tokens",
        }
        found = store.execute(f"SELECT * FROM {allowed[table]} ORDER BY rowid ASC").fetchall()
        return [dict(row) for row in found]

    def _business(store) -> dict:
        tokens = []
        for row in _rows(store, "api_tokens"):
            tokens.append({key: value for key, value in row.items() if key != "last_used_at"})
        return {
            "feedback": _rows(store, "feedback"),
            "feedback_state": _rows(store, "feedback_state"),
            "knowledge_usage": _rows(store, "knowledge_usage"),
            "messages": _rows(store, "messages"),
            "eval_cases": _rows(store, "eval_cases"),
            "api_tokens": tokens,
        }

    def _audit(store) -> list[dict]:
        found = store.execute(
            "SELECT rowid, id, user_id, action, target_type, target_id, before_json, "
            "after_json, ip_address, request_id, created_at FROM audit_log ORDER BY rowid ASC"
        ).fetchall()
        return [dict(row) for row in found]

    def _needles(*extra: str) -> tuple[str, ...]:
        return (
            secret_p,
            secret_q,
            secret_a,
            settings.api_token,
            foreign_comment,
            *extra,
        )

    def _scan(payload, *extra: str) -> None:
        text = None
        decoded = payload
        if (
            not isinstance(payload, (str, dict, list))
            and hasattr(payload, "status_code")
            and hasattr(payload, "text")
        ):
            decoded = payload.json()
            text = payload.text
        blob = decoded if isinstance(decoded, str) else json.dumps(decoded, ensure_ascii=False, default=str)
        for needle in _needles(*extra):
            assert needle not in blob, needle
            if text is not None:
                assert needle not in text, needle

    def _observe(store, call, *, audit_delta=0, business=None):
        before_business = _business(store) if business is None else business
        before_audit = _audit(store)
        response = call()
        after_audit = _audit(store)
        assert after_audit[: len(before_audit)] == before_audit
        delta = after_audit[len(before_audit) :]
        assert len(delta) == audit_delta, [row["action"] for row in delta]
        assert _business(store) == before_business
        return response, before_audit, after_audit, delta

    def _state_row(row: dict) -> dict:
        closed = {
            "user_id": row["user_id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "feedback_type": row["feedback_type"],
            "score": float(row["score"]),
            "comment": row["comment"],
            "context_json": row["context_json"],
            "feedback_id": row["feedback_id"],
            "updated_at": row["updated_at"],
        }
        assert len(closed) == 9
        assert set(closed) == {
            "user_id",
            "target_type",
            "target_id",
            "feedback_type",
            "score",
            "comment",
            "context_json",
            "feedback_id",
            "updated_at",
        }
        return closed

    with TestClient(create_app(replace(settings, shared_archive=True))) as client:
        store = client.app.state.storage
        headers_p = _headers(store, person_p, secret_p)
        _headers(store, person_q, secret_q)
        headers_a = _headers(store, person_a, secret_a, preset="admin")
        store.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
        store.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=LEGACY_OWNER_USER_ID,
                action="inbox.classify",
                target_type="inbox",
                target_id="inbox_prior_nonvacuous",
                after_json={"status": "classified"},
            )
        )

        conversation = store.create_conversation(person_p, "feedback stats target")
        message = store.store_message(conversation["id"], person_p, "assistant", "answer")
        message_id = str(message["id"])
        first_id = new_id("fb")
        second_id = new_id("fb")
        first = store.store_feedback(
            FeedbackItem(
                id=first_id,
                user_id=person_p,
                target_type="answer",
                target_id=message_id,
                feedback_type=FeedbackType.ANSWER_USEFULNESS,
                score=1.0,
            )
        )
        second = store.store_feedback(
            FeedbackItem(
                id=second_id,
                user_id=person_p,
                target_type="answer",
                target_id=message_id,
                feedback_type=FeedbackType.ANSWER_USEFULNESS,
                score=-1.0,
            )
        )
        foreign_convo = store.create_conversation(person_q, "foreign stats")
        foreign_msg = store.store_message(
            foreign_convo["id"],
            person_q,
            "assistant",
            "FOREIGN-CANARY-ANSWER",
        )
        store.store_feedback(
            FeedbackItem(
                id=new_id("fb"),
                user_id=person_q,
                target_type="answer",
                target_id=str(foreign_msg["id"]),
                feedback_type=FeedbackType.ANSWER_USEFULNESS,
                score=1.0,
                comment=foreign_comment,
            )
        )
        assert fb_id_re.fullmatch(first.id)
        assert fb_id_re.fullmatch(second.id)
        _second_stamp(first.created_at)
        _second_stamp(second.created_at)
        seeded_state = [row for row in _rows(store, "feedback_state") if row["user_id"] == person_p]
        assert len(seeded_state) == 1
        expected_current = [
            {
                "user_id": person_p,
                "target_type": "answer",
                "target_id": message_id,
                "feedback_type": "answer_usefulness",
                "score": -1.0,
                "comment": "",
                "context_json": "{}",
                "feedback_id": second_id,
                "updated_at": second.created_at,
            }
        ]
        assert seeded_state == expected_current
        assert [_state_row(seeded_state[0])] == expected_current
        assert expected_current[0]["score"] == -1.0
        assert expected_current[0]["feedback_id"] == second.id
        assert expected_current[0]["updated_at"] == second.created_at
        assert expected_current[0]["user_id"] == person_p
        history = store.get_feedback_stats(person_p)
        assert history == {"answer_usefulness": {"avg_score": 0.0, "count": 2}}
        expected_body = {
            "user_id": person_p,
            "stats": {"answer_usefulness": {"avg_score": 0.0, "count": 2}},
            "current": expected_current,
        }

        setup_business = _business(store)
        setup_audit = _audit(store)
        assert setup_audit, "prior audit row required"

        same, *_ = _observe(
            store,
            lambda: client.get(
                "/api/admin/feedback",
                params={"user_id": person_a},
                headers=headers_a,
            ),
            audit_delta=0,
            business=setup_business,
        )
        assert same.status_code == 200, same.text
        assert same.json() == {"user_id": person_a, "stats": {}, "current": []}
        _scan(same)

        t0 = datetime.now(UTC)
        listed, _, _, listed_delta = _observe(
            store,
            lambda: client.get(
                "/api/admin/feedback",
                params={"user_id": person_p},
                headers=headers_a,
            ),
            audit_delta=1,
            business=setup_business,
        )
        t1 = datetime.now(UTC)
        assert listed.status_code == 200, listed.text
        assert listed.json() == expected_body
        assert listed.json()["current"][0]["feedback_id"] == second.id
        assert listed.json()["current"][0]["updated_at"] == second.created_at
        assert person_q not in listed.text
        assert str(foreign_msg["id"]) not in listed.text
        _scan(listed)
        assert len(listed_delta) == 1
        audit_row = listed_delta[0]
        assert audit_row["user_id"] == person_a
        assert audit_row["action"] == "admin.feedback.read"
        assert audit_row["target_type"] == "user"
        assert audit_row["target_id"] == person_p
        assert audit_row["before_json"] is None
        assert audit_row["after_json"] is None
        assert audit_row["ip_address"] == ""
        assert audit_row["request_id"] == listed.headers["x-request-id"]
        created = _second_stamp(audit_row["created_at"], audit=True)
        assert t0.replace(microsecond=0) <= created <= t1
        for column in (
            "id",
            "user_id",
            "action",
            "target_type",
            "target_id",
            "before_json",
            "after_json",
            "ip_address",
            "request_id",
            "created_at",
        ):
            assert column in audit_row

        denied, *_ = _observe(
            store,
            lambda: client.get(
                "/api/admin/feedback",
                params={"user_id": person_p},
                headers=headers_p,
            ),
            audit_delta=0,
            business=setup_business,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json() == {"detail": "Access denied for admin.all_data.read (default_deny)"}
        _scan(denied)

        before_anon_business = _business(store)
        before_anon_audit = _audit(store)
        anon_t0 = datetime.now(UTC)
        anonymous = client.get("/api/admin/feedback", params={"user_id": person_p})
        anon_t1 = datetime.now(UTC)
        after_anon_audit = _audit(store)
        assert anonymous.status_code == 401, anonymous.text
        assert anonymous.json() == {"detail": "Missing authentication"}
        assert _business(store) == before_anon_business
        assert after_anon_audit[: len(before_anon_audit)] == before_anon_audit
        anon_delta = after_anon_audit[len(before_anon_audit) :]
        assert len(anon_delta) == 1
        anon_row = anon_delta[0]
        assert re.fullmatch(r"audit_[0-9a-f]{16}", anon_row["id"])
        assert anon_row["id"] not in {row["id"] for row in before_anon_audit}
        anon_request_id = anonymous.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{24}", anon_request_id)
        assert anon_request_id not in {row["request_id"] for row in before_anon_audit}
        anon_created = _second_stamp(anon_row["created_at"], audit=True)
        assert anon_t0.replace(microsecond=0) <= anon_created <= anon_t1
        assert anon_row == {
            "rowid": before_anon_audit[-1]["rowid"] + 1,
            "id": anon_row["id"],
            "user_id": "anonymous",
            "action": "auth.failed",
            "target_type": "auth",
            "target_id": "invalid_credentials",
            "before_json": None,
            "after_json": json.dumps(
                {
                    "method_chars": 3,
                    "path_chars": 19,
                    "reason": "invalid_credentials",
                    "status_present": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "ip_address": "",
            "request_id": anon_request_id,
            "created_at": anon_row["created_at"],
        }
        assert anon_delta[0]["action"] == "auth.failed"
        assert anon_delta[0]["user_id"] == "anonymous"
        _scan(anonymous)
        assert store.get_feedback_for_target(person_q, "answer", str(foreign_msg["id"]))
        assert len(store.get_feedback_for_target(person_p, "answer", message_id)) == 2
        assert first.created_at
        assert history["answer_usefulness"]["count"] == 2


@pytest.mark.asyncio
async def test_agent_feedback_attribution_uses_only_cited_knowledge(settings, storage):
    from friday.agent_runtime import AgentRuntime
    from friday.permissions import ActorContext

    first = _store_knowledge(storage, "alice", "Atlas uses Redis.", title="Atlas cache")
    second = _store_knowledge(storage, "alice", "Atlas uses PostgreSQL 16.", title="Atlas database")

    class FakeSearcher:
        async def search(self, user_id, query, **kwargs):
            assert user_id == "alice"
            assert query
            assert kwargs["limit"] == 16
            return {
                "results": [
                    {**first, "_score": 0.91, "_entities": []},
                    {**second, "_score": 0.88, "_entities": []},
                ],
                "entity_matches": [],
            }

    class CitationLLM:
        enabled = True
        model = "citation-test"

        async def chat(self, messages, **kwargs):
            del kwargs
            context_message = next(
                item["content"]
                for item in messages
                if item.get("role") == "user"
                and str(item.get("content") or "").startswith("FRIDAY_CONTEXT_DATA")
            )
            assert '"citation": "K1"' in context_message
            assert '"citation": "K2"' in context_message
            return {"content": "Для рабочей базы Atlas используется PostgreSQL 16 [K2]."}

    runtime = AgentRuntime(settings, storage, llm=CitationLLM())
    response = await runtime.chat(
        "alice",
        "Подготовь структурированную справку по базе Atlas",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
        hybrid_searcher=FakeSearcher(),
        mode="knowledge_work",
    )

    message = storage.get_message(response["message_id"], "alice")
    assert message is not None
    metadata = json.loads(message["metadata_json"])
    assert metadata["knowledge_object_ids"] == [second["id"]]
    assert metadata["knowledge_citations"] == {"K2": second["id"]}
    usage = storage.get_knowledge_usage("alice", [first["id"], second["id"]])
    assert first["id"] not in usage
    assert usage[second["id"]]["answer_count"] == 1

    await runtime.record_feedback(
        "alice",
        "answer",
        response["message_id"],
        FeedbackType.ANSWER_USEFULNESS,
        -1.0,
        context={
            "channel": "api",
            "knowledge_object_ids": [first["id"]],
            "interaction_mode": "research",
        },
    )
    state = storage.get_feedback_state(
        "alice",
        target_type="answer",
        target_id=response["message_id"],
        feedback_type=FeedbackType.ANSWER_USEFULNESS.value,
    )[0]
    feedback_context = json.loads(state["context_json"])
    assert feedback_context["knowledge_object_ids"] == [second["id"]]
    assert feedback_context["knowledge_citations"] == {"K2": second["id"]}
    assert feedback_context["interaction_mode"] == "knowledge_work"
    usage = storage.get_knowledge_usage("alice", [first["id"], second["id"]])
    assert first["id"] not in usage
    assert usage[second["id"]]["negative_feedback_count"] == 1

    with pytest.raises(LookupError, match="Assistant answer not found"):
        await runtime.record_feedback(
            "alice",
            "answer",
            "missing-message",
            FeedbackType.ANSWER_USEFULNESS,
            1.0,
        )


@pytest.mark.asyncio
async def test_ungrounded_vision_confidence_is_capped_and_remains_advisory(settings, storage):
    class UngroundedVisionLLM:
        enabled = True
        model = "ungrounded-vision-test"

        async def chat(self, messages, **kwargs):
            del messages, kwargs
            return {
                "content": json.dumps(
                    {
                        "text": "Проект Aurora использует неизвестную БД.",
                        "title": "Скан Aurora",
                        "summary": "Якобы распознана архитектурная схема.",
                        "document_type": "diagram",
                        "entities": [
                            {
                                "name": "Aurora",
                                "entity_type": "project",
                                "confidence": 0.99,
                            }
                        ],
                        "evidence": [],
                        "warnings": [],
                        "confidence": 0.99,
                    },
                    ensure_ascii=False,
                )
            }

    image = Image.new("RGB", (640, 360), "white")
    data = BytesIO()
    image.save(data, format="PNG")
    pipeline = IngestionPipeline(
        replace(settings, profile=PROFILES["qwen36-vl"]),
        storage,
        KnowledgeGraph(storage),
        UngroundedVisionLLM(),
    )
    result = await pipeline.ingest_file(
        "alice",
        None,
        data.getvalue(),
        filename="aurora-ungrounded.png",
        mime_type="image/png",
        source_ref="vision:aurora:ungrounded",
    )

    vision = result["extraction"]["vision"]
    assert vision["confidence"] <= 0.55
    assert vision["grounded_evidence_count"] == 0
    assert vision["warnings"]
    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    assert inbox is not None and inbox["status"] == "pending"
    suggestions = json.loads(inbox["suggestions_json"])
    assert all(float(item["confidence"]) <= 0.55 for item in suggestions["entities"])


@pytest.mark.asyncio
async def test_vision_failure_without_local_ocr_keeps_private_details_out(settings, storage, monkeypatch):
    private = "VISION-EXCEPTION-SENTINEL-51ec2a"

    class FailingVisionLLM:
        enabled = True
        model = "synthetic-vision"

        async def chat(self, _messages, **_kwargs):
            raise RuntimeError(f"private model detail {private}")

    image = Image.new("RGB", (64, 64), "white")
    data = BytesIO()
    image.save(data, format="PNG")
    pipeline = IngestionPipeline(
        replace(settings, profile=PROFILES["qwen36-vl"]),
        storage,
        KnowledgeGraph(storage),
        FailingVisionLLM(),
    )
    monkeypatch.setattr(pipeline._doc_extractor, "local_ocr_available", lambda: False)

    def forbidden_ocr(*_args, **_kwargs):
        raise AssertionError("unavailable local OCR must not be invoked")

    monkeypatch.setattr(pipeline._doc_extractor, "ocr_visual_assets", forbidden_ocr)
    result = await pipeline.ingest_file(
        "alice",
        None,
        data.getvalue(),
        filename="synthetic-private-error.png",
        mime_type="image/png",
        source_ref="vision:synthetic-private-error",
    )

    encoded_result = json.dumps(result, ensure_ascii=False)
    assert private not in encoded_result
    vision = result["extraction"]["vision"]
    # The unavailable optional provider does not become an invented empty OCR page.
    assert vision["error"] == "vision_request_failed:RuntimeError"
    raw = storage.get_raw_object(result["raw_object_id"], "alice")
    assert private not in json.dumps(raw, ensure_ascii=False)
