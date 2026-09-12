"""Supplemental real-HTTP/storage oracles for six R10 graph and Inbox contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.security import sign_bridge_request
from friday.server import create_app
from friday.storage.models import Entity, EntityType, Relation, RelationType

_GROUP_USER = "r10-directory-owner"
_GROUP_FOREIGN_USER = "r10-directory-foreign"
_GROUP_PRIVATE_CANARY = "R10_GROUP_PRIVATE_CANARY"
_GROUP_RESPONSE_FIELDS = frozenset(
    {
        "user_id",
        "axis",
        "axes",
        "groups",
        "grouped",
        "groups_shown",
        "groups_total",
        "pending_total",
    }
)
_GROUP_FIELDS = frozenset(
    {
        "key",
        "axis",
        "total",
        "actions",
        "inbox_ids",
        "truncated",
        "quality_min",
        "quality_median",
        "quality_max",
    }
)

_ASSISTANT_EXTERNAL_ID = "7002"
_ASSISTANT_TENANT = "telegram:telegram:7002"
_ASSISTANT_PRIVATE_CANARY = "R10_ASSISTANT_PRIVATE_CANARY_5e67f0ad"
_ASSISTANT_RESPONSE_FIELDS = frozenset(
    {
        "idempotent_replay",
        "promoted",
        "queued_for_review",
        "action",
        "candidate_type",
        "reason",
        "raw_object_id",
        "inbox_id",
        "suggestions",
    }
)
_ASSISTANT_REPLAY_FIELDS = _ASSISTANT_RESPONSE_FIELDS - {"suggestions"}
_SUGGESTION_FIELDS = frozenset(
    {
        "title",
        "summary",
        "tags",
        "importance",
        "quality_score",
        "knowledge_kind",
        "entities",
        "metadata",
    }
)
_PRIVATE_RESPONSE_FIELDS = frozenset(
    {
        "assistant_message_id",
        "classification_notes",
        "content",
        "content_hash",
        "conversation_id",
        "metadata_json",
        "raw_content",
        "requested_by",
        "reviewed_by",
        "source_ref",
        "user_id",
    }
)
_OPAQUE_ID = re.compile(r"^(?:raw|inbox)_[0-9a-f]{16}$")


def _group_seed() -> tuple[dict[str, Any], list[tuple[Any, ...]], list[tuple[Any, ...]], str, str]:
    """Freeze the complete expected projection before seeding or requesting it."""

    code_ids = [f"inbox_r10_directory_code_{index:03d}" for index in range(201)]
    document_ids = [f"inbox_r10_directory_document_{index:03d}" for index in range(2)]
    no_import_id = "inbox_r10_directory_chat_000"
    foreign_id = "inbox_r10_directory_foreign_000"
    expected_groups = [
        {
            "key": "/home/r10/Projects/code",
            "axis": "directory",
            "total": 201,
            "actions": {"promote": 201},
            "inbox_ids": code_ids[:200],
            "truncated": True,
            "quality_min": 0.9,
            "quality_median": 0.9,
            "quality_max": 0.9,
        },
        {
            "key": "/home/r10/Documents",
            "axis": "directory",
            "total": 2,
            "actions": {"promote": 2},
            "inbox_ids": document_ids,
            "truncated": False,
            "quality_min": 0.9,
            "quality_median": 0.9,
            "quality_max": 0.9,
        },
        {
            "key": "(не из импорта)",
            "axis": "directory",
            "total": 1,
            "actions": {"promote": 1},
            "inbox_ids": [no_import_id],
            "truncated": False,
            "quality_min": 0.9,
            "quality_median": 0.9,
            "quality_max": 0.9,
        },
    ]
    expected = {
        "user_id": _GROUP_USER,
        "axis": "directory",
        "axes": ["extension", "directory", "source", "quality"],
        "groups": expected_groups,
        "grouped": 204,
        "groups_shown": 3,
        "groups_total": 3,
        "pending_total": 204,
    }

    specifications = [
        *(
            (
                _GROUP_USER,
                f"raw_r10_directory_code_{index:03d}",
                inbox_id,
                f"/home/r10/Projects/code/mod{index:03d}.py",
            )
            for index, inbox_id in enumerate(code_ids)
        ),
        *(
            (
                _GROUP_USER,
                f"raw_r10_directory_document_{index:03d}",
                inbox_id,
                f"/home/r10/Documents/note{index:03d}.md",
            )
            for index, inbox_id in enumerate(document_ids)
        ),
        (_GROUP_USER, "raw_r10_directory_chat_000", no_import_id, None),
        (
            _GROUP_FOREIGN_USER,
            "raw_r10_directory_foreign_000",
            foreign_id,
            "/home/r10/Projects/code/foreign.py",
        ),
    ]
    timestamp = "2026-09-08T00:00:00+00:00"
    raw_rows: list[tuple[Any, ...]] = []
    inbox_rows: list[tuple[Any, ...]] = []
    for index, (user_id, raw_id, inbox_id, path) in enumerate(specifications):
        private_value = f"{_GROUP_PRIVATE_CANARY}:{index:03d}"
        metadata = {"private_canary": private_value}
        if path is not None:
            metadata["import_source_path"] = path
        content = f"private raw body {private_value}"
        source = "upload" if path is not None else "telegram"
        raw_rows.append(
            (
                raw_id,
                user_id,
                source,
                f"private-source-ref:{private_value}",
                content,
                "text/plain",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                hashlib.sha256(content.encode()).hexdigest(),
                timestamp,
                timestamp,
            )
        )
        inbox_rows.append(
            (
                inbox_id,
                user_id,
                raw_id,
                "pending",
                "promote",
                0.9,
                0.9,
                f"private classification note {private_value}",
                timestamp,
            )
        )
    return expected, raw_rows, inbox_rows, code_ids[-1], foreign_id


def _observe_directory_groups(settings) -> dict[str, Any]:
    from friday.server import create_app

    expected, raw_rows, inbox_rows, omitted_id, foreign_id = _group_seed()
    owner_headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        storage.ensure_user(_GROUP_USER, source="upload")
        storage.ensure_user(_GROUP_FOREIGN_USER, source="upload")
        with storage.transaction() as connection:
            connection.executemany(
                """INSERT INTO raw_objects(
                       id, user_id, source, source_ref, raw_content, content_type,
                       metadata_json, content_hash, received_at, created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                raw_rows,
            )
            connection.executemany(
                """INSERT INTO inbox(
                       id, user_id, raw_object_id, status, suggested_action,
                       promotion_score, quality_score, classification_notes, created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                inbox_rows,
            )
        response = client.get(
            "/api/admin/inbox/groups",
            params={"user_id": _GROUP_USER, "by": "directory"},
            headers=owner_headers,
        )
    return {
        "status_code": response.status_code,
        "body": response.json(),
        "expected": expected,
        "omitted_id": omitted_id,
        "foreign_id": foreign_id,
    }


def _assert_directory_group_observation(observation: dict[str, Any]) -> None:
    body = observation["body"]
    expected = observation["expected"]
    assert observation["status_code"] == 200
    assert set(body) == _GROUP_RESPONSE_FIELDS
    assert all(set(group) == _GROUP_FIELDS for group in body["groups"])
    assert body == expected
    member_ids = [member for group in body["groups"] for member in group["inbox_ids"]]
    assert len(member_ids) == len(set(member_ids)) == 203
    assert observation["omitted_id"] not in member_ids
    assert observation["foreign_id"] not in member_ids
    assert _GROUP_PRIVATE_CANARY not in json.dumps(body, ensure_ascii=False, sort_keys=True)


def _signed_post(client: TestClient, settings, path: str, payload: dict[str, Any]):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Friday-Timestamp": str(timestamp),
        "X-Friday-User": _ASSISTANT_EXTERNAL_ID,
        "X-Friday-Chat": _ASSISTANT_EXTERNAL_ID,
        "X-Friday-Nonce": nonce,
        "X-Friday-Signature": sign_bridge_request(
            settings.telegram_bridge_secret,
            timestamp=timestamp,
            method="POST",
            path=path,
            external_user_id=_ASSISTANT_EXTERNAL_ID,
            chat_id=_ASSISTANT_EXTERNAL_ID,
            nonce=nonce,
            body=body,
        ),
    }
    return client.post(path, content=body, headers=headers)


def _candidate_rows(storage) -> dict[str, list[dict[str, Any]]]:
    """Read the whole candidate class, so a replay cannot escape under a new key."""

    raw_rows = [
        dict(row)
        for row in storage.execute(
            """SELECT id, user_id, source, source_ref, raw_content, content_type,
                      content_hash, metadata_json, deleted_at
                 FROM raw_objects AS r
                WHERE r.source='knowledge_work'
                ORDER BY r.id"""
        ).fetchall()
    ]
    for row in raw_rows:
        row["metadata_json"] = json.loads(str(row["metadata_json"]))
    inbox_rows = [
        dict(row)
        for row in storage.execute(
            """SELECT i.id, i.user_id, i.raw_object_id, i.knowledge_object_id,
                      i.status, i.suggested_action
                 FROM inbox AS i
                 JOIN raw_objects AS r ON r.id=i.raw_object_id
                WHERE r.source='knowledge_work'
                ORDER BY i.id"""
        ).fetchall()
    ]
    knowledge_rows = [
        dict(row)
        for row in storage.execute(
            """SELECT k.id, k.user_id, k.raw_object_id
                 FROM knowledge_objects AS k
                 JOIN raw_objects AS r ON r.id=k.raw_object_id
                WHERE r.source='knowledge_work'
                ORDER BY k.id"""
        ).fetchall()
    ]
    return {"raw": raw_rows, "inbox": inbox_rows, "knowledge": knowledge_rows}


def _observe_assistant_candidate(settings) -> dict[str, Any]:
    from friday.server import create_app

    # These transport and ownership expectations are literal, not inferred from a row
    # returned by the implementation under test.
    settings = replace(
        settings,
        shared_archive=False,
        telegram_owner_chat_ids=[],
        telegram_realm_id="telegram",
    )
    expected_tenant = _ASSISTANT_TENANT
    expected_candidate_type = "knowledge_work"
    telegram_user = {"id": int(_ASSISTANT_EXTERNAL_ID), "first_name": "R10 Worker"}
    with TestClient(create_app(settings)) as client:
        mode = _signed_post(
            client,
            settings,
            "/api/conversations/channel/mode",
            {
                "channel": "telegram",
                "channel_id": _ASSISTANT_EXTERNAL_ID,
                "mode": expected_candidate_type,
                "telegram_user": telegram_user,
            },
        )
        assert mode.status_code == 200, mode.text
        answer = _signed_post(
            client,
            settings,
            "/api/chat",
            {
                "message": "Собери структурированную карточку проекта Orion.",
                "source_ref": "telegram-update:r10-assistant-oracle-1",
                "telegram_message_id": 1,
                "telegram_user": telegram_user,
            },
        )
        assert answer.status_code == 200, answer.text
        answer_body = answer.json()
        assert answer_body["context"]["interaction_mode"] == expected_candidate_type
        assert answer_body["context"]["can_queue_to_inbox"] is True

        message_id = str(answer_body["message_id"])
        message = client.app.state.storage.get_message(message_id, expected_tenant)
        assert message is not None and message["role"] == "assistant"
        message_metadata = json.loads(str(message["metadata_json"] or "{}"))
        message_metadata["private_response_canary"] = _ASSISTANT_PRIVATE_CANARY
        client.app.state.storage.execute(
            "UPDATE messages SET metadata_json=? WHERE id=? AND user_id=?",
            (json.dumps(message_metadata, sort_keys=True), message_id, expected_tenant),
        )
        client.app.state.storage.conn.commit()
        expected_source_ref = f"{expected_candidate_type}-answer:{message_id}"
        expected_content = str(message["content"])
        expected_content_hash = hashlib.sha256(expected_content.encode()).hexdigest()
        expected = {
            "tenant": expected_tenant,
            "candidate_type": expected_candidate_type,
            "message_id": message_id,
            "conversation_id": str(message["conversation_id"]),
            "source_ref": expected_source_ref,
            "content": expected_content,
            "content_hash": expected_content_hash,
        }
        before = _candidate_rows(client.app.state.storage)

        first_response = _signed_post(
            client,
            settings,
            "/api/assistant/candidates",
            {"message_id": message_id, "telegram_user": telegram_user},
        )
        first_body = first_response.json()
        after_first = _candidate_rows(client.app.state.storage)

        # The first returned opaque handles become frozen replay expectations before
        # the replay request is issued; no replay value is used to choose them.
        replay_anchor = {
            "raw_object_id": first_body.get("raw_object_id"),
            "inbox_id": first_body.get("inbox_id"),
        }
        replay_response = _signed_post(
            client,
            settings,
            "/api/assistant/candidates",
            {"message_id": message_id, "telegram_user": telegram_user},
        )
        replay_body = replay_response.json()
        after_replay = _candidate_rows(client.app.state.storage)

    return {
        "expected": expected,
        "before": before,
        "first_status": first_response.status_code,
        "first": first_body,
        "after_first": after_first,
        "replay_anchor": replay_anchor,
        "replay_status": replay_response.status_code,
        "replay": replay_body,
        "after_replay": after_replay,
    }


def _nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _nested_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _nested_keys(child)}
    return set()


def _assert_candidate_projection(body: dict[str, Any], *, replay: bool) -> None:
    assert set(body) == (_ASSISTANT_REPLAY_FIELDS if replay else _ASSISTANT_RESPONSE_FIELDS)
    assert body["idempotent_replay"] is replay
    assert body["promoted"] is False
    assert body["queued_for_review"] is True
    assert body["action"] == "review"
    assert body["candidate_type"] == "knowledge_work"
    assert isinstance(body["reason"], str) and 1 <= len(body["reason"]) <= 256
    assert _OPAQUE_ID.fullmatch(str(body["raw_object_id"]))
    assert _OPAQUE_ID.fullmatch(str(body["inbox_id"]))
    assert not (_nested_keys(body) & _PRIVATE_RESPONSE_FIELDS)
    encoded = json.dumps(body, ensure_ascii=False)
    assert _ASSISTANT_PRIVATE_CANARY not in encoded
    assert len(encoded.encode()) <= 65_536
    if replay:
        return
    suggestions = body["suggestions"]
    assert isinstance(suggestions, dict) and set(suggestions) == _SUGGESTION_FIELDS
    assert isinstance(suggestions["title"], str) and len(suggestions["title"]) <= 200
    assert isinstance(suggestions["summary"], str) and len(suggestions["summary"]) <= 2_000
    assert isinstance(suggestions["tags"], list) and len(suggestions["tags"]) <= 16
    assert all(isinstance(tag, str) and len(tag) <= 64 for tag in suggestions["tags"])
    for field in ("importance", "quality_score"):
        assert type(suggestions[field]) in {int, float} and 0 <= suggestions[field] <= 1
    assert isinstance(suggestions["knowledge_kind"], str)
    assert len(suggestions["knowledge_kind"]) <= 80
    assert isinstance(suggestions["entities"], list) and len(suggestions["entities"]) <= 30
    assert isinstance(suggestions["metadata"], dict)


def _assert_assistant_candidate_observation(observation: dict[str, Any]) -> None:
    expected = observation["expected"]
    first = observation["first"]
    replay = observation["replay"]
    assert observation["before"] == {"raw": [], "inbox": [], "knowledge": []}
    assert observation["first_status"] == 200
    assert observation["replay_status"] == 200
    _assert_candidate_projection(first, replay=False)
    _assert_candidate_projection(replay, replay=True)

    raw_id = first["raw_object_id"]
    inbox_id = first["inbox_id"]
    assert observation["replay_anchor"] == {
        "raw_object_id": raw_id,
        "inbox_id": inbox_id,
    }
    assert replay["raw_object_id"] == raw_id
    assert replay["inbox_id"] == inbox_id

    rows = observation["after_first"]
    assert len(rows["raw"]) == len(rows["inbox"]) == 1
    assert rows["knowledge"] == []
    raw = rows["raw"][0]
    inbox = rows["inbox"][0]
    assert raw["id"] == raw_id
    assert raw["user_id"] == expected["tenant"]
    assert raw["source"] == "knowledge_work"
    assert raw["source_ref"] == expected["source_ref"]
    assert raw["raw_content"] == expected["content"]
    assert raw["content_type"] == "text"
    assert raw["content_hash"] == expected["content_hash"]
    assert raw["deleted_at"] is None
    metadata = raw["metadata_json"]
    assert metadata["assistant_message_id"] == expected["message_id"]
    assert metadata["conversation_id"] == expected["conversation_id"]
    assert metadata["requested_by"] == expected["tenant"]
    assert metadata["interaction_mode"] == expected["candidate_type"]
    assert metadata["agent_candidate"] is True
    assert metadata["candidate_type"] == expected["candidate_type"]
    assert metadata["review_only"] is True
    assert inbox == {
        "id": inbox_id,
        "user_id": expected["tenant"],
        "raw_object_id": raw_id,
        "knowledge_object_id": None,
        "status": "pending",
        "suggested_action": "promote",
    }
    assert observation["after_replay"] == rows


def test_inbox_directory_groups_have_exact_membership_truncation_and_projection(settings):
    _assert_directory_group_observation(_observe_directory_groups(settings))


def test_inbox_directory_group_oracle_rejects_observed_mutations(settings):
    observation = _observe_directory_groups(settings)
    _assert_directory_group_observation(observation)
    mutations = {
        "wrong-directory": lambda item: item["body"]["groups"][0].update(key="/forged/wrong"),
        "empty-members": lambda item: item["body"]["groups"][0].update(inbox_ids=[]),
        "duplicate-member": lambda item: item["body"]["groups"][0]["inbox_ids"].__setitem__(
            1, item["body"]["groups"][0]["inbox_ids"][0]
        ),
        "false-truncation": lambda item: item["body"]["groups"][0].update(truncated=False),
        "private-field": lambda item: item["body"]["groups"][0].update(raw_content=_GROUP_PRIVATE_CANARY),
    }
    for _name, mutate in mutations.items():
        damaged = copy.deepcopy(observation)
        mutate(damaged)
        with pytest.raises(AssertionError):
            _assert_directory_group_observation(damaged)


def test_signed_assistant_candidate_has_exact_tenant_replay_rows_and_projection(settings):
    _assert_assistant_candidate_observation(_observe_assistant_candidate(settings))


def test_signed_assistant_candidate_oracle_rejects_observed_mutations(settings):
    observation = _observe_assistant_candidate(settings)
    _assert_assistant_candidate_observation(observation)

    def wrong_tenant(item: dict[str, Any]) -> None:
        item["after_first"]["raw"][0]["user_id"] = "forged-caller"
        item["after_first"]["inbox"][0]["user_id"] = "forged-caller"
        item["after_replay"] = copy.deepcopy(item["after_first"])

    def duplicate_replay(item: dict[str, Any]) -> None:
        duplicate_raw = copy.deepcopy(item["after_replay"]["raw"][0])
        duplicate_raw["id"] = "raw_1111111111111111"
        duplicate_inbox = copy.deepcopy(item["after_replay"]["inbox"][0])
        duplicate_inbox["id"] = "inbox_1111111111111111"
        duplicate_inbox["raw_object_id"] = duplicate_raw["id"]
        item["after_replay"]["raw"].append(duplicate_raw)
        item["after_replay"]["inbox"].append(duplicate_inbox)

    mutations = {
        "wrong-signed-tenant": wrong_tenant,
        "duplicate-replay-rows": duplicate_replay,
        "unstable-replay-id": lambda item: item["replay"].update(raw_object_id="raw_2222222222222222"),
        "private-top-level-field": lambda item: item["first"].update(raw_content="PRIVATE_ASSISTANT_CANARY"),
        "private-allowed-nested-value": lambda item: item["first"]["suggestions"]["metadata"]["urls"].append(
            _ASSISTANT_PRIVATE_CANARY
        ),
    }
    for _name, mutate in mutations.items():
        damaged = copy.deepcopy(observation)
        mutate(damaged)
        with pytest.raises(AssertionError):
            _assert_assistant_candidate_observation(damaged)


RelationIdentity = tuple[str, str, str, str]


@dataclass(frozen=True)
class _SnapshotExpectation:
    node_ids: frozenset[str]
    relations: frozenset[RelationIdentity]


@dataclass(frozen=True)
class _NeighbourhoodExpectation:
    root_id: str
    known_at: str
    current: _SnapshotExpectation
    historical: _SnapshotExpectation


@dataclass(frozen=True)
class _OverviewExpectation:
    as_of: str
    snapshot: _SnapshotExpectation


def _relation_identity(item: dict) -> RelationIdentity:
    return (
        str(item["id"]),
        str(item["source_entity_id"]),
        str(item["target_entity_id"]),
        str(item["relation_type"]),
    )


def _overview_relation_identity(item: dict) -> RelationIdentity:
    return (
        str(item["id"]),
        str(item["source"]),
        str(item["target"]),
        str(item["relation_type"]),
    )


def _assert_exact_nodes(items: list[dict], expected_ids: frozenset[str]) -> None:
    observed_ids = [str(item["id"]) for item in items]
    assert len(observed_ids) == len(set(observed_ids)), "duplicate graph node"
    assert frozenset(observed_ids) == expected_ids, "wrong graph node membership"


def _assert_exact_relations(
    items: list[dict],
    expected: frozenset[RelationIdentity],
    *,
    overview: bool = False,
) -> None:
    project = _overview_relation_identity if overview else _relation_identity
    observed = [project(item) for item in items]
    observed_ids = [identity[0] for identity in observed]
    assert len(observed_ids) == len(set(observed_ids)), "duplicate graph relation"
    assert frozenset(observed) == expected, "wrong graph relation membership or endpoints"


def _assert_local_graph(
    body: dict,
    expected: _SnapshotExpectation,
    *,
    root_id: str,
    known_at: str,
) -> None:
    assert body["root"] == root_id
    assert body["known_at"] == known_at
    assert body["temporal_basis"] == ("bitemporal" if known_at else "valid_time")
    _assert_exact_nodes(body["nodes"], expected.node_ids)
    _assert_exact_relations(body["edges"], expected.relations)
    assert body["nodes_matched_at_least"] == len(expected.node_ids)
    assert body["edges_matched_at_least"] == len(expected.relations)
    assert body["nodes_truncated"] is False
    assert body["edges_truncated"] is False


def _assert_neighbourhood_observation(observed: dict[str, dict], expected: _NeighbourhoodExpectation) -> None:
    for surface in ("public", "admin"):
        _assert_local_graph(
            observed[f"{surface}_current"],
            expected.current,
            root_id=expected.root_id,
            known_at="",
        )
        _assert_local_graph(
            observed[f"{surface}_historical"],
            expected.historical,
            root_id=expected.root_id,
            known_at=expected.known_at,
        )

    for moment, snapshot in (
        ("current", expected.current),
        ("historical", expected.historical),
    ):
        card = observed[f"entity_{moment}"]
        _assert_exact_relations(card["relations"], snapshot.relations)
        assert card["relations_matched_at_least"] == len(snapshot.relations)
        assert card["relations_truncated"] is False
        assert card["temporal_basis"] == ("bitemporal" if moment == "historical" else "valid_time")
        if moment == "historical":
            assert card["known_at"] == expected.known_at

    profile = observed["profile_current"]
    _assert_exact_relations(profile["relations"], expected.current.relations)
    assert profile["relations_matched_at_least"] == len(expected.current.relations)
    assert profile["relations_truncated"] is False


def _get_json(client: TestClient, path: str, headers: dict[str, str], **params: str) -> dict:
    response = client.get(path, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _observe_neighbourhood(settings) -> tuple[_NeighbourhoodExpectation, dict[str, dict]]:
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        headers = {"Authorization": f"Bearer {settings.api_token}"}

        root = kg.create_entity(LEGACY_OWNER_USER_ID, "Oracle root", EntityType.PROJECT)
        old_target = kg.create_entity(
            LEGACY_OWNER_USER_ID, "Oracle historical target", EntityType.ORGANIZATION
        )
        new_target = kg.create_entity(LEGACY_OWNER_USER_ID, "Oracle current target", EntityType.ORGANIZATION)
        stable_target = kg.create_entity(LEGACY_OWNER_USER_ID, "Oracle stable target", EntityType.CONCEPT)
        moving = kg.create_relation(
            LEGACY_OWNER_USER_ID,
            str(root["id"]),
            str(old_target["id"]),
            RelationType.MEMBER_OF,
            weight=0.61,
        )
        stable = kg.create_relation(
            LEGACY_OWNER_USER_ID,
            str(root["id"]),
            str(stable_target["id"]),
            RelationType.RELATED_TO,
            weight=0.73,
        )
        boundary_row = storage.execute(
            """SELECT recorded_at FROM relation_revisions
                 WHERE user_id=? ORDER BY event_seq DESC LIMIT 1""",
            (LEGACY_OWNER_USER_ID,),
        ).fetchone()
        assert boundary_row is not None
        historical_known_at = str(boundary_row["recorded_at"])

        with storage.transaction() as connection:
            connection.execute(
                """UPDATE relations
                      SET target_entity_id=?, relation_type=?, weight=?
                    WHERE id=? AND user_id=?""",
                (
                    str(new_target["id"]),
                    RelationType.WORKS_ON.value,
                    0.89,
                    moving.id,
                    LEGACY_OWNER_USER_ID,
                ),
            )

        # Freeze IDs and complete relation tuples before any HTTP observation.
        historical = _SnapshotExpectation(
            node_ids=frozenset({str(root["id"]), str(old_target["id"]), str(stable_target["id"])}),
            relations=frozenset(
                {
                    (
                        moving.id,
                        str(root["id"]),
                        str(old_target["id"]),
                        RelationType.MEMBER_OF.value,
                    ),
                    (
                        stable.id,
                        str(root["id"]),
                        str(stable_target["id"]),
                        RelationType.RELATED_TO.value,
                    ),
                }
            ),
        )
        current = _SnapshotExpectation(
            node_ids=frozenset({str(root["id"]), str(new_target["id"]), str(stable_target["id"])}),
            relations=frozenset(
                {
                    (
                        moving.id,
                        str(root["id"]),
                        str(new_target["id"]),
                        RelationType.WORKS_ON.value,
                    ),
                    (
                        stable.id,
                        str(root["id"]),
                        str(stable_target["id"]),
                        RelationType.RELATED_TO.value,
                    ),
                }
            ),
        )
        expected = _NeighbourhoodExpectation(
            root_id=str(root["id"]),
            known_at=historical_known_at,
            current=current,
            historical=historical,
        )

        public_path = f"/api/kg/graph/{root['id']}"
        admin_path = f"/api/admin/graph/{root['id']}"
        entity_path = f"/api/kg/entities/{root['id']}"
        observed = {
            "public_current": _get_json(client, public_path, headers),
            "public_historical": _get_json(client, public_path, headers, known_at=historical_known_at),
            "admin_current": _get_json(client, admin_path, headers, user_id=LEGACY_OWNER_USER_ID),
            "admin_historical": _get_json(
                client,
                admin_path,
                headers,
                user_id=LEGACY_OWNER_USER_ID,
                known_at=historical_known_at,
            ),
            "entity_current": _get_json(client, entity_path, headers),
            "entity_historical": _get_json(client, entity_path, headers, known_at=historical_known_at),
            "profile_current": _get_json(client, "/api/kg/entity-profile", headers, name="Oracle root"),
        }
    return expected, observed


def test_neighbourhood_http_oracle_distinguishes_current_from_known_at_and_exact_members(
    settings,
) -> None:
    expected, observed = _observe_neighbourhood(settings)
    _assert_neighbourhood_observation(observed, expected)


def test_neighbourhood_http_oracle_rejects_observed_result_mutations(settings) -> None:
    expected, observed = _observe_neighbourhood(settings)
    _assert_neighbourhood_observation(observed, expected)

    historical_is_current = copy.deepcopy(observed)
    for surface in ("public", "admin"):
        historical_is_current[f"{surface}_historical"]["nodes"] = copy.deepcopy(
            observed[f"{surface}_current"]["nodes"]
        )
        historical_is_current[f"{surface}_historical"]["edges"] = copy.deepcopy(
            observed[f"{surface}_current"]["edges"]
        )
    historical_is_current["entity_historical"]["relations"] = copy.deepcopy(
        observed["entity_current"]["relations"]
    )

    wrong_endpoint = copy.deepcopy(observed)
    wrong_endpoint["public_current"]["edges"][0]["target_entity_id"] = "ent-fabricated"

    duplicate_member = copy.deepcopy(observed)
    duplicate_member["admin_current"]["edges"][1] = copy.deepcopy(
        duplicate_member["admin_current"]["edges"][0]
    )

    for mutant in (historical_is_current, wrong_endpoint, duplicate_member):
        with pytest.raises(AssertionError):
            _assert_neighbourhood_observation(mutant, expected)


def _assert_overview_snapshot(body: dict, expected: _OverviewExpectation) -> None:
    assert body["as_of"] == expected.as_of
    assert body["known_at"] == ""
    assert body["temporal_basis"] == "valid_time"
    _assert_exact_nodes(body["nodes"], expected.snapshot.node_ids)
    _assert_exact_relations(body["edges"], expected.snapshot.relations, overview=True)
    assert body["shown"] == len(expected.snapshot.node_ids)
    assert body["total"] == len(expected.snapshot.node_ids)
    assert body["nodes_matched_at_least"] == len(expected.snapshot.node_ids)
    assert body["edges_matched_at_least"] == len(expected.snapshot.relations)
    assert body["nodes_truncated"] is False
    assert body["edges_truncated"] is False


def _assert_overview_observation(
    observed: dict[str, dict], expected: dict[str, _OverviewExpectation]
) -> None:
    for moment in ("empty", "middle", "late"):
        _assert_overview_snapshot(observed[moment], expected[moment])
    assert observed["empty"]["nodes"] == []
    assert observed["empty"]["edges"] == []


def _observe_overview(
    settings,
) -> tuple[dict[str, _OverviewExpectation], dict[str, dict]]:
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        root = Entity(
            "ent-overview-oracle-root",
            LEGACY_OWNER_USER_ID,
            "Overview root",
            EntityType.PROJECT,
        )
        middle = Entity(
            "ent-overview-oracle-middle",
            LEGACY_OWNER_USER_ID,
            "Overview middle",
            EntityType.ORGANIZATION,
        )
        late = Entity(
            "ent-overview-oracle-late",
            LEGACY_OWNER_USER_ID,
            "Overview late",
            EntityType.CONCEPT,
        )
        storage.create_entity(root)
        storage.create_entity(middle)
        storage.create_entity(late)
        first = Relation(
            "rel-overview-oracle-first",
            LEGACY_OWNER_USER_ID,
            root.id,
            middle.id,
            RelationType.MEMBER_OF,
            weight=0.82,
            valid_from="2024-01-01",
        )
        second = Relation(
            "rel-overview-oracle-second",
            LEGACY_OWNER_USER_ID,
            root.id,
            late.id,
            RelationType.WORKS_ON,
            weight=0.71,
            valid_from="2025-01-01",
        )
        storage.create_relation(first)
        storage.create_relation(second)

        # Freeze the three complete temporal projections before making requests.
        expected = {
            "empty": _OverviewExpectation(
                as_of="2023-01-01",
                snapshot=_SnapshotExpectation(frozenset(), frozenset()),
            ),
            "middle": _OverviewExpectation(
                as_of="2024-06-01",
                snapshot=_SnapshotExpectation(
                    frozenset({root.id, middle.id}),
                    frozenset(
                        {
                            (
                                first.id,
                                root.id,
                                middle.id,
                                RelationType.MEMBER_OF.value,
                            )
                        }
                    ),
                ),
            ),
            "late": _OverviewExpectation(
                as_of="2025-06-01",
                snapshot=_SnapshotExpectation(
                    frozenset({root.id, middle.id, late.id}),
                    frozenset(
                        {
                            (
                                first.id,
                                root.id,
                                middle.id,
                                RelationType.MEMBER_OF.value,
                            ),
                            (
                                second.id,
                                root.id,
                                late.id,
                                RelationType.WORKS_ON.value,
                            ),
                        }
                    ),
                ),
            ),
        }
        observed = {
            "empty": _get_json(
                client,
                "/api/admin/graph",
                headers,
                user_id=LEGACY_OWNER_USER_ID,
                as_of="2023",
            ),
            "middle": _get_json(
                client,
                "/api/admin/graph",
                headers,
                user_id=LEGACY_OWNER_USER_ID,
                as_of="2024/6",
            ),
            "late": _get_json(
                client,
                "/api/admin/graph",
                headers,
                user_id=LEGACY_OWNER_USER_ID,
                as_of="2025/6",
            ),
        }
    return expected, observed


def test_overview_http_oracle_has_exact_temporal_members_and_a_truly_empty_earlier_graph(
    settings,
) -> None:
    expected, observed = _observe_overview(settings)
    _assert_overview_observation(observed, expected)


def test_overview_http_oracle_rejects_observed_result_mutations(settings) -> None:
    expected, observed = _observe_overview(settings)
    _assert_overview_observation(observed, expected)

    wrong_endpoint = copy.deepcopy(observed)
    wrong_endpoint["middle"]["edges"][0]["target"] = "ent-fabricated"

    nonempty_earlier_graph = copy.deepcopy(observed)
    nonempty_earlier_graph["empty"]["edges"] = [copy.deepcopy(observed["middle"]["edges"][0])]

    duplicate_member = copy.deepcopy(observed)
    duplicate_member["late"]["edges"][1] = copy.deepcopy(duplicate_member["late"]["edges"][0])

    for mutant in (wrong_endpoint, nonempty_earlier_graph, duplicate_member):
        with pytest.raises(AssertionError):
            _assert_overview_observation(mutant, expected)


_RELATION_RESPONSE_KEYS = {
    "id",
    "source_entity_id",
    "target_entity_id",
    "relation_type",
    "weight",
    "valid_from",
    "valid_to",
    "created_at",
    "invalidated_at",
    "superseded_by",
    "provenance",
}
_RELATION_AUDIT_KEYS = {
    "id",
    "source_entity_id",
    "target_entity_id",
    "relation_type",
    "weight",
    "valid_from",
    "valid_to",
    "created_at",
}
_CANDIDATE_CARD_KEYS = {
    "id",
    "source_entity_id",
    "target_entity_id",
    "relation_type",
    "confidence",
    "status",
    "created_at",
    "reviewed_at",
    "source_name",
    "source_type",
    "target_name",
    "target_type",
    "evidence",
}


def _rows(storage, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in storage.execute(query, params).fetchall()]


def _relation_rows(storage, user_id: str) -> list[dict[str, Any]]:
    return _rows(
        storage,
        """SELECT id,user_id,source_entity_id,target_entity_id,relation_type,
                         weight,metadata_json,created_at,deleted_at,valid_from,
                         valid_to,invalidated_at,superseded_by
                    FROM relations WHERE user_id=? ORDER BY id""",
        (user_id,),
    )


def _revision_rows(storage, user_id: str) -> list[dict[str, Any]]:
    return _rows(
        storage,
        """SELECT event_seq,relation_id,revision,present,operation,recorded_at,
                         batch_id,history_quality,user_id,source_entity_id,
                         target_entity_id,relation_type,weight,metadata_json,
                         created_at,deleted_at,valid_from,valid_to,invalidated_at,
                         superseded_by
                    FROM relation_revisions WHERE user_id=? ORDER BY event_seq""",
        (user_id,),
    )


def _relation_candidate_rows(storage, user_id: str) -> list[dict[str, Any]]:
    return _rows(
        storage,
        """SELECT id,user_id,source_entity_id,target_entity_id,relation_type,
                         confidence,evidence_json,status,created_at,reviewed_at,reviewed_by
                    FROM relation_candidates WHERE user_id=? ORDER BY id""",
        (user_id,),
    )


def _audit_rows(storage, actions: tuple[str, ...]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in actions)
    return _rows(
        storage,
        f"""SELECT id,user_id,action,target_type,target_id,before_json,after_json,
                          ip_address,request_id,created_at
                     FROM audit_log WHERE action IN ({placeholders}) ORDER BY id""",  # nosec B608
        actions,
    )


def _new_rows(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    key: str = "id",
) -> list[dict[str, Any]]:
    before_ids = {str(row[key]) for row in before}
    assert before_ids <= {str(row[key]) for row in after}
    return [row for row in after if str(row[key]) not in before_ids]


def _assert_relation_projection(
    projection: Mapping[str, Any],
    persisted: Mapping[str, Any],
    frozen: Mapping[str, Any],
    *,
    private_canaries: tuple[str, ...],
) -> None:
    assert set(projection) == _RELATION_RESPONSE_KEYS
    for field in ("source_entity_id", "target_entity_id", "relation_type", "weight", "valid_from"):
        assert projection[field] == frozen[field]
        assert persisted[field] == frozen[field]
    assert persisted["user_id"] == frozen["user_id"]
    assert projection["id"] == persisted["id"]
    assert projection["created_at"] == persisted["created_at"]
    assert projection["valid_to"] == persisted["valid_to"] is None
    assert projection["invalidated_at"] == persisted["invalidated_at"] is None
    assert projection["superseded_by"] == persisted["superseded_by"] is None
    assert persisted["deleted_at"] is None
    assert projection["provenance"] == {"origin": "api"}
    assert json.loads(str(persisted["metadata_json"])) == frozen["metadata_json"]
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True)
    assert all(canary not in encoded for canary in private_canaries)
    assert all(len(value) <= 240 for value in projection.values() if isinstance(value, str))


def _assert_audit(
    row: Mapping[str, Any],
    *,
    action: str,
    target_id: str,
    after: Mapping[str, Any],
    private_canaries: tuple[str, ...],
) -> None:
    assert row["user_id"] == LEGACY_OWNER_USER_ID
    assert row["action"] == action
    assert row["target_type"] == "relation"
    assert row["target_id"] == target_id
    assert row["before_json"] is None
    decoded = json.loads(str(row["after_json"]))
    assert decoded == after
    encoded = json.dumps(decoded, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert len(encoded) <= 2_048
    assert all(canary.encode() not in encoded for canary in private_canaries)


def _assert_relation_revision_matches(revision: Mapping[str, Any], persisted: Mapping[str, Any]) -> None:
    assert revision["relation_id"] == persisted["id"]
    assert revision["revision"] == 1
    assert revision["present"] == 1
    assert revision["operation"] == "insert"
    assert revision["history_quality"] == "captured"
    for field in (
        "user_id",
        "source_entity_id",
        "target_entity_id",
        "relation_type",
        "weight",
        "metadata_json",
        "created_at",
        "deleted_at",
        "valid_from",
        "valid_to",
        "invalidated_at",
        "superseded_by",
    ):
        assert revision[field] == persisted[field]


def _graph_state(storage, user_id: str) -> dict[str, Any]:
    return {
        "candidates": _relation_candidate_rows(storage, user_id),
        "relations": _relation_rows(storage, user_id),
        "revisions": _revision_rows(storage, user_id),
        "audits": _audit_rows(
            storage,
            (
                "admin.relation_candidate.accepted",
                "admin.relation_candidate.rejected",
            ),
        ),
    }


def _assert_candidate_edges(
    relations: list[dict[str, Any]], frozen_by_candidate: Mapping[str, Mapping[str, Any]]
) -> None:
    by_candidate: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for row in relations:
        metadata = json.loads(str(row["metadata_json"]))
        by_candidate[str(metadata.get("candidate_id") or "")] = (row, metadata)
    assert set(by_candidate) == set(frozen_by_candidate)
    for candidate_id, frozen in frozen_by_candidate.items():
        row, metadata = by_candidate[candidate_id]
        assert row["user_id"] == frozen["user_id"]
        assert row["source_entity_id"] == frozen["source_entity_id"]
        assert row["target_entity_id"] == frozen["target_entity_id"]
        assert row["relation_type"] == frozen["relation_type"]
        assert row["weight"] == frozen["confidence"]
        assert row["deleted_at"] is None
        assert row["valid_from"] == ""
        assert row["valid_to"] is None
        assert metadata == {
            "candidate_id": candidate_id,
            "confidence": frozen["confidence"],
            "evidence": frozen["evidence"],
            "origin": "review",
            "reviewed_by": LEGACY_OWNER_USER_ID,
            "source": "reviewed_relation_candidate",
        }


def test_relation_create_binds_http_projection_to_exact_persisted_tuple_and_replay_audit(
    settings,
) -> None:
    secret = "SYNTHETIC_PRIVATE_RELATION_ORACLE_" + "z" * 250_000
    replay_secret = "SYNTHETIC_PRIVATE_RELATION_REPLAY_" + "q" * 250_000
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        source = app.state.kg.create_entity(LEGACY_OWNER_USER_ID, "Oracle source", EntityType.PROJECT)
        target = app.state.kg.create_entity(LEGACY_OWNER_USER_ID, "Oracle target", EntityType.CONCEPT)
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        frozen = {
            "user_id": LEGACY_OWNER_USER_ID,
            "source_entity_id": source["id"],
            "target_entity_id": target["id"],
            "relation_type": "related_to",
            "weight": 0.63,
            "valid_from": "2024-02-03",
            "metadata_json": {
                "private": secret,
                "unbounded": "x" * 250_000,
                "created_by": LEGACY_OWNER_USER_ID,
                "origin": "api",
            },
        }
        relations_before = _relation_rows(storage, LEGACY_OWNER_USER_ID)
        revisions_before = _revision_rows(storage, LEGACY_OWNER_USER_ID)
        audits_before = _audit_rows(storage, ("relation.create", "relation.create.idempotent"))

        created = client.post(
            "/api/kg/relations",
            headers=headers,
            json={
                "source_entity_id": frozen["source_entity_id"],
                "target_entity_id": frozen["target_entity_id"],
                "relation_type": frozen["relation_type"],
                "weight": frozen["weight"],
                "valid_from": frozen["valid_from"],
                "metadata": {
                    "private": secret,
                    "unbounded": "x" * 250_000,
                    "created_by": "forged",
                    "origin": "forged",
                },
            },
        )
        assert created.status_code == 200, created.text
        assert set(created.json()) == {"relation", "idempotent_replay"}
        assert created.json()["idempotent_replay"] is False
        projection = created.json()["relation"]

        relations_after = _relation_rows(storage, LEGACY_OWNER_USER_ID)
        persisted_delta = _new_rows(relations_before, relations_after)
        assert len(persisted_delta) == 1
        persisted = persisted_delta[0]
        _assert_relation_projection(
            projection,
            persisted,
            frozen,
            private_canaries=(secret, replay_secret),
        )
        revision_delta = _new_rows(
            revisions_before,
            _revision_rows(storage, LEGACY_OWNER_USER_ID),
            key="event_seq",
        )
        assert len(revision_delta) == 1
        _assert_relation_revision_matches(revision_delta[0], persisted)

        audits_after_create = _audit_rows(storage, ("relation.create", "relation.create.idempotent"))
        create_audit_delta = _new_rows(audits_before, audits_after_create)
        assert len(create_audit_delta) == 1
        initial_audit_after = {field: persisted[field] for field in _RELATION_AUDIT_KEYS}
        _assert_audit(
            create_audit_delta[0],
            action="relation.create",
            target_id=str(projection["id"]),
            after=initial_audit_after,
            private_canaries=(secret, replay_secret),
        )

        frozen_state_before_replay = {
            "relations": relations_after,
            "revisions": _revision_rows(storage, LEGACY_OWNER_USER_ID),
        }
        frozen_audits_before_replay = audits_after_create
        replayed = client.post(
            "/api/kg/relations",
            headers=headers,
            json={
                "source_entity_id": frozen["source_entity_id"],
                "target_entity_id": frozen["target_entity_id"],
                "relation_type": frozen["relation_type"],
                "weight": 0.11,
                "valid_from": "2030-12-31",
                "metadata": {"private": replay_secret, "origin": "forged"},
            },
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == {"relation": projection, "idempotent_replay": True}
        assert {
            "relations": _relation_rows(storage, LEGACY_OWNER_USER_ID),
            "revisions": _revision_rows(storage, LEGACY_OWNER_USER_ID),
        } == frozen_state_before_replay

        audits_after_replay = _audit_rows(storage, ("relation.create", "relation.create.idempotent"))
        replay_audit_delta = _new_rows(frozen_audits_before_replay, audits_after_replay)
        assert len(replay_audit_delta) == 1
        replay_audit_after = {**initial_audit_after, "idempotent_replay": True}
        _assert_audit(
            replay_audit_delta[0],
            action="relation.create.idempotent",
            target_id=str(projection["id"]),
            after=replay_audit_after,
            private_canaries=(secret, replay_secret, "forged"),
        )


def test_bulk_review_binds_candidates_to_exact_edges_and_refusals_leave_state_unchanged(
    settings,
) -> None:
    user_id = "graph-oracle-user"
    secret = "SYNTHETIC_PRIVATE_BULK_ORACLE_" + "p" * 3_000
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        graph = app.state.kg
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        storage.ensure_user(user_id)
        source = graph.create_entity(user_id, "Orion", EntityType.PROJECT)
        target_a = graph.create_entity(user_id, "PostgreSQL", EntityType.CONCEPT)
        target_b = graph.create_entity(user_id, "Redis", EntityType.CONCEPT)
        evidence_a = {"source": "oracle-a", "private": secret}
        evidence_b = {"source": "oracle-b", "private": secret[::-1]}
        first = storage.store_relation_candidate(
            user_id,
            source["id"],
            target_a["id"],
            "uses",
            confidence=0.76,
            evidence=evidence_a,
        )
        second = storage.store_relation_candidate(
            user_id,
            source["id"],
            target_b["id"],
            "uses",
            confidence=0.68,
            evidence=evidence_b,
        )
        frozen_by_candidate = {
            str(first["id"]): {
                "user_id": user_id,
                "source_entity_id": source["id"],
                "target_entity_id": target_a["id"],
                "relation_type": "uses",
                "confidence": 0.76,
                "evidence": evidence_a,
                "source_name": "Orion",
                "target_name": "PostgreSQL",
            },
            str(second["id"]): {
                "user_id": user_id,
                "source_entity_id": source["id"],
                "target_entity_id": target_b["id"],
                "relation_type": "uses",
                "confidence": 0.68,
                "evidence": evidence_b,
                "source_name": "Orion",
                "target_name": "Redis",
            },
        }
        state_before_accept = _graph_state(storage, user_id)
        accepted = client.post(
            "/api/admin/relation-candidates/bulk-review",
            headers=headers,
            json={
                "user_id": user_id,
                "candidate_ids": [first["id"], second["id"], "missing"],
                "status": "accepted",
            },
        )
        assert accepted.status_code == 200, accepted.text
        payload = accepted.json()
        assert set(payload) == {"user_id", "status", "changed", "changed_count", "skipped"}
        assert payload["user_id"] == user_id
        assert payload["status"] == "accepted"
        assert payload["changed_count"] == 2
        assert payload["skipped"] == [{"id": "missing", "reason": "not_found"}]
        assert [row["id"] for row in payload["changed"]] == [first["id"], second["id"]]

        candidate_after = {row["id"]: row for row in _relation_candidate_rows(storage, user_id)}
        for card in payload["changed"]:
            candidate_id = str(card["id"])
            frozen = frozen_by_candidate[candidate_id]
            persisted_candidate = candidate_after[candidate_id]
            assert set(card) == _CANDIDATE_CARD_KEYS
            assert card["source_entity_id"] == frozen["source_entity_id"]
            assert card["target_entity_id"] == frozen["target_entity_id"]
            assert card["relation_type"] == frozen["relation_type"]
            assert card["confidence"] == frozen["confidence"]
            assert card["status"] == "accepted"
            assert card["source_name"] == frozen["source_name"]
            assert card["target_name"] == frozen["target_name"]
            assert card["source_type"] == "project"
            assert card["target_type"] == "concept"
            expected_evidence_bytes = len(
                json.dumps(frozen["evidence"], ensure_ascii=False, sort_keys=True).encode()
            )
            assert card["evidence"] == {"present": True, "bytes": expected_evidence_bytes}
            assert persisted_candidate["status"] == "accepted"
            assert persisted_candidate["reviewed_by"] == LEGACY_OWNER_USER_ID
            assert persisted_candidate["reviewed_at"] == card["reviewed_at"]
            assert json.loads(str(persisted_candidate["evidence_json"])) == frozen["evidence"]

        encoded_response = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        assert secret not in encoded_response
        assert secret[::-1] not in encoded_response
        assert "evidence_json" not in encoded_response
        assert "reviewed_by" not in encoded_response

        state_after_accept = _graph_state(storage, user_id)
        relation_delta = _new_rows(state_before_accept["relations"], state_after_accept["relations"])
        assert len(relation_delta) == 2
        _assert_candidate_edges(relation_delta, frozen_by_candidate)
        revision_delta = _new_rows(
            state_before_accept["revisions"],
            state_after_accept["revisions"],
            key="event_seq",
        )
        assert len(revision_delta) == 2
        relation_by_id = {row["id"]: row for row in relation_delta}
        for revision in revision_delta:
            _assert_relation_revision_matches(revision, relation_by_id[revision["relation_id"]])

        audit_delta = _new_rows(state_before_accept["audits"], state_after_accept["audits"])
        assert len(audit_delta) == 2
        assert {row["target_id"] for row in audit_delta} == set(frozen_by_candidate)
        for row in audit_delta:
            candidate_id = str(row["target_id"])
            assert row["user_id"] == LEGACY_OWNER_USER_ID
            assert row["action"] == "admin.relation_candidate.accepted"
            assert row["target_type"] == "relation_candidate"
            assert row["before_json"] is None
            decoded = json.loads(str(row["after_json"]))
            assert set(decoded) == {
                "id",
                "source_entity_id",
                "target_entity_id",
                "relation_type",
                "confidence",
                "status",
                "created_at",
                "reviewed_at",
                "private_fields_count",
                "private_items_count",
            }
            assert decoded["id"] == candidate_id
            assert decoded["source_entity_id"] == frozen_by_candidate[candidate_id]["source_entity_id"]
            assert decoded["target_entity_id"] == frozen_by_candidate[candidate_id]["target_entity_id"]
            assert decoded["relation_type"] == frozen_by_candidate[candidate_id]["relation_type"]
            assert decoded["confidence"] == frozen_by_candidate[candidate_id]["confidence"]
            assert decoded["status"] == "accepted"
            assert decoded["created_at"] == candidate_after[candidate_id]["created_at"]
            assert decoded["reviewed_at"] == candidate_after[candidate_id]["reviewed_at"]
            assert decoded["private_fields_count"] == 1
            assert decoded["private_items_count"] == 2
            encoded_audit = json.dumps(decoded, ensure_ascii=False, sort_keys=True).encode()
            assert len(encoded_audit) <= 2_048
            assert secret.encode() not in encoded_audit
            assert secret[::-1].encode() not in encoded_audit

        frozen_terminal_state = state_after_accept
        frozen_terminal_response = {
            "user_id": user_id,
            "status": "rejected",
            "changed": [],
            "changed_count": 0,
            "skipped": [
                {
                    "id": first["id"],
                    "reason": ("Relation candidate is already accepted; reviewed decisions are terminal"),
                }
            ],
        }
        terminal = client.post(
            "/api/admin/relation-candidates/bulk-review",
            headers=headers,
            json={
                "user_id": user_id,
                "candidate_ids": [first["id"]],
                "status": "rejected",
            },
        )
        assert terminal.status_code == 200, terminal.text
        assert terminal.json() == frozen_terminal_response
        assert _graph_state(storage, user_id) == frozen_terminal_state

        target_c = graph.create_entity(user_id, "SQLite", EntityType.CONCEPT)
        third = storage.store_relation_candidate(
            user_id,
            source["id"],
            target_c["id"],
            "uses",
            confidence=0.57,
            evidence={"source": "oracle-c", "private": secret},
        )
        oversized_ids = [third["id"], *(f"candidate-{index}" for index in range(200))]
        assert len(oversized_ids) == len(set(oversized_ids)) == 201
        frozen_refusal_state = _graph_state(storage, user_id)
        frozen_refusal_body = {"detail": "За раз можно разобрать не больше 200 кандидатов связей"}
        refused = client.post(
            "/api/admin/relation-candidates/bulk-review",
            headers=headers,
            json={"user_id": user_id, "candidate_ids": oversized_ids, "status": "rejected"},
        )
        assert refused.status_code == 400, refused.text
        assert refused.json() == frozen_refusal_body
        assert _graph_state(storage, user_id) == frozen_refusal_state
