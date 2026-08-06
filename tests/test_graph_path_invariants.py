"""One coherent temporal/provenance route must survive graph expansion.

These tests pin proposal 26 at the graph boundary.  Retrieval and the agent may
only explain the snapshot produced here; reconstructing a route later from the
flat relation list would mix paths, dates, and evidence.
"""

from __future__ import annotations

import hashlib
import json

import pytest

import friday.knowledge_graph as knowledge_graph_module
import friday.storage._graph as storage_graph_module
from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import (
    Entity,
    EntityType,
    KnowledgeObject,
    RawObject,
    Relation,
    RelationHistorySnapshotError,
    new_id,
)


def _entity(storage, user_id: str, name: str) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type="thing")
    storage.create_entity(entity)
    return entity.id


def _knowledge(storage, user_id: str, title: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=title,
        content_type="text",
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=title,
        content_type="text",
        title=title,
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _relation(
    storage,
    user_id: str,
    source: str,
    target: str,
    *,
    relation_id: str | None = None,
    weight: float = 1.0,
    metadata: dict | None = None,
    valid_from: str = "",
    valid_to: str | None = None,
) -> str:
    relation = Relation(
        id=relation_id or new_id("rel"),
        user_id=user_id,
        source_entity_id=source,
        target_entity_id=target,
        relation_type="related_to",
        weight=weight,
        metadata_json=metadata or {"origin": "manual"},
        valid_from=valid_from,
        valid_to=valid_to,
    )
    storage.create_relation(relation)
    return relation.id


def _path_to(context: dict, target: str) -> dict:
    return next(path for path in context["paths"] if path["target"] == target)


def test_two_hop_route_keeps_order_direction_and_allowlisted_provenance(storage) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    bridge = _entity(storage, "alice", "Бета")
    target = _entity(storage, "alice", "Гамма")
    first_ko = _knowledge(storage, "alice", "Основание первой связи")
    target_ko = _knowledge(storage, "alice", "Карточка Гаммы")
    storage.link_knowledge_entity("alice", target_ko, target, status="accepted")
    grounding = storage.store_relation_candidate(
        "alice",
        bridge,
        alpha,
        "related_to",
        confidence=0.81,
        evidence={
            "knowledge_object_id": first_ko,
            "span": "PRIVATE SENTINEL must never leave metadata",
        },
    )
    grounding_id = str(grounding["id"])
    with storage.transaction() as connection:
        connection.execute(
            """UPDATE relation_candidates
               SET status='accepted', reviewed_by='owner', reviewed_at='2024-01-01T00:00:00Z'
               WHERE id=? AND user_id='alice'""",
            (grounding_id,),
        )

    first = _relation(
        storage,
        "alice",
        bridge,
        alpha,
        relation_id="rel-first",
        metadata={
            "origin": "review",
            "source": "reviewed_relation_candidate",
            "candidate_id": grounding_id,
            "reviewed_by": "owner",
            "confidence": 0.81,
            "evidence": {
                "knowledge_object_id": first_ko,
                "span": "PRIVATE SENTINEL must never leave metadata",
            },
            "private": "PRIVATE SENTINEL",
        },
        valid_from="2020-01-01",
    )
    second = _relation(
        storage,
        "alice",
        bridge,
        target,
        relation_id="rel-second",
        metadata={"origin": "manual", "created_by": "alice", "secret": "PRIVATE SENTINEL"},
    )

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=2)
    path = _path_to(context, target)

    assert path["entity_ids"] == [alpha, bridge, target]
    assert [step["id"] for step in path["edges"]] == [first, second]
    assert [step["direction"] for step in path["edges"]] == ["reverse", "forward"]
    assert [(step["from"], step["to"]) for step in path["edges"]] == [
        (alpha, bridge),
        (bridge, target),
    ]
    assert [(step["source"], step["target"]) for step in path["edges"]] == [
        (bridge, alpha),
        (bridge, target),
    ]
    assert [(item["id"], item["name"]) for item in path["entities"]] == [
        (alpha, "Альфа"),
        (bridge, "Бета"),
        (target, "Гамма"),
    ]
    assert path["edges"][0]["provenance"] == {
        "origin": "review",
        "source": "reviewed_relation_candidate",
        "candidate_id": grounding_id,
        "reviewed": True,
        "confidence": 0.81,
        "knowledge_object_id": first_ko,
    }
    assert path["edges"][0]["knowledge_object_id"] == first_ko
    assert "PRIVATE SENTINEL" not in json.dumps(context, ensure_ascii=False)
    assert all("metadata_json" not in relation for relation in context["relations"])
    assert all(len(step.keys()) <= 18 for step in path["edges"]), "raw relation rows leaked into paths"

    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == target_ko
    )
    assert candidate["path_id"] == path["path_id"]
    anchored = [item for item in candidate["evidence"] if item.get("path_id")]
    assert len(anchored) == 1 and anchored[0]["path_id"] == path["path_id"]
    assert anchored[0]["entity_score"] == path["score"]


def test_context_never_retains_raw_relation_metadata_current_or_known_at(storage) -> None:
    """Mutation kill: bypassing `_public_relation` leaks the 250 KB sentinel."""

    secret = "SYNTHETIC_PRIVATE_CONTEXT_RELATION_" + "p" * 250_000
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    relation_id = _relation(
        storage,
        "alice",
        root,
        target,
        metadata={"origin": "manual", "private": secret, "unbounded": secret},
    )
    revision = storage.execute(
        """SELECT recorded_at FROM relation_revisions
             WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1""",
        (relation_id,),
    ).fetchone()
    assert revision is not None

    graph = KnowledgeGraph(storage)
    contexts = (
        graph.context_for_query("alice", "Альфа", depth=1),
        graph.context_for_query(
            "alice",
            "Альфа",
            depth=1,
            known_at=str(revision["recorded_at"]),
        ),
    )
    for context in contexts:
        encoded = json.dumps(context, ensure_ascii=False)
        assert secret not in encoded
        assert all("metadata_json" not in relation for relation in context["relations"])
        assert len(encoded) < 100_000


def test_as_of_is_normalized_before_roots_and_applied_to_every_hop(storage, monkeypatch) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    bridge = _entity(storage, "alice", "Бета")
    future = _entity(storage, "alice", "Будущее")
    ended = _entity(storage, "alice", "Прошлое")
    _relation(storage, "alice", alpha, bridge, valid_from="2020-01-01")
    _relation(storage, "alice", bridge, future, valid_from="2025-01-01")
    _relation(storage, "alice", bridge, ended, valid_from="2019-01-01", valid_to="2023-01-01")

    graph = KnowledgeGraph(storage)
    seen_as_of: list[str] = []
    original = knowledge_graph_module._current_entity_relations_for_traversal

    def spy(
        storage_arg,
        entity_id: str,
        user_id: str,
        *,
        as_of: str = "",
        row_limit: int | None = None,
    ) -> list[dict]:
        seen_as_of.append(as_of)
        assert row_limit == 513
        return original(storage_arg, entity_id, user_id, as_of=as_of, row_limit=row_limit)

    monkeypatch.setattr(knowledge_graph_module, "_current_entity_relations_for_traversal", spy)
    context = graph.context_for_query("alice", "Альфа", depth=3, as_of="2024/6")

    assert context["as_of"] == "2024-06-01"
    assert context["temporal_basis"] == "valid_time"
    assert seen_as_of and set(seen_as_of) == {"2024-06-01"}
    targets = {path["target"] for path in context["paths"]}
    assert bridge in targets
    assert future not in targets, "a relation that starts later crossed the historical boundary"
    assert ended not in targets, "an ended relation crossed the historical boundary"


