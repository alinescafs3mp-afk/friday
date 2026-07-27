"""Near-duplicate Knowledge Object detection — §6.

Entities dedup; knowledge did not, so bulk imports accumulate near-identical
records. Pins the pure pairwise-cosine detector, the store-as-conflict flow
(reusing the near_duplicate type + §5 resolve), the resolve→deprecate loop
end-to-end, exclusion of near-duplicates from the agent's reasoning context,
and the on-demand admin endpoint.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from jericho.dedup import NEAR_DUPLICATE_TYPE, detect_near_duplicates, find_near_duplicate_pairs
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.retrieval import pack_vector
from jericho.server import create_app
from jericho.storage.models import KnowledgeObject, RawObject, new_id

# --- pure detector --------------------------------------------------------


def test_find_near_duplicate_pairs_thresholds_and_canonicalises():
    vectors = [
        ("b", [1.0, 0.0, 0.0]),
        ("a", [0.99, 0.14, 0.0]),  # ~0.99 cosine with b
        ("c", [0.0, 1.0, 0.0]),  # orthogonal to b
        ("z", [0.0, 0.0, 0.0]),  # zero vector -> skipped
    ]
    pairs = find_near_duplicate_pairs(vectors, threshold=0.9)
    assert len(pairs) == 1
    id_a, id_b, score = pairs[0]
    assert (id_a, id_b) == ("a", "b")  # canonical order, lexicographic
    assert score >= 0.9
    # A high threshold rejects the near-but-not-identical pair.
    assert find_near_duplicate_pairs(vectors, threshold=0.999) == []


# --- storage integration --------------------------------------------------


def _store(storage, user_id: str, content: str, title: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
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
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _index(storage, user_id: str, ko_id: str, vector: list[float], model: str) -> None:
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": ko_id,
                "user_id": user_id,
                "model": model,
                "dim": len(vector),
                "source_version": 1,
                "content_hash": "h",
                "vector": pack_vector(vector),
            }
        ]
    )


def _dedup_settings(settings):
    from dataclasses import replace

    return replace(settings, embeddings_model="test-embed", dedup_threshold=0.9)


def test_detect_stores_near_duplicate_conflicts(settings, storage):
    cfg = _dedup_settings(settings)
    storage.ensure_user("alice")
    dup_a = _store(storage, "alice", "Купить молоко и хлеб.", "Список A")
    dup_b = _store(storage, "alice", "Купить молоко, хлеб.", "Список B")
    other = _store(storage, "alice", "Позвонить врачу в среду.", "Врач")
    _index(storage, "alice", dup_a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", dup_b, [0.98, 0.2, 0.0], "test-embed")
    _index(storage, "alice", other, [0.0, 0.0, 1.0], "test-embed")

    result = _run(storage, cfg)
    assert result["detected"] == 1
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == NEAR_DUPLICATE_TYPE
    assert {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]} == {dup_a, dup_b}
    assert other not in {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]}

    # Re-running is idempotent (ON CONFLICT upsert on the pair), not duplicating.
    _run(storage, cfg)
    assert len(storage.list_knowledge_conflicts("alice", status="suggested")) == 1

    # And the §5 resolve action deduplicates: keep A, deprecate the near-copy.
    resolved = storage.resolve_conflict("alice", conflicts[0]["id"], dup_a, reviewed_by="alice")
    assert resolved["deprecated_id"] == dup_b
    assert storage.get_knowledge_object(dup_b, "alice")["lifecycle_stage"] == "deprecated"


def test_detect_no_model_is_a_clean_noop(settings, storage):
    storage.ensure_user("alice")
    result = detect_near_duplicates(storage, settings, "alice")  # embeddings_model = ""
    assert result == {"detected": 0, "reason": "embeddings model not configured"}


@pytest.mark.asyncio
async def test_near_duplicates_excluded_from_agent_context(settings, storage):
    import json

    from jericho.agent_runtime import AgentRuntime
    from jericho.permissions import ActorContext

    storage.ensure_user("alice")
    a = _store(storage, "alice", "Купить молоко и хлеб.", "Список A")
    b = _store(storage, "alice", "Купить молоко, хлеб.", "Список B")
    storage.store_knowledge_conflict("alice", a, b, conflict_type=NEAR_DUPLICATE_TYPE, confidence=0.97)

    class _Searcher:
        async def search(self, *a, **k):
            return {
                "results": [
                    {**storage.get_knowledge_object(a, "alice"), "_score": 0.9, "_entities": []},
                    {**storage.get_knowledge_object(b, "alice"), "_score": 0.85, "_entities": []},
                ],
                "entity_matches": [],
            }

    class _LLM:
        enabled = True
        model = "x"

        def __init__(self):
            self.payload = None

        async def chat(self, messages, **kwargs):
            for item in messages:
                content = str(item.get("content") or "")
                if "JERICHO_CONTEXT_DATA" in content and "{" in content:
                    self.payload = json.loads(content[content.index("{") :])
            return {"content": "ок"}

    llm = _LLM()
    await AgentRuntime(settings, storage, llm=llm).chat(
        "alice",
        "что купить?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
        hybrid_searcher=_Searcher(),
    )
    # No knowledge object in the prompt carries a near_duplicate "conflict".
    for obj in llm.payload["knowledge_objects"]:
        assert "conflict" not in obj


def test_detect_duplicates_endpoint(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        # Default settings have no embeddings model -> clean, gated no-op.
        response = client.post(
            "/api/admin/knowledge/detect-duplicates",
            json={"user_id": LEGACY_OWNER_USER_ID},
            headers=owner,
        )
        assert response.status_code == 200, response.text
        assert response.json()["reason"] == "embeddings model not configured"
        actions = [row["action"] for row in app.state.storage.list_audit_log(None, limit=20)]
        assert "admin.knowledge.detect_duplicates" in actions
        assert client.post("/api/admin/knowledge/detect-duplicates", json={}).status_code == 401


# --- the blind spot -------------------------------------------------------


def test_duplicate_of_an_out_of_window_object_is_detected(settings, storage):
    """A new object must be compared against the WHOLE corpus, not just the newest N.

    Before 0.42 the detector read only the newest ``dedup_max_objects`` vectors and
    did all-pairs inside that window, so an older object was never a comparison
    partner and a duplicate of it arriving later was invisible — permanently.
    """
    from dataclasses import replace

    cfg = replace(_dedup_settings(settings), dedup_scan_batch=1)
    storage.ensure_user("alice")
    old = _store(storage, "alice", "Купить молоко и хлеб.", "Старый список")
    _index(storage, "alice", old, [1.0, 0.0, 0.0], "test-embed")
    # Mutually dissimilar filler: identical vectors here would be genuine duplicates
    # of each other, and a full-corpus scan would rightly report them too.
    for index, vector in enumerate(([0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.7, 0.71])):
        noise = _store(storage, "alice", f"Позвонить врачу {index}.", f"Врач {index}")
        _index(storage, "alice", noise, vector, "test-embed")
    fresh = _store(storage, "alice", "Купить молоко, хлеб.", "Новый список")
    _index(storage, "alice", fresh, [0.98, 0.2, 0.0], "test-embed")

    _run(storage, cfg)
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert len(conflicts) == 1
    assert {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]} == {old, fresh}


# --- incremental scan mechanics -------------------------------------------


_CLOSED = [1000]


def _close_second(storage) -> None:
    """Move every freshly written vector into a CLOSED second.

    The scanner ignores the second it starts in: those rows may not all be written
    yet, and second-precision timestamps make an exact cursor unsafe across that
    boundary. In production that costs one tick of a six-hourly job; in tests, which
    write and scan inside one second, it would mean nothing is ever scanned. So tests
    state "time has moved on" explicitly instead of sleeping — and each call lands in
    a LATER second than the previous one, preserving write order.
    """
    stamp = f"{_CLOSED[0]}-01-01T00:00:00+00:00"
    _CLOSED[0] += 1
    # Only REAL (21st-century) stamps are moved. Matching anything else would also
    # catch a stamp a test deliberately placed in the past, undoing its setup.
    storage.execute("UPDATE knowledge_embeddings SET updated_at=? WHERE updated_at LIKE '2%'", (stamp,))


def _run(storage, cfg, user_id="alice", *, close=True, **kwargs):
    if close:
        _close_second(storage)
    return detect_near_duplicates(storage, cfg, user_id, **kwargs)


def test_second_run_with_no_changes_compares_nothing(settings, storage):
    cfg = _dedup_settings(settings)
    storage.ensure_user("alice")
    for index, vector in enumerate(([1.0, 0.0, 0.0], [0.98, 0.2, 0.0], [0.0, 0.0, 1.0])):
        _index(
            storage, "alice", _store(storage, "alice", f"Текст {index}", f"T{index}"), vector, "test-embed"
        )

    first = _run(storage, cfg)
    assert first["detected"] == 1
    assert first["objects_compared"] == 3

    second = _run(storage, cfg)
    # Everything already swept: the corpus is walked once, not on every tick.
    assert second["objects_compared"] == 0
    assert second["candidate_pairs"] == 0
    assert second["pending"] == 0
    assert len(storage.list_knowledge_conflicts("alice", status="suggested")) == 1


def test_a_vector_written_in_the_same_second_is_not_skipped(settings, storage):
    """utc_now() is second-precision, so an exact watermark would let a row written
    later in the SAME second sort below it and be skipped forever."""
    cfg = _dedup_settings(settings)
    storage.ensure_user("alice")
    old = _store(storage, "alice", "Купить молоко и хлеб.", "Старый")
    _index(storage, "alice", old, [1.0, 0.0, 0.0], "test-embed")
    _run(storage, cfg)

    # Same second as the run above; its id may sort either side of the old one.
    fresh = _store(storage, "alice", "Купить молоко, хлеб.", "Новый")
    _index(storage, "alice", fresh, [0.98, 0.2, 0.0], "test-embed")
    result = _run(storage, cfg)

    assert result["detected"] == 1
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]} == {old, fresh}


def test_edited_object_is_recompared_after_reindex(settings, storage):
    cfg = _dedup_settings(settings)
    storage.ensure_user("alice")
    a = _store(storage, "alice", "Купить молоко.", "A")
    b = _store(storage, "alice", "Позвонить врачу.", "B")
    _index(storage, "alice", a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", b, [0.0, 1.0, 0.0], "test-embed")
    assert _run(storage, cfg)["detected"] == 0

    # B is re-embedded close to A; the new vector bumps knowledge_embeddings.updated_at.
    _index(storage, "alice", b, [0.98, 0.2, 0.0], "test-embed")
    assert _run(storage, cfg)["detected"] == 1


def test_backfill_walks_the_whole_corpus_across_ticks(settings, storage):
    """The cap stopped meaning 'silently miss' and started meaning 'catch up'."""
    from dataclasses import replace

    cfg = replace(_dedup_settings(settings), dedup_scan_batch=2)
    storage.ensure_user("alice")
    old_a = _store(storage, "alice", "Купить молоко и хлеб.", "Старый A")
    old_b = _store(storage, "alice", "Купить молоко, хлеб.", "Старый B")
    _index(storage, "alice", old_a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", old_b, [0.98, 0.2, 0.0], "test-embed")
    for index, vector in enumerate(([0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.7, 0.71])):
        _index(
            storage, "alice", _store(storage, "alice", f"Прочее {index}", f"P{index}"), vector, "test-embed"
        )

    seen_modes = set()
    pending = None
    for _ in range(6):
        result = _run(storage, cfg)
        seen_modes.add(result["mode"])
        if pending is not None:
            assert result["pending"] <= pending  # monotonically paid off
        pending = result["pending"]
        if pending == 0 and result["objects_compared"] == 0:
            break
    assert pending == 0
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert len(conflicts) == 1
    assert {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]} == {old_a, old_b}


def test_dismissed_conflict_is_not_resurrected(settings, storage):
    cfg = _dedup_settings(settings)
    storage.ensure_user("alice")
    a = _store(storage, "alice", "Купить молоко и хлеб.", "A")
    b = _store(storage, "alice", "Купить молоко, хлеб.", "B")
    _index(storage, "alice", a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", b, [0.98, 0.2, 0.0], "test-embed")
    _run(storage, cfg)
    conflict = storage.list_knowledge_conflicts("alice", status="suggested")[0]
    storage.review_knowledge_conflict("alice", conflict["id"], "dismissed", reviewed_by="alice")

    _index(storage, "alice", b, [0.99, 0.1, 0.0], "test-embed")  # re-embedded, still a dup
    result = _run(storage, cfg)
    assert storage.list_knowledge_conflicts("alice", status="suggested") == []
    assert storage.get_knowledge_conflict("alice", conflict["id"])["status"] == "dismissed"
    assert result["suppressed"] >= 1


def test_confirmed_conflict_is_not_suppressed(settings, storage):
    cfg = _dedup_settings(settings)
    storage.ensure_user("alice")
    a = _store(storage, "alice", "Купить молоко и хлеб.", "A")
    b = _store(storage, "alice", "Купить молоко, хлеб.", "B")
    _index(storage, "alice", a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", b, [0.98, 0.2, 0.0], "test-embed")
    _run(storage, cfg)
    conflict = storage.list_knowledge_conflicts("alice", status="suggested")[0]
    storage.review_knowledge_conflict("alice", conflict["id"], "confirmed", reviewed_by="alice")

    _index(storage, "alice", b, [0.99, 0.1, 0.0], "test-embed")
    _run(storage, cfg)
    after = storage.get_knowledge_conflict("alice", conflict["id"])
    assert after["status"] == "confirmed"  # re-touching only raises confidence
    assert after["confidence"] >= conflict["confidence"]


def test_scan_state_survives_a_shrinking_corpus(settings, storage):
    """The cursor is a VALUE, not a row reference: purging what it names is fine."""
    from dataclasses import replace

    cfg = replace(_dedup_settings(settings), dedup_scan_batch=1)
    storage.ensure_user("alice")
    ids = []
    for index, vector in enumerate(([0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.98, 0.2, 0.0])):
        ko = _store(storage, "alice", f"Текст {index}", f"T{index}")
        _index(storage, "alice", ko, vector, "test-embed")
        ids.append(ko)
    _run(storage, cfg)

    storage.soft_delete_knowledge_object(ids[0], "alice")
    storage.purge_knowledge_object(ids[0], "alice")
    for _ in range(6):
        result = _run(storage, cfg)
        if result["pending"] == 0 and result["objects_compared"] == 0:
            break
    assert result["pending"] == 0
    assert len(storage.list_knowledge_conflicts("alice", status="suggested")) == 1


def test_deadline_stops_without_losing_progress(settings, storage):
    from dataclasses import replace

    cfg = replace(_dedup_settings(settings), dedup_scan_batch=1)
    storage.ensure_user("alice")
    for index, vector in enumerate(([1.0, 0.0, 0.0], [0.98, 0.2, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])):
        _index(
            storage, "alice", _store(storage, "alice", f"Текст {index}", f"T{index}"), vector, "test-embed"
        )

    starved = _run(storage, cfg, max_seconds=0.0)
    # At least one tile always runs: a zero budget must not livelock.
    assert starved["objects_compared"] >= 1
    assert starved["incomplete"] is True

    for _ in range(8):
        result = _run(storage, cfg)
        if result["pending"] == 0 and result["objects_compared"] == 0:
            break
    assert result["pending"] == 0
    assert len(storage.list_knowledge_conflicts("alice", status="suggested")) == 1


def test_threshold_decrease_rescans_but_increase_does_not(settings, storage):
    from dataclasses import replace

    high = replace(_dedup_settings(settings), dedup_threshold=0.99)
    storage.ensure_user("alice")
    a = _store(storage, "alice", "Купить молоко и хлеб.", "A")
    b = _store(storage, "alice", "Купить молоко, хлеб.", "B")
    _index(storage, "alice", a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", b, [0.98, 0.2, 0.0], "test-embed")  # ~0.98 cosine
    assert _run(storage, high)["detected"] == 0

    # Lowering the bar makes previously rejected pairs eligible while no vector moved.
    low = replace(_dedup_settings(settings), dedup_threshold=0.9)
    assert _run(storage, low)["detected"] == 1
    # Raising it again needs no rescan.
    assert _run(storage, high)["objects_compared"] == 0


def test_scan_state_roundtrip_and_corruption_recovery(settings, storage):
    from jericho.dedup import ScanState, load_scan_state, save_scan_state

    storage.ensure_user("alice")
    state = ScanState(model="m", threshold=0.9, watermark=("2026-01-01T00:00:00+00:00", "ko_x"))
    save_scan_state(storage, "alice", state)
    assert load_scan_state(storage, "alice") == state

    storage.kv_set("dedup:scan:alice", "{not json")
    assert load_scan_state(storage, "alice") == ScanState()


def test_storage_has_no_capped_created_at_vector_scan(storage):
    """The old capped scan IS the defect; leaving it around invites reuse."""
    assert not hasattr(storage, "get_user_vectors")


def test_dedup_scan_settings_are_clamped(monkeypatch):
    from jericho.config import load_settings

    monkeypatch.setenv("JERICHO_DEDUP_SCAN_BATCH", "0")
    monkeypatch.setenv("JERICHO_DEDUP_SCAN_MAX_SECONDS", "0.1")
    loaded = load_settings()
    assert loaded.dedup_scan_batch == 1
    assert loaded.dedup_scan_max_seconds == 1.0


# --- compare kernel and storage pages -------------------------------------


def _packed(vector):
    return pack_vector(vector)


def test_kernel_skips_zero_norm_mismatched_dim_and_self_pairs():
    from jericho.dedup import find_near_duplicate_pairs_against

    probes = [
        ("a", _packed([1.0, 0.0, 0.0])),
        ("zero", _packed([0.0, 0.0, 0.0])),
        ("wide", _packed([1.0, 0.0, 0.0, 0.0])),
    ]
    corpus = [
        ("a", _packed([1.0, 0.0, 0.0])),  # same id -> self-pair, suppressed
        ("b", _packed([0.99, 0.14, 0.0])),
        ("zero", _packed([0.0, 0.0, 0.0])),
        ("wide2", _packed([1.0, 0.0, 0.0, 0.0])),
    ]
    pairs = list(find_near_duplicate_pairs_against(probes, corpus, threshold=0.9))
    assert {(low, high) for low, high, _ in pairs} == {("a", "b"), ("wide", "wide2")}


def test_numpy_and_python_kernels_agree(monkeypatch):
    """The optional extra must not change WHICH pairs are reported."""
    import random

    from jericho import dedup as dedup_module
    from jericho.dedup import find_near_duplicate_pairs_against

    random.seed(11)
    rows = [
        (f"ko_{index}", _packed([random.gauss(0.0, 1.0) for _ in range(64)]))  # noqa: S311
        for index in range(40)
    ]
    # Materialised BEFORE the monkeypatch: the kernel is a generator, so its body
    # would otherwise run after _np was already replaced.
    with_numpy = list(find_near_duplicate_pairs_against(rows, rows, threshold=0.1))
    monkeypatch.setattr(dedup_module, "_np", None)
    without_numpy = list(find_near_duplicate_pairs_against(rows, rows, threshold=0.1))

    assert {(low, high) for low, high, _ in with_numpy} == {(low, high) for low, high, _ in without_numpy}
    scores = {(low, high): score for low, high, score in without_numpy}
    for low, high, score in with_numpy:
        assert score == pytest.approx(scores[(low, high)], abs=1e-4)


def test_vector_pages_walk_the_corpus_without_gaps_or_repeats(settings, storage):
    storage.ensure_user("alice")
    expected = set()
    for index in range(7):
        ko = _store(storage, "alice", f"Текст {index}", f"T{index}")
        _index(storage, "alice", ko, [float(index), 1.0, 0.0], "test-embed")
        expected.add(ko)
    # Every row shares one timestamp: ordering must still be total, via the id.
    storage.execute("UPDATE knowledge_embeddings SET updated_at='1500-01-01T00:00:00+00:00'")

    seen: list[str] = []
    cursor = None
    while True:
        page = storage.list_user_vectors_page("alice", "test-embed", after=cursor, limit=2)
        if not page:
            break
        seen.extend(object_id for object_id, _, _ in page)
        cursor = (page[-1][1], page[-1][0])
    assert seen == sorted(expected)  # no gaps, no repeats, deterministic order
    assert storage.count_user_vectors("alice", "test-embed") == 7


def test_vector_pages_exclude_soft_deleted_and_respect_the_second_bound(settings, storage):
    storage.ensure_user("alice")
    live = _store(storage, "alice", "Живой", "L")
    gone = _store(storage, "alice", "Удалённый", "D")
    _index(storage, "alice", live, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", gone, [0.0, 1.0, 0.0], "test-embed")
    storage.soft_delete_knowledge_object(gone, "alice")

    ids = {object_id for object_id, _, _ in storage.list_user_vectors_page("alice", "test-embed")}
    assert ids == {live}
    # An open second is excluded entirely: those rows may not all be written yet.
    assert storage.list_user_vectors_page("alice", "test-embed", max_updated_at="1500-01-01") == []


def test_count_user_vectors_before_a_cursor(settings, storage):
    storage.ensure_user("alice")
    for index in range(4):
        _index(storage, "alice", _store(storage, "alice", f"T{index}", f"T{index}"), [1.0, 0.0], "test-embed")
    storage.execute("UPDATE knowledge_embeddings SET updated_at='1500-01-01T00:00:00+00:00'")
    rows = storage.list_user_vectors_page("alice", "test-embed")
    middle = (rows[2][1], rows[2][0])
    assert storage.count_user_vectors("alice", "test-embed") == 4
    assert storage.count_user_vectors("alice", "test-embed", before=middle) == 2


def test_get_conflict_pair_statuses_reflects_review(settings, storage):
    storage.ensure_user("alice")
    a = _store(storage, "alice", "A", "A")
    b = _store(storage, "alice", "B", "B")
    c = _store(storage, "alice", "C", "C")
    storage.store_knowledge_conflict("alice", a, b, conflict_type=NEAR_DUPLICATE_TYPE, confidence=0.95)
    other = storage.store_knowledge_conflict(
        "alice", a, c, conflict_type="potential_contradiction", confidence=0.5
    )
    conflict = storage.list_knowledge_conflicts("alice", status="suggested")
    near = next(row for row in conflict if row["conflict_type"] == NEAR_DUPLICATE_TYPE)
    storage.review_knowledge_conflict("alice", near["id"], "dismissed", reviewed_by="alice")

    statuses = storage.get_conflict_pair_statuses("alice", NEAR_DUPLICATE_TYPE)
    assert statuses == {storage.conflict_pair_key(a, b): "dismissed"}
    assert other["pair_key"] not in statuses  # a different conflict type stays out


# --- regressions found by adversarial review ------------------------------


def test_a_tile_costlier_than_the_budget_still_makes_progress(settings, storage, monkeypatch):
    """The page cursor inside a sweep is local, so an interrupted sweep is discarded
    and replayed identically. Without a guaranteed first tile, a corpus whose sweep
    costs more than one budget would never advance — zero progress, forever."""
    from dataclasses import replace

    from jericho import dedup as dedup_module

    monkeypatch.setattr(dedup_module, "_CORPUS_PAGE", 2)  # force a multi-page sweep
    cfg = replace(_dedup_settings(settings), dedup_scan_batch=2)
    storage.ensure_user("alice")
    vectors = ([1.0, 0.0, 0.0], [0.98, 0.2, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.7, 0.71])
    for index, vector in enumerate(vectors):
        _index(storage, "alice", _store(storage, "alice", f"T{index}", f"T{index}"), vector, "test-embed")

    seen = []
    for _ in range(6):
        result = _run(storage, cfg, max_seconds=0.0)
        seen.append(result["objects_compared"])
        if result["pending"] == 0 and result["objects_compared"] == 0:
            break
    assert seen[0] > 0, "a starved run made no progress at all — the scan is stalled"
    assert result["pending"] == 0
    assert len(storage.list_knowledge_conflicts("alice", status="suggested")) == 1


def test_end_of_history_comes_from_an_empty_page_not_a_short_one(settings, storage, monkeypatch):
    """Storage silently trims an oversized page request, so a short page must never be
    read as 'history finished' — that would leave everything older unprobed forever."""
    from dataclasses import replace

    from jericho import dedup as dedup_module

    cfg = replace(_dedup_settings(settings), dedup_scan_batch=99999)
    requested: list[int] = []
    original_page = storage.list_user_vectors_page

    def _spy(*args, **kwargs):
        requested.append(int(kwargs.get("limit", 2048)))
        return original_page(*args, **kwargs)

    monkeypatch.setattr(storage, "list_user_vectors_page", _spy)
    storage.ensure_user("alice")
    old_a = _store(storage, "alice", "Купить молоко и хлеб.", "Старый A")
    old_b = _store(storage, "alice", "Купить молоко, хлеб.", "Старый B")
    _index(storage, "alice", old_a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", old_b, [0.98, 0.2, 0.0], "test-embed")
    for index, vector in enumerate(([0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.7, 0.71])):
        _index(storage, "alice", _store(storage, "alice", f"P{index}", f"P{index}"), vector, "test-embed")

    for _ in range(6):
        result = _run(storage, cfg)
        if result["pending"] == 0 and result["objects_compared"] == 0:
            break
    assert result["pending"] == 0
    # Never ask storage for more than it will honour: a silently trimmed page would
    # come back "short" and be misread as end-of-history.
    assert max(requested) <= dedup_module._MAX_PAGE_ROWS
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert len(conflicts) == 1
    assert {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]} == {old_a, old_b}


def test_pair_accumulator_is_bounded_on_a_duplicate_cluster(settings, storage, monkeypatch):
    """Every above-threshold pair used to be retained: quadratic in the cluster size,
    on exactly the bulk-import workload this module exists for."""
    from dataclasses import replace

    from jericho import dedup as dedup_module

    monkeypatch.setattr(dedup_module, "_MAX_PAIRS_PER_RUN", 3)
    cfg = replace(_dedup_settings(settings), dedup_scan_batch=64)
    storage.ensure_user("alice")
    for index in range(8):  # 8 identical records -> 28 pairs, only 3 may be kept
        _index(
            storage,
            "alice",
            _store(storage, "alice", f"Копия {index}", f"K{index}"),
            [1.0, 0.0],
            "test-embed",
        )

    result = _run(storage, cfg)
    assert result["detected"] == 3
    assert result["candidate_pairs"] == 28  # all seen and counted...
    assert result["incomplete"] is True  # ...but the run says work was left over


def test_numpy_and_python_agree_at_the_threshold_boundary(monkeypatch):
    """float32 sgemm drifts ~5e-7 — enough to move a pair across the threshold and
    make the optional extra report a DIFFERENT set of duplicates."""
    import math

    from jericho import dedup as dedup_module
    from jericho.dedup import find_near_duplicate_pairs_against

    threshold = 0.92
    rows = []
    for index, target in enumerate((threshold - 1e-7, threshold, threshold + 1e-7, 1.0)):
        angle = math.acos(max(-1.0, min(1.0, target)))
        rows.append((f"ko_{index}", pack_vector([math.cos(angle), math.sin(angle)])))
    base = [("ko_base", pack_vector([1.0, 0.0]))]

    with_numpy = list(find_near_duplicate_pairs_against(base, rows, threshold=threshold))
    monkeypatch.setattr(dedup_module, "_np", None)
    without_numpy = list(find_near_duplicate_pairs_against(base, rows, threshold=threshold))
    assert {(low, high) for low, high, _ in with_numpy} == {(low, high) for low, high, _ in without_numpy}
    # float32 sgemm would drift ~5e-7 here; float64 keeps both accumulators together,
    # which is what stops a borderline pair landing on different sides of the gate.
    python_scores = {(low, high): score for low, high, score in without_numpy}
    for low, high, score in with_numpy:
        assert score == pytest.approx(python_scores[(low, high)], abs=1e-12)


def test_concurrent_state_writes_merge_conservatively():
    """Worker tick and manual admin scan overlap; the loser may cost extra work but
    must never mark history finished on the winner's behalf."""
    from jericho.dedup import ScanState, _merge_concurrent, save_scan_state

    class _Kv:
        def __init__(self):
            self.value = None

        def kv_set(self, key, value):
            self.value = value

    stored = ScanState(model="m", threshold=0.9, watermark=("2020", "ko_z"), backfill_done=True)
    kv = _Kv()
    save_scan_state(kv, "alice", stored)

    mine = ScanState(model="m", threshold=0.9, watermark=("2019", "ko_a"), backfill_done=False)
    merged = _merge_concurrent(kv.value, mine)
    assert merged.watermark == ("2020", "ko_z")  # the further cursor wins
    assert merged.backfill_done is False  # finished only when BOTH agree