def test_invalid_as_of_fails_before_root_lookup(storage, monkeypatch) -> None:
    graph = KnowledgeGraph(storage)

    def roots_must_not_run(*_args, **_kwargs):
        raise AssertionError("date validation happened after root lookup")

    monkeypatch.setattr(graph, "search_entities", roots_must_not_run)
    with pytest.raises(ValueError, match="Invalid date"):
        graph.context_for_query("alice", "что угодно", as_of="not-a-date")


def test_historical_traversal_never_uses_timeless_cooccurrence(storage) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    neighbour = _entity(storage, "alice", "Бета")
    shared = _knowledge(storage, "alice", "Оба имени встретились сейчас")
    for entity_id in (alpha, neighbour):
        storage.link_knowledge_entity("alice", shared, entity_id, status="accepted")

    graph = KnowledgeGraph(storage)
    current = graph.context_for_query("alice", "Альфа", depth=1)
    historical = graph.context_for_query("alice", "Альфа", depth=1, as_of="2020-01-01")

    assert any(path["target"] == neighbour for path in current["paths"])
    assert historical["paths"] == []
    assert not any(relation.get("implicit") for relation in historical["relations"])
    assert historical["as_of"] == "2020-01-01"


def test_known_at_is_normalized_before_roots_and_reaches_every_hop(storage, monkeypatch) -> None:
    """One storage-approved KG boundary reaches every BFS node unchanged.

    The relation spy deliberately reads the current projection: this test owns
    KG wiring, while storage truth-table tests own revision selection. Losing the
    parameter on any hop still turns one of the recorded values into an empty
    string and fails independently of storage contents.
    """

    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    bridge = _entity(storage, "alice", "Бета")
    target = _entity(storage, "alice", "Гамма")
    _relation(storage, "alice", alpha, bridge)
    _relation(storage, "alice", bridge, target)

    requested = "2025-03-04T06:07:08+03:00"
    normalized = "2025-03-04T03:07:08.000000Z"
    floor = "2025-01-01T00:00:00.000000Z"
    status_calls: list[tuple[str, str]] = []

    def status(user_id: str, *, known_at: str = "") -> dict:
        status_calls.append((user_id, known_at))
        return {
            "known_at": normalized,
            "known_at_floor": floor,
            "history_complete": True,
            "identity_basis": "current_names",
        }

    monkeypatch.setattr(storage, "relation_history_status", status)
    graph = KnowledgeGraph(storage)
    seen: list[tuple[str, str]] = []

    def relations(
        _storage,
        entity_id: str,
        user_id: str,
        *,
        include_invalidated: bool = False,
        as_of: str = "",
        known_at: str = "",
        require_live_endpoints: bool = True,
        row_limit: int | None = None,
    ) -> list[dict]:
        del include_invalidated, require_live_endpoints
        assert row_limit == 513
        seen.append((as_of, known_at))
        # The current read keeps this a propagation test rather than duplicating
        # the storage agent's bi-temporal selection tests.
        return storage.get_entity_relations(entity_id, user_id, as_of=as_of)

    monkeypatch.setattr(
        knowledge_graph_module,
        "_assert_entities_existed_at_boundary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(knowledge_graph_module, "_historical_entity_relations", relations)
    context = graph.context_for_query("alice", "Альфа", depth=2, known_at=requested)

    assert status_calls == [
        ("alice", requested),
        ("alice", normalized),
    ], "KG failed to preflight and postflight one normalized boundary"
    assert seen and set(seen) == {("", normalized)}
    assert target in {path["target"] for path in context["paths"]}
    assert context["known_at"] == normalized
    assert context["known_at_floor"] == floor
    assert context["history_complete"] is True
    assert context["identity_basis"] == "current_names"
    assert context["temporal_basis"] == "bitemporal"


def test_invalid_or_merge_crossing_known_at_fails_before_root_lookup(storage, monkeypatch) -> None:
    graph = KnowledgeGraph(storage)

    def roots_must_not_run(*_args, **_kwargs):
        raise AssertionError("known_at refusal happened after root lookup")

    monkeypatch.setattr(graph, "search_entities", roots_must_not_run)
    # A date without an explicit offset is not transaction-time. The real
    # storage status helper owns normalization and must reject it before roots.
    with pytest.raises(ValueError):
        graph.context_for_query("alice", "что угодно", known_at="2025-03-04")

    def merge_crossing(_user_id: str, *, known_at: str = "") -> dict:
        raise ValueError(f"known_at {known_at} пересекает merge")

    monkeypatch.setattr(storage, "relation_history_status", merge_crossing)
    with pytest.raises(ValueError, match="merge"):
        graph.context_for_query(
            "alice",
            "что угодно",
            known_at="2025-03-04T03:07:08Z",
        )


def test_missing_entity_does_not_hide_invalid_known_at(storage, monkeypatch) -> None:
    storage.ensure_user("alice")
    graph = KnowledgeGraph(storage)
    entity_reads: list[str] = []

    def entity_must_not_run(*_args, **_kwargs):
        entity_reads.append("entity")
        raise AssertionError("missing entity lookup ran before known_at normalization")

    monkeypatch.setattr(storage, "get_entity", entity_must_not_run)
    with pytest.raises(ValueError, match="known_at"):
        graph.get_entity_relations("ent-missing", "alice", known_at="2025-03-04")
    assert entity_reads == []


def test_missing_entity_does_not_hide_pre_floor_snapshot_refusal(storage, monkeypatch) -> None:
    storage.ensure_user("alice")
    graph = KnowledgeGraph(storage)
    entity_reads: list[str] = []

    def entity_must_not_run(*_args, **_kwargs):
        entity_reads.append("entity")
        raise AssertionError("missing entity lookup ran before history floor preflight")

    monkeypatch.setattr(storage, "get_entity", entity_must_not_run)
    with pytest.raises(RelationHistorySnapshotError, match="precedes complete relation history"):
        graph.get_entity_relations(
            "ent-missing",
            "alice",
            known_at="2000-01-01T00:00:00Z",
        )
    assert entity_reads == []


def test_graph_overview_never_mixes_current_cooccurrence_into_historical_boundaries(storage) -> None:
    """Mutation: dropping `and not as_of` reintroduces today's implicit edge."""

    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    beta = _entity(storage, "alice", "Бета")
    knowledge_id = _knowledge(storage, "alice", "Оба имени связаны только сегодняшним документом")
    for entity_id in (alpha, beta):
        storage.link_knowledge_entity("alice", knowledge_id, entity_id, status="accepted")

    current = storage.graph_overview("alice")
    as_of = storage.graph_overview("alice", as_of="2024-01-01")
    floor = str(storage.relation_history_status("alice")["known_at_floor"])
    known_at = storage.graph_overview("alice", known_at=floor)

    assert [edge for edge in current["edges"] if edge.get("kind") == "cooccurrence"]
    assert not [edge for edge in as_of["edges"] if edge.get("kind") == "cooccurrence"]
    assert not [edge for edge in known_at["edges"] if edge.get("kind") == "cooccurrence"]


def test_temporal_overview_uses_relation_projection_for_nodes_and_honest_limits(
    storage,
    monkeypatch,
) -> None:
    """Relation-only endpoints remain visible without consulting today's links.

    Mutation kills: restoring the old knowledge-link node query raises from the
    SQL spy; dropping the deterministic limit/count fields fails the repeated
    result and matched/truncated assertions.
    """

    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    beta = _entity(storage, "alice", "Бета")
    gamma = _entity(storage, "alice", "Гамма")
    _relation(storage, "alice", alpha, beta)
    _relation(storage, "alice", alpha, gamma)

    original_execute = storage.execute

    def no_current_links(sql: str, params=()):
        # Privacy closure legitimately checks KO dependencies through a
        # correlated `dependency_link` subquery.  What this test forbids is the
        # old node source based on today's co-occurrence links.
        without_privacy_checks = sql.replace("knowledge_entity_links dependency_link", "")
        if "knowledge_entity_links" in without_privacy_checks:
            raise AssertionError("temporal overview consulted today's knowledge links")
        return original_execute(sql, params)

    monkeypatch.setattr(storage, "execute", no_current_links)
    first = storage.graph_overview("alice", as_of="2024/6", limit=2)
    second = storage.graph_overview("alice", as_of="2024-06-01", limit=2)

    assert [node["id"] for node in first["nodes"]] == [node["id"] for node in second["nodes"]]
    assert first["as_of"] == "2024-06-01"
    assert first["known_at"] == ""
    assert first["identity_basis"] == "current_names"
    assert first["temporal_basis"] == "valid_time"
    assert first["total"] == first["nodes_matched_at_least"] == 3
    assert first["shown"] == 2
    assert first["nodes_truncated"] is True
    assert alpha in {node["id"] for node in first["nodes"]}
    assert all(node["knowledge_count"] == 0 for node in first["nodes"])
    assert not any(edge.get("kind") == "cooccurrence" for edge in first["edges"])

    found = storage.graph_overview("alice", as_of="2024-06-01", search="Гамма")
    assert [node["id"] for node in found["nodes"]] == [gamma]
    assert found["total"] == found["nodes_matched_at_least"] == 1


def test_known_at_refuses_later_soft_delete_and_undelete_but_allows_name_edits(storage) -> None:
    """Entity existence/topology is not reconstructed; current names explicitly are."""

    storage.ensure_user("alice")
    graph = KnowledgeGraph(storage)
    alpha = str(graph.create_entity("alice", "Альфа", EntityType.PROJECT)["id"])
    beta = str(graph.create_entity("alice", "Бета", EntityType.ORGANIZATION)["id"])
    relation_id = _relation(storage, "alice", alpha, beta)
    revision = storage.execute(
        "SELECT recorded_at FROM relation_revisions WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1",
        (relation_id,),
    ).fetchone()
    assert revision is not None
    relation_boundary = str(revision["recorded_at"])

    renamed = graph.update_entity("alice", alpha, name="Альфа сегодня")
    assert renamed and renamed["name"] == "Альфа сегодня"
    assert storage.relation_history_status("alice", relation_boundary)["identity_basis"] == "current_names"

    assert storage.soft_delete_entity(beta, "alice") is True
    with pytest.raises(RelationHistorySnapshotError, match="entity topology"):
        storage.get_entity_graph("alice", alpha, 1, known_at=relation_boundary)

    # A relation revision supplies a schema-floor-valid boundary after deletion
    # but before the later undelete.
    with storage.transaction() as connection:
        connection.execute("UPDATE relations SET weight=0.9 WHERE id=?", (relation_id,))
    deletion_revision = storage.execute(
        "SELECT recorded_at FROM relation_revisions WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1",
        (relation_id,),
    ).fetchone()
    assert deletion_revision is not None
    deletion_boundary = str(deletion_revision["recorded_at"])
    assert storage.undelete_entity(beta, "alice") is not None
    with pytest.raises(RelationHistorySnapshotError, match="entity topology"):
        storage.relation_history_status("alice", deletion_boundary)


@pytest.mark.parametrize("corrupt_kind", ["deleted", "merged"])
def test_known_at_refuses_current_topology_not_recorded_in_entity_versions(
    storage,
    corrupt_kind: str,
) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    relation_id = _relation(storage, "alice", root, target)
    revision = storage.execute(
        "SELECT recorded_at FROM relation_revisions WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1",
        (relation_id,),
    ).fetchone()
    assert revision is not None
    with storage.transaction() as connection:
        if corrupt_kind == "deleted":
            connection.execute(
                "UPDATE entities SET deleted_at=? WHERE id=? AND user_id=?",
                ("2025-01-01T00:00:00Z", root, "alice"),
            )
        else:
            connection.execute(
                "UPDATE entities SET canonical=0, merged_into_id=? WHERE id=? AND user_id=?",
                (target, root, "alice"),
            )

    with pytest.raises(RelationHistorySnapshotError, match="recorded history"):
        storage.get_entity_graph(
            "alice",
            root,
            1,
            known_at=str(revision["recorded_at"]),
        )


def test_invalid_as_of_is_rejected_before_missing_entity_reads_in_every_direct_wrapper(
    storage,
    monkeypatch,
) -> None:
    graph = KnowledgeGraph(storage)
    reads: list[str] = []

    def entity_must_not_run(*_args, **_kwargs):
        reads.append("entity")
        raise AssertionError("entity read ran before invalid as_of refusal")

    monkeypatch.setattr(storage, "get_entity", entity_must_not_run)
    with pytest.raises(ValueError, match="as_of"):
        storage.get_entity_graph("alice", "ent-missing", as_of="2024-99-99")
    with pytest.raises(ValueError, match="[Dd]ate|as_of"):
        graph.get_entity_relations("ent-missing", "alice", as_of="2024-99-99")
    assert reads == []


@pytest.mark.parametrize(
    "broken",
    [
        {"known_at": ""},
        {"known_at_floor": ""},
        {"known_at_floor": "2025-04-01T00:00:00.000000Z"},
        {"history_complete": 1},
        {"identity_basis": "historical_names"},
    ],
)
def test_query_context_rejects_incomplete_or_inexact_history_metadata_before_roots(
    storage,
    monkeypatch,
    broken: dict,
) -> None:
    graph = KnowledgeGraph(storage)
    status = {
        "known_at": "2025-03-04T03:07:08.000000Z",
        "known_at_floor": "2025-01-01T00:00:00.000000Z",
        "history_complete": True,
        "identity_basis": "current_names",
    }
    status.update(broken)
    roots: list[str] = []
    monkeypatch.setattr(storage, "relation_history_status", lambda *_args, **_kwargs: dict(status))

    def roots_must_not_run(*_args, **_kwargs):
        roots.append("root")
        return []

    monkeypatch.setattr(graph, "search_entities", roots_must_not_run)
    with pytest.raises(RelationHistorySnapshotError):
        graph.context_for_query("alice", "Альфа", known_at="2025-03-04T06:07:08+03:00")
    assert roots == []


def test_query_context_postflight_compares_the_full_history_tuple(storage, monkeypatch) -> None:
    graph = KnowledgeGraph(storage)
    calls = 0

    def changing_status(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "known_at": "2025-03-04T03:07:08.000000Z",
            "known_at_floor": (
                "2025-01-01T00:00:00.000000Z" if calls == 1 else "2025-02-01T00:00:00.000000Z"
            ),
            "history_complete": True,
            "identity_basis": "current_names",
        }

    monkeypatch.setattr(storage, "relation_history_status", changing_status)
    monkeypatch.setattr(graph, "search_entities", lambda *_args, **_kwargs: [])
    with pytest.raises(RelationHistorySnapshotError, match="status changed"):
        graph.context_for_query("alice", "нет", known_at="2025-03-04T06:07:08+03:00")
    assert calls == 2


def test_query_context_refuses_a_relation_revision_racing_between_hops(
    storage,
    monkeypatch,
) -> None:
    """A same-boundary higher event_seq must not create a mixed traversal."""

    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    bridge = _entity(storage, "alice", "Бета")
    target = _entity(storage, "alice", "Гамма")
    _relation(storage, "alice", root, bridge, relation_id="rel-watermark-first")
    second_id = _relation(storage, "alice", bridge, target, relation_id="rel-watermark-second")
    boundary_row = storage.execute(
        "SELECT MAX(recorded_at) AS boundary FROM relation_revisions WHERE user_id='alice'"
    ).fetchone()
    assert boundary_row and boundary_row["boundary"]
    boundary = str(boundary_row["boundary"])
    original = knowledge_graph_module._historical_entity_relations
    mutated = False

    def mutate_after_first_hop(*args, **kwargs):
        nonlocal mutated
        rows = original(*args, **kwargs)
        if not mutated:
            mutated = True
            with storage.transaction() as connection:
                connection.execute(
                    "UPDATE relation_revision_context SET recorded_at=? WHERE singleton=1",
                    (boundary,),
                )
                connection.execute(
                    "UPDATE relations SET weight=0.2 WHERE id=? AND user_id=?",
                    (second_id, "alice"),
                )
        return rows

    monkeypatch.setattr(
        knowledge_graph_module,
        "_historical_entity_relations",
        mutate_after_first_hop,
    )
    with pytest.raises(RelationHistorySnapshotError, match="relation history changed"):
        KnowledgeGraph(storage).context_for_query(
            "alice",
            "Альфа",
            depth=2,
            known_at=boundary,
        )
    assert mutated is True


@pytest.mark.parametrize("entrypoint", ["overview", "entity_graph"])
def test_storage_multiread_graphs_compare_the_relation_watermark(
    storage,
    monkeypatch,
    entrypoint: str,
) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    relation_id = _relation(storage, "alice", root, target)
    boundary_row = storage.execute(
        "SELECT recorded_at FROM relation_revisions WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1",
        (relation_id,),
    ).fetchone()
    assert boundary_row is not None
    values = iter((10, 11))
    monkeypatch.setattr(
        storage_graph_module,
        "_relation_revision_watermark",
        lambda *_args, **_kwargs: next(values),
    )

    with pytest.raises(RelationHistorySnapshotError, match="relation history changed"):
        if entrypoint == "overview":
            storage.graph_overview("alice", known_at=str(boundary_row["recorded_at"]))
        else:
            storage.get_entity_graph(
                "alice",
                root,
                1,
                known_at=str(boundary_row["recorded_at"]),
            )


def test_storage_graph_rejects_incomplete_history_status_before_entity_reads(
    storage,
    monkeypatch,
) -> None:
    entity_reads: list[str] = []
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda *_args, **_kwargs: {
            "known_at": "2025-03-04T03:07:08.000000Z",
            # Deliberately no completeness floor.
            "history_complete": True,
            "identity_basis": "current_names",
        },
    )

    def entity_must_not_run(*_args, **_kwargs):
        entity_reads.append("entity")
        raise AssertionError("entity lookup ran after incomplete history metadata")

    monkeypatch.setattr(storage, "get_entity", entity_must_not_run)
    with pytest.raises(RelationHistorySnapshotError, match="incomplete"):
        storage.get_entity_graph(
            "alice",
            "ent-missing",
            known_at="2025-03-04T06:07:08+03:00",
        )
    assert entity_reads == []


def test_public_entity_graph_has_a_total_edge_order_including_relation_id(
    storage,
    monkeypatch,
) -> None:
    """Alternating raw SQL order cannot make two identical HTTP payloads drift."""

    graph = KnowledgeGraph(storage)
    calls = 0
    nodes = [
        {"id": "ent-root", "name": "Корень", "entity_type": "project", "knowledge_count": 0},
        {"id": "ent-target", "name": "Цель", "entity_type": "thing", "knowledge_count": 0},
    ]
    edges = [
        {
            "id": "rel-z",
            "source_entity_id": "ent-root",
            "target_entity_id": "ent-target",
            "relation_type": "related_to",
            "weight": 1.0,
        },
        {
            "id": "rel-a",
            "source_entity_id": "ent-root",
            "target_entity_id": "ent-target",
            "relation_type": "related_to",
            "weight": 1.0,
        },
    ]

    def alternating_graph(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "root": "ent-root",
            "nodes": list(nodes),
            "edges": list(edges if calls % 2 else reversed(edges)),
            "as_of": "",
            "known_at": "",
            "temporal_basis": "valid_time",
        }

    monkeypatch.setattr(storage, "get_entity_graph", alternating_graph)
    first = graph.get_entity_graph("alice", "ent-root")
    second = graph.get_entity_graph("alice", "ent-root")
    assert [edge["id"] for edge in first["edges"]] == ["rel-a", "rel-z"]
    assert first == second