def test_a_row_appearing_below_the_watermark_reopens_the_backfill(settings, storage):
    """A wall-clock step backwards can land a row under the cursor, where the strict
    `after` bound would hide it from probing forever."""
    cfg = _dedup_settings(settings)
    storage.ensure_user("alice")
    for index, vector in enumerate(([0.0, 1.0, 0.0], [0.0, 0.0, 1.0])):
        _index(storage, "alice", _store(storage, "alice", f"P{index}", f"P{index}"), vector, "test-embed")
    for _ in range(4):
        result = _run(storage, cfg)
        if result["pending"] == 0 and result["objects_compared"] == 0:
            break
    assert result["pending"] == 0

    # Two duplicates land BELOW the watermark, as a backwards clock step would do.
    old_a = _store(storage, "alice", "Купить молоко и хлеб.", "Старый A")
    old_b = _store(storage, "alice", "Купить молоко, хлеб.", "Старый B")
    _index(storage, "alice", old_a, [1.0, 0.0, 0.0], "test-embed")
    _index(storage, "alice", old_b, [0.98, 0.2, 0.0], "test-embed")
    from jericho.dedup import load_scan_state

    watermark = load_scan_state(storage, "alice").watermark
    assert watermark is not None
    before_insert = storage.count_user_vectors("alice", "test-embed", before=watermark)
    storage.execute(
        "UPDATE knowledge_embeddings SET updated_at='0500-01-01T00:00:00+00:00' "
        "WHERE knowledge_object_id IN (?, ?)",
        (old_a, old_b),
    )
    # Strictly BELOW the cursor: the ascending `after` bound would hide them forever.
    assert storage.count_user_vectors("alice", "test-embed", before=watermark) == before_insert + 2

    for _ in range(6):
        result = _run(storage, cfg)
        if result["pending"] == 0 and result["objects_compared"] == 0:
            break
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert len(conflicts) == 1
    assert {conflicts[0]["knowledge_a_id"], conflicts[0]["knowledge_b_id"]} == {old_a, old_b}


def test_the_default_threshold_clears_the_measured_non_duplicate_ceiling():
    """The shipped default must sit ABOVE what non-duplicates actually score.

    The previous default, 0.92, sat inside that distribution: two weekly meeting
    notes written to one template scored 0.928 on the installed model, two entries
    about one apartment 0.917 and 0.914. Whether a series of minutes got proposed
    for merging came down to the third decimal — and the reviewer sees only a number,
    with no way to tell "the same note twice" from "the next note in the series".

    Measured by `tools/dedup_threshold_probe.py`; rerun it if the model changes, and
    move the ceiling constant to whatever it then reports rather than moving this
    assertion.
    """
    from jericho.config import load_settings
    from jericho.dedup import _MEASURED_NON_DUPLICATE_CEILING

    default = load_settings().dedup_threshold
    assert default > _MEASURED_NON_DUPLICATE_CEILING, (
        f"default {default} does not clear the measured non-duplicate ceiling "
        f"{_MEASURED_NON_DUPLICATE_CEILING}"
    )