def test_idempotent_relation_create_never_returns_an_ignored_tombstone_id(storage) -> None:
    """RAISE(IGNORE) must resolve the live duplicate, not publish a phantom ID.

    Mutation kill: removing the post-INSERT rowcount branch returns
    `rel-tombstoned` even though that row is absent.
    """

    storage.ensure_user("alice")
    source = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    tombstoned = Relation(
        id="rel-tombstoned",
        user_id="alice",
        source_entity_id=source,
        target_entity_id=target,
        relation_type="related_to",
        weight=0.1,
        metadata_json={"private": "caller-value-must-not-be-returned"},
    )
    storage.create_relation(tombstoned)
    with storage.transaction() as connection:
        connection.execute(
            "DELETE FROM relations WHERE id=? AND user_id=?",
            (tombstoned.id, "alice"),
        )
    live = storage.create_relation(
        Relation(
            id="rel-live-duplicate",
            user_id="alice",
            source_entity_id=source,
            target_entity_id=target,
            relation_type="related_to",
            weight=0.9,
            metadata_json={"origin": "persisted"},
        )
    )

    resolved = storage.create_relation(tombstoned)

    assert resolved.id == live.id == "rel-live-duplicate"
    assert resolved.weight == live.weight == 0.9
    assert resolved.metadata_json == live.metadata_json == {"origin": "persisted"}
    assert resolved.created_at == live.created_at
    assert getattr(resolved, "_idempotent_replay") is True  # noqa: B009
    assert storage.execute("SELECT 1 FROM relations WHERE id='rel-tombstoned'").fetchone() is None
    assert storage.execute("SELECT 1 FROM relations WHERE id='rel-live-duplicate'").fetchone()


def test_current_relation_join_never_publishes_a_foreign_tenant_endpoint(storage) -> None:
    """A corrupt/imported relation row cannot turn another tenant's name public.

    Mutation kill: dropping either `entity.user_id = relation.user_id` join
    condition publishes the synthetic foreign endpoint and this test turns red.
    """

    storage.ensure_user("alice")
    storage.ensure_user("bob")
    alice = _entity(storage, "alice", "Альфа")
    bob = _entity(storage, "bob", "SYNTHETIC_FOREIGN_ENDPOINT_SENTINEL")
    corrupt = Relation(
        id="rel-cross-tenant-corrupt",
        user_id="alice",
        source_entity_id=alice,
        target_entity_id=bob,
        relation_type="related_to",
    )
    # Product writers reject this shape. Direct SQL models an old/imported or
    # externally corrupted row and verifies the read boundary remains defensive.
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                   relation_type, weight, metadata_json, created_at, deleted_at,
                   valid_from, valid_to, invalidated_at, superseded_by)
               VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                   :relation_type, :weight, :metadata_json, :created_at, :deleted_at,
                   :valid_from, :valid_to, :invalidated_at, :superseded_by)""",
            corrupt.to_row(),
        )

    public = KnowledgeGraph(storage).get_entity_relations(alice, "alice")
    overview = storage.graph_overview("alice", as_of="2024-01-01")

    assert public == []
    assert overview["nodes"] == []
    assert overview["nodes_matched_at_least"] == 0
    assert "SYNTHETIC_FOREIGN_ENDPOINT_SENTINEL" not in json.dumps(public, ensure_ascii=False)


def test_current_graph_overview_never_publishes_a_noncanonical_node(storage) -> None:
    storage.ensure_user("alice")
    entity_id = _entity(storage, "alice", "Неканонический узел")
    knowledge_id = _knowledge(storage, "alice", "Привязанный документ")
    storage.link_knowledge_entity("alice", knowledge_id, entity_id, status="accepted")
    with storage.transaction() as connection:
        connection.execute(
            """UPDATE entities SET canonical=0, merged_into_id=NULL
                 WHERE id=? AND user_id=?""",
            (entity_id, "alice"),
        )

    overview = storage.graph_overview("alice")

    assert overview["nodes"] == []
    assert overview["nodes_matched_at_least"] == 0


def test_selected_entity_created_after_known_at_is_refused_without_invalidating_others(storage) -> None:
    storage.ensure_user("alice")
    existing = _entity(storage, "alice", "Существовал раньше")
    witness = _entity(storage, "alice", "Свидетель")
    relation_id = _relation(storage, "alice", existing, witness)
    revision = storage.execute(
        "SELECT recorded_at FROM relation_revisions WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1",
        (relation_id,),
    ).fetchone()
    assert revision is not None
    boundary = str(revision["recorded_at"])
    late = _entity(storage, "alice", "Появился позже")

    # An unrelated later entity does not globally poison the valid old root.
    old_graph = storage.get_entity_graph("alice", existing, 1, known_at=boundary)
    assert old_graph["root"] == existing
    with pytest.raises(RelationHistorySnapshotError, match="recorded existence"):
        storage.get_entity_graph("alice", late, 1, known_at=boundary)
    with pytest.raises(RelationHistorySnapshotError, match="recorded existence"):
        KnowledgeGraph(storage).context_for_query(
            "alice",
            "Появился позже",
            known_at=boundary,
        )
    assert storage.soft_delete_entity(late, "alice") is True
    assert storage.get_entity_graph("alice", existing, 1, known_at=boundary)["root"] == existing

    late_merge = _entity(storage, "alice", "Поздний дубль")
    storage.merge_entities("alice", late_merge, existing, merged_by="alice")
    assert storage.get_entity_graph("alice", existing, 1, known_at=boundary)["root"] == existing


def test_entity_version_uses_the_exact_outer_graph_batch_timestamp(storage) -> None:
    """Identity and relation history from one atomic batch share one boundary."""

    storage.ensure_user("alice")
    with storage.transaction() as connection:
        context = connection.execute(
            "SELECT recorded_at FROM relation_revision_context WHERE singleton=1"
        ).fetchone()
        assert context and context["recorded_at"]
        boundary = str(context["recorded_at"])
        entity_id = _entity(storage, "alice", "Альфа")
        version = connection.execute(
            """SELECT created_at FROM entity_versions
                 WHERE user_id=? AND entity_id=? ORDER BY version, id LIMIT 1""",
            ("alice", entity_id),
        ).fetchone()

    assert version is not None
    assert version["created_at"] == boundary


def test_entity_existence_uses_logical_first_version_when_legacy_clock_rewinds(storage) -> None:
    storage.ensure_user("alice")
    entity_id = _entity(storage, "alice", "Альфа")
    first = storage.execute(
        """SELECT snapshot_json FROM entity_versions
             WHERE user_id=? AND entity_id=? AND version=1""",
        ("alice", entity_id),
    ).fetchone()
    assert first is not None
    with storage.transaction() as connection:
        connection.execute(
            """UPDATE entity_versions SET created_at=?
                 WHERE user_id=? AND entity_id=? AND version=1""",
            ("2025-01-02T00:00:00.000000Z", "alice", entity_id),
        )
        connection.execute(
            """INSERT INTO entity_versions
                 (id, user_id, entity_id, version, snapshot_json, created_at)
                 VALUES(?, ?, ?, ?, ?, ?)""",
            (
                "entv-rewound-clock",
                "alice",
                entity_id,
                2,
                first["snapshot_json"],
                "2025-01-01T00:00:00.000000Z",
            ),
        )

    with pytest.raises(RelationHistorySnapshotError, match="recorded existence"):
        storage_graph_module._assert_entities_existed_at_boundary(
            storage,
            "alice",
            [entity_id],
            "2025-01-01T12:00:00.000000Z",
        )


def test_same_batch_relation_is_a_causal_existence_witness(storage, monkeypatch) -> None:
    """Legacy slow entity stamps cannot outrank their atomic relation batch."""

    storage.ensure_user("alice")
    with storage.transaction() as connection:
        context = connection.execute(
            "SELECT recorded_at FROM relation_revision_context WHERE singleton=1"
        ).fetchone()
        assert context and context["recorded_at"]
        boundary = str(context["recorded_at"])
        monkeypatch.setattr(
            storage_graph_module,
            "_relation_batch_timestamp",
            lambda _connection: "2099-01-01T00:00:00.000000Z",
        )
        source = _entity(storage, "alice", "Альфа")
        target = _entity(storage, "alice", "Бета")
        relation_id = _relation(storage, "alice", source, target)

    rows = storage.get_entity_relations(source, "alice", known_at=boundary)
    assert [row["id"] for row in rows] == [relation_id]


def test_quarantined_topology_and_revisions_cannot_poison_a_public_known_at_snapshot(storage) -> None:
    storage.ensure_user("alice")
    public_source = _entity(storage, "alice", "Публичный источник")
    public_target = _entity(storage, "alice", "Публичная цель")
    public_relation = _relation(
        storage,
        "alice",
        public_source,
        public_target,
        relation_id="rel-public-known-at-control",
    )
    public_revision = storage.execute(
        """SELECT event_seq FROM relation_revisions
             WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1""",
        (public_relation,),
    ).fetchone()
    assert public_revision is not None

    hidden = _entity(storage, "alice", "PRIVATE KNOWN AT SENTINEL")
    _relation(
        storage,
        "alice",
        public_source,
        hidden,
        relation_id="rel-private-known-at-poison",
    )
    hidden_revision = storage.execute(
        """SELECT recorded_at FROM relation_revisions
             WHERE relation_id='rel-private-known-at-poison'
             ORDER BY event_seq DESC LIMIT 1"""
    ).fetchone()
    assert hidden_revision is not None
    boundary = str(hidden_revision["recorded_at"])
    with storage.transaction() as conn:
        # Corrupt the legacy snapshot before quarantine. Once privacy authority
        # is live, authenticated history is intentionally immutable.
        conn.execute(
            "UPDATE entity_versions SET snapshot_json=? WHERE entity_id=?",
            ("PRIVATE MALFORMED SNAPSHOT SENTINEL", hidden),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden, "person-alice", "2026-08-05T00:00:00Z"),
        )

    assert storage_graph_module._relation_revision_watermark(storage, "alice", boundary) == int(
        public_revision["event_seq"]
    )
    graph = storage.get_entity_graph("alice", public_source, 1, known_at=boundary)
    assert {str(edge["id"]) for edge in graph["edges"]} == {public_relation}
    encoded = json.dumps(graph, ensure_ascii=False)
    assert hidden not in encoded
    assert "PRIVATE KNOWN AT SENTINEL" not in encoded
    assert "PRIVATE MALFORMED SNAPSHOT SENTINEL" not in encoded


def test_relation_create_revalidates_live_endpoints_inside_the_write_transaction(
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice")
    source = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    original_get_entity = storage.get_entity
    deleted = False

    def delete_after_validation(entity_id: str, user_id: str | None = None):
        nonlocal deleted
        row = original_get_entity(entity_id, user_id)
        if entity_id == target and not deleted:
            deleted = True
            assert storage.soft_delete_entity(target, "alice") is True
        return row

    monkeypatch.setattr(storage, "get_entity", delete_after_validation)
    with pytest.raises(ValueError, match="Both entities"):
        storage.create_relation(
            Relation(
                "rel-racing-tombstone",
                "alice",
                source,
                target,
                "related_to",
            )
        )
    assert storage.execute("SELECT 1 FROM relations WHERE id='rel-racing-tombstone'").fetchone() is None


def test_direct_relations_hide_deleted_roots_and_endpoints_current_and_historical(
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice")
    source = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    relation_id = _relation(storage, "alice", source, target)
    revision = storage.execute(
        "SELECT recorded_at FROM relation_revisions WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1",
        (relation_id,),
    ).fetchone()
    assert revision is not None
    boundary = str(revision["recorded_at"])
    status = storage.relation_history_status("alice")
    assert storage.soft_delete_entity(target, "alice") is True
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda *_args, **_kwargs: {
            **status,
            "known_at": boundary,
            "known_at_floor": min(str(status["known_at_floor"]), boundary),
        },
    )
    graph = KnowledgeGraph(storage)

    assert graph.get_entity_relations(source, "alice") == []
    assert graph.get_entity_relations(source, "alice", known_at=boundary) == []
    assert storage.soft_delete_entity(source, "alice") is True
    assert graph.get_entity_relations(source, "alice") == []


def test_bounded_entity_graph_keeps_a_connected_rooted_prefix(storage, monkeypatch) -> None:
    root = "ent-root"
    bridge = "ent-bridge"
    nodes = [
        {"id": root, "name": "Корень", "entity_type": "project"},
        {"id": bridge, "name": "Мост", "entity_type": "thing"},
        *[
            {"id": f"ent-leaf-{index:04d}", "name": f"Лист {index:04d}", "entity_type": "thing"}
            for index in range(801)
        ],
    ]
    edges = [
        {
            "id": "rel-root-bridge",
            "source_entity_id": root,
            "target_entity_id": bridge,
            "relation_type": "related_to",
            "weight": 0.01,
        },
        *[
            {
                "id": f"rel-leaf-{index:04d}",
                "source_entity_id": bridge,
                "target_entity_id": f"ent-leaf-{index:04d}",
                "relation_type": "related_to",
                "weight": 1.0,
            }
            for index in range(801)
        ],
    ]
    monkeypatch.setattr(
        storage,
        "get_entity_graph",
        lambda *_args, **_kwargs: {
            "root": root,
            "nodes": nodes,
            "edges": edges,
            "as_of": "",
            "known_at": "",
            "temporal_basis": "valid_time",
        },
    )

    result = KnowledgeGraph(storage).get_entity_graph("alice", root, depth=2)
    assert len(result["edges"]) == 800
    assert result["edges_matched_at_least"] == 802
    assert result["edges_truncated"] is True
    assert "rel-root-bridge" in {edge["id"] for edge in result["edges"]}
    reachable = {root}
    pending = list(result["edges"])
    while pending:
        progressed = False
        for edge in list(pending):
            source_id = str(edge["source_entity_id"])
            target_id = str(edge["target_entity_id"])
            if source_id in reachable or target_id in reachable:
                reachable.update((source_id, target_id))
                pending.remove(edge)
                progressed = True
        assert progressed, "published entity graph contains a disconnected component"
    assert {node["id"] for node in result["nodes"]} <= reachable


def test_storage_neighbourhood_caps_relation_materialization_before_public_projection(
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice")
    with storage.transaction():
        root = _entity(storage, "alice", "Корень")
        for index in range(802):
            target = _entity(storage, "alice", f"Широкий сосед {index:04d}")
            _relation(
                storage,
                "alice",
                root,
                target,
                relation_id=f"rel-wide-{index:04d}",
            )

    observed_limits: list[int | None] = []
    original = storage_graph_module._current_entity_relations_for_traversal

    def bounded_page(*args, **kwargs):
        observed_limits.append(kwargs.get("row_limit"))
        rows = original(*args, **kwargs)
        assert len(rows) <= 802
        return rows

    monkeypatch.setattr(
        storage_graph_module,
        "_current_entity_relations_for_traversal",
        bounded_page,
    )

    raw = storage.get_entity_graph("alice", root, depth=1)
    public = KnowledgeGraph(storage).get_entity_graph("alice", root, depth=1)

    assert observed_limits and set(observed_limits) == {802}
    assert len(raw["edges"]) == 801
    assert raw["edges_matched_at_least"] == 802
    assert raw["edges_truncated"] is True
    assert raw["nodes_matched_at_least"] == 803
    assert raw["nodes_truncated"] is True
    assert len(public["edges"]) == 800
    assert public["edges_matched_at_least"] == 802
    assert public["edges_truncated"] is True


def test_known_at_never_uses_present_day_cooccurrence(storage, monkeypatch) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    neighbour = _entity(storage, "alice", "Бета")
    shared = _knowledge(storage, "alice", "Оба имени встретились сейчас")
    for entity_id in (alpha, neighbour):
        storage.link_knowledge_entity("alice", shared, entity_id, status="accepted")

    normalized = "2025-03-04T03:07:08.000000Z"
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": {
            "known_at": normalized,
            "known_at_floor": "2025-01-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        },
    )
    graph = KnowledgeGraph(storage)
    monkeypatch.setattr(
        knowledge_graph_module,
        "_assert_entities_existed_at_boundary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        knowledge_graph_module,
        "_historical_entity_relations",
        lambda *_args, **_kwargs: [],
    )
    context = graph.context_for_query(
        "alice",
        "Альфа",
        depth=1,
        known_at="2025-03-04T03:07:08Z",
    )

    assert context["paths"] == []
    assert not any(relation.get("implicit") for relation in context["relations"])
    assert context["known_at"] == normalized


@pytest.mark.parametrize("query", ["Альфа", "Заведомо отсутствующий корень"])
def test_known_at_postflight_refuses_merge_racing_current_identity_reads(
    storage,
    monkeypatch,
    query: str,
) -> None:
    """Both normal and empty-result paths recheck after current name reads."""

    storage.ensure_user("alice")
    _entity(storage, "alice", "Альфа")
    calls: list[str] = []

    def status(_user_id: str, *, known_at: str = "") -> dict:
        calls.append(known_at)
        if len(calls) == 2:
            raise RelationHistorySnapshotError("known_at crosses concurrent merge")
        return {
            "known_at": "2025-03-04T03:07:08.000000Z",
            "known_at_floor": "2025-01-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        }

    monkeypatch.setattr(storage, "relation_history_status", status)
    graph = KnowledgeGraph(storage)
    monkeypatch.setattr(
        knowledge_graph_module,
        "_assert_entities_existed_at_boundary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        knowledge_graph_module,
        "_historical_entity_relations",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(RelationHistorySnapshotError, match="merge"):
        graph.context_for_query(
            "alice",
            query,
            known_at="2025-03-04T06:07:08+03:00",
        )

    assert calls == [
        "2025-03-04T06:07:08+03:00",
        "2025-03-04T03:07:08.000000Z",
    ]


def test_current_context_does_not_require_relation_history_preflight(storage, monkeypatch) -> None:
    """Default current retrieval remains independent of the schema-31 floor."""

    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")

    def history_must_not_run(*_args, **_kwargs):
        raise AssertionError("current graph unexpectedly read relation history status")

    monkeypatch.setattr(storage, "relation_history_status", history_must_not_run)
    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=0)

    assert [item["id"] for item in context["roots"]] == [alpha]
    assert context["known_at"] == ""
    assert context["temporal_basis"] == "valid_time"


def test_query_context_caps_relation_width_before_building_the_raw_snapshot(
    storage,
    monkeypatch,
) -> None:
    root_id = "ent-root"
    graph = KnowledgeGraph(storage)
    monkeypatch.setattr(
        graph,
        "search_entities",
        lambda *_args, **_kwargs: [
            {
                "id": root_id,
                "name": "Корень",
                "entity_type": "project",
                "_match_score": 1.0,
                "_match_method": "exact",
            }
        ],
    )

    def entity(_storage, entity_id: str, _user_id: str) -> dict:
        return {
            "id": entity_id,
            "name": "Корень" if entity_id == root_id else entity_id,
            "entity_type": "thing",
            "canonical": 1,
            "merged_into_id": None,
            "deleted_at": None,
        }

    monkeypatch.setattr(knowledge_graph_module, "_graph_entity_for_traversal", entity)
    observed_limits: list[int | None] = []

    def wide_relations(
        _storage,
        entity_id: str,
        user_id: str,
        *,
        as_of: str,
        row_limit: int | None = None,
        **_kwargs,
    ) -> list[dict]:
        del user_id, as_of
        observed_limits.append(row_limit)
        if entity_id != root_id:
            return []
        return [
            {
                "id": f"rel-context-{index:04d}",
                "user_id": "alice",
                "source_entity_id": root_id,
                "target_entity_id": f"ent-target-{index:04d}",
                "source_name": "Корень",
                "target_name": f"Цель {index:04d}",
                "relation_type": "related_to",
                "weight": 1.0,
                "metadata_json": "{}",
                "created_at": "2026-01-01T00:00:00Z",
                "valid_from": "",
                "valid_to": None,
            }
            for index in range(513)
        ]

    monkeypatch.setattr(
        knowledge_graph_module,
        "_current_entity_relations_for_traversal",
        wide_relations,
    )

    context = graph.context_for_query("alice", "Корень", depth=2)

    assert observed_limits == [513]
    assert len(context["relations"]) == 512
    assert len(context["paths"]) == 10
    assert context["paths_matched_at_least"] >= 10
    assert context["paths_truncated"] is True


def test_one_best_state_owns_score_edges_and_candidate_path_id(storage) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    strong_bridge = _entity(storage, "alice", "Сильный мост")
    weak_bridge = _entity(storage, "alice", "Слабый мост")
    target = _entity(storage, "alice", "Цель")
    target_ko = _knowledge(storage, "alice", "Документ цели")
    storage.link_knowledge_entity("alice", target_ko, target, status="accepted")

    # IDs deliberately put the weak road first in a lexical traversal.  A queue
    # implementation that keeps score and path in separate mutable maps can then
    # splice the strong score onto the weak road.
    _relation(storage, "alice", alpha, weak_bridge, relation_id="rel-00-weak", weight=0.95)
    _relation(storage, "alice", weak_bridge, target, relation_id="rel-01-weak", weight=1.0)
    _relation(storage, "alice", alpha, strong_bridge, relation_id="rel-10-strong", weight=1.0)
    _relation(storage, "alice", strong_bridge, target, relation_id="rel-11-strong", weight=1.0)

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=2, as_of="2024-01-01")
    path = _path_to(context, target)
    assert path["entity_ids"] == [alpha, strong_bridge, target]
    assert [edge["id"] for edge in path["edges"]] == ["rel-10-strong", "rel-11-strong"]
    assert len(path["entity_ids"]) == len(set(path["entity_ids"])), "the published route is not simple"

    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == target_ko
    )
    evidence = next(item for item in candidate["evidence"] if item.get("path_id"))
    assert candidate["path_id"] == evidence["path_id"] == path["path_id"]
    assert evidence["entity_score"] == path["score"]


def test_candidate_never_borrows_a_weaker_entitys_path_id(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    neighbour = _entity(storage, "alice", "Бета")
    knowledge_id = _knowledge(storage, "alice", "Общее основание")
    storage.link_knowledge_entity("alice", knowledge_id, root, status="accepted")
    storage.link_knowledge_entity("alice", knowledge_id, neighbour, status="accepted")
    _relation(storage, "alice", root, neighbour)

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=1)
    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == knowledge_id
    )

    assert candidate["score"] > _path_to(context, neighbour)["score"]
    assert "path_id" not in candidate
    assert not any(item.get("path_id") for item in candidate["evidence"])


def test_each_candidate_uses_its_own_scoring_entity_path(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    root_knowledge = _knowledge(storage, "alice", "Карточка Альфы")
    target_knowledge = _knowledge(storage, "alice", "Карточка Беты")
    storage.link_knowledge_entity("alice", root_knowledge, root, status="accepted")
    storage.link_knowledge_entity("alice", target_knowledge, target, status="accepted")
    _relation(storage, "alice", root, target)

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=1)
    candidates = {item["knowledge_object_id"]: item for item in context["knowledge_candidates"]}
    target_path = _path_to(context, target)

    assert "path_id" not in candidates[root_knowledge]
    assert candidates[target_knowledge]["path_id"] == target_path["path_id"]
    target_evidence = next(item for item in candidates[target_knowledge]["evidence"] if item.get("path_id"))
    assert target_evidence["entity_id"] == target
    assert target_evidence["path_id"] == target_path["path_id"]


def test_rejected_grounded_offer_cannot_ground_a_seed_roots_best_path(storage) -> None:
    storage.ensure_user("alice")
    query_root = _entity(storage, "alice", "Альфа")
    seed_root = _entity(storage, "alice", "Бета")
    target = _entity(storage, "alice", "Гамма")
    seed_knowledge = _knowledge(storage, "alice", "Посев Беты")
    target_knowledge = _knowledge(storage, "alice", "Карточка Гаммы")
    storage.link_knowledge_entity("alice", seed_knowledge, seed_root, status="accepted")
    storage.link_knowledge_entity("alice", target_knowledge, target, status="accepted")
    _relation(storage, "alice", query_root, seed_root, relation_id="rel-query-to-seed")
    _relation(storage, "alice", seed_root, target, relation_id="rel-seed-to-target")

    context = KnowledgeGraph(storage).context_for_query(
        "alice",
        "Альфа",
        depth=2,
        seed_knowledge_ids=[seed_knowledge],
    )
    target_path = _path_to(context, target)
    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == target_knowledge
    )

    assert target_path["root"] == seed_root
    assert target_path["entity_ids"] == [seed_root, target]
    assert not any(path["root"] == query_root and path["target"] == target for path in context["paths"])
    assert candidate["path_id"] == target_path["path_id"]
    assert candidate["query_matched"] is False


def test_candidate_grounding_comes_from_the_entity_that_earned_its_score(storage) -> None:
    storage.ensure_user("alice")
    query_root = _entity(storage, "alice", "Альфа")
    grounded_neighbour = _entity(storage, "alice", "Дельта")
    seed_root = _entity(storage, "alice", "Посев")
    seed_knowledge = _knowledge(storage, "alice", "Основание посева")
    shared_document = _knowledge(storage, "alice", "Документ Посева и Дельты")
    storage.link_knowledge_entity("alice", seed_knowledge, seed_root, status="accepted")
    storage.link_knowledge_entity("alice", shared_document, seed_root, status="accepted")
    storage.link_knowledge_entity("alice", shared_document, grounded_neighbour, status="accepted")
    _relation(
        storage,
        "alice",
        query_root,
        grounded_neighbour,
        relation_id="rel-query-to-grounded",
    )

    context = KnowledgeGraph(storage).context_for_query(
        "alice",
        "Альфа",
        depth=1,
        seed_knowledge_ids=[seed_knowledge],
    )
    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == shared_document
    )
    evidence_by_entity = {item["entity_id"]: item for item in candidate["evidence"]}

    assert (
        evidence_by_entity[seed_root]["entity_score"] > evidence_by_entity[grounded_neighbour]["entity_score"]
    )
    assert candidate["score"] == evidence_by_entity[seed_root]["entity_score"]
    assert "path_id" not in candidate
    assert candidate["query_matched"] is False


def test_unreviewed_relation_metadata_cannot_forge_a_knowledge_anchor(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    knowledge_id = _knowledge(storage, "alice", "Существующий объект")
    with pytest.raises(ValueError, match="private knowledge"):
        _relation(
            storage,
            "alice",
            root,
            target,
            metadata={
                "origin": "api",
                "created_by": "alice",
                "source": "reviewed_relation_candidate",
                "candidate_id": "forged-candidate",
                "reviewed_by": "alice",
                "confidence": 1.0,
                "evidence": {"knowledge_object_id": knowledge_id},
            },
        )

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа")
    assert context["paths"] == []
    assert storage.execute("SELECT 1 FROM relations WHERE user_id='alice'").fetchone() is None


def test_paths_are_stably_capped_with_an_honest_count(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Центр")
    neighbours = [_entity(storage, "alice", f"Луч {index:02d}") for index in range(12)]
    for index, neighbour in enumerate(neighbours):
        _relation(
            storage,
            "alice",
            root,
            neighbour,
            relation_id=f"rel-star-{index:02d}",
        )

    graph = KnowledgeGraph(storage)
    first = graph.context_for_query("alice", "Центр", depth=1, as_of="2024-01-01")
    second = graph.context_for_query("alice", "Центр", depth=1, as_of="2024-01-01")

    assert len(first["paths"]) == 10
    assert first["paths_matched_at_least"] == 12
    assert first["paths_truncated"] is True
    assert first["paths"] == second["paths"]


def test_legacy_merged_endpoint_is_published_as_its_live_canonical_entity(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Начало")
    obsolete = _entity(storage, "alice", "Старое имя")
    canonical = _entity(storage, "alice", "Живое имя")
    relation_id = _relation(storage, "alice", root, obsolete)
    with storage.transaction() as connection:
        connection.execute(
            """UPDATE entities
               SET canonical=0, merged_into_id=?, deleted_at='2024-01-01T00:00:00Z'
               WHERE id=? AND user_id=?""",
            (canonical, obsolete, "alice"),
        )

    context = KnowledgeGraph(storage).context_for_query("alice", "Начало", depth=1)
    path = _path_to(context, canonical)
    assert obsolete not in path["entity_ids"]
    assert path["edges"] == [
        {
            "id": relation_id,
            "from": root,
            "to": canonical,
            "direction": "forward",
            "source": root,
            "target": canonical,
            "type": "related_to",
            "weight": 1.0,
            "implicit": False,
            "valid_from": "",
            "valid_to": None,
            "created_at": path["edges"][0]["created_at"],
            "invalidated_at": None,
            "superseded_by": None,
            "provenance": {"origin": "manual"},
        }
    ]
