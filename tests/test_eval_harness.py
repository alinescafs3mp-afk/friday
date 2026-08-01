"""Retrieval eval harness — §7: quality stops being a feeling.

Pins the pure metrics (recall@k / precision@k / MRR), the gold-set storage, a
run over the real hybrid searcher that actually finds seeded knowledge,
regression detection against the previous run, and the admin endpoints
(cases CRUD + label-from-results search + run).
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from friday.eval import precision_at_k, recall_at_k, reciprocal_rank, run_eval
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id

# --- pure metrics ---------------------------------------------------------


def test_metric_functions():
    retrieved = ["a", "b", "c", "d"]
    expected = {"b", "d", "z"}
    assert recall_at_k(retrieved, expected, 4) == pytest.approx(2 / 3)
    assert recall_at_k(retrieved, expected, 1) == 0.0  # only "a" in top-1
    assert precision_at_k(retrieved, expected, 2) == 0.5  # "a","b" -> 1 hit
    assert reciprocal_rank(retrieved, expected) == 0.5  # first hit at rank 2
    assert reciprocal_rank(["x", "y"], expected) == 0.0


# --- storage gold set -----------------------------------------------------


def test_add_list_delete_eval_case(storage):
    storage.ensure_user("alice")
    case = storage.add_eval_case("alice", "  ip   atlas ", ["ko_1", "ko_2", "ko_1"], note="dupes")
    assert case["query"] == "ip atlas"  # normalised
    assert case["expected_ids"] == ["ko_1", "ko_2"]  # deduped, sorted

    # Same query upserts rather than duplicating.
    storage.add_eval_case("alice", "ip atlas", ["ko_3"])
    cases = storage.list_eval_cases("alice")
    assert len(cases) == 1
    assert cases[0]["expected_ids"] == ["ko_3"]

    with pytest.raises(ValueError, match="query is required"):
        storage.add_eval_case("alice", "   ", ["ko_1"])
    with pytest.raises(ValueError, match="expected"):
        storage.add_eval_case("alice", "another", [])

    assert storage.delete_eval_case("alice", cases[0]["id"]) is True
    assert storage.list_eval_cases("alice") == []


# --- run over the real searcher -------------------------------------------


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


@pytest.mark.asyncio
async def test_run_eval_measures_and_detects_regression(settings, storage):
    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7 в дата-центре.", "Atlas IP")
    _store(storage, "alice", "Отпуск запланирован на декабрь.", "Отпуск")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])

    report = await run_eval(storage, None, settings, "alice", k=5)
    assert report["cases"] == 1
    assert report["listed"] == 1
    assert report["scored"] == 1
    assert report["skipped_empty_expected"] == 0
    assert report["recall_at_k"] == 1.0  # the seeded record is found
    assert report["per_case"][0]["found"] == 1
    assert report["regression"]["previous_recall"] is None  # first run, no baseline

    # A second gold case the searcher cannot satisfy drops recall — a regression
    # relative to the stored first run.
    storage.add_eval_case("alice", "несуществующая тема без совпадений xyzzy", ["ko_missing"])
    second = await run_eval(storage, None, settings, "alice", k=5)
    assert second["recall_at_k"] < 1.0
    assert second["regression"]["previous_recall"] == 1.0
    assert second["regression"]["regressed"] is True


@pytest.mark.asyncio
async def test_run_eval_empty_gold_set(settings, storage):
    storage.ensure_user("alice")
    report = await run_eval(storage, None, settings, "alice")
    assert report == {"cases": 0, "recall_at_k": None, "reason": "no gold cases"}


# --- admin endpoints ------------------------------------------------------


def test_eval_endpoints_end_to_end(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        target = _store(app.state.storage, LEGACY_OWNER_USER_ID, "Проект Orion на PostgreSQL 16.", "Orion")

        # Label-from-results: search surfaces the record to pick.
        found = client.get("/api/admin/eval/search", params={"q": "Orion PostgreSQL"}, headers=owner)
        assert found.status_code == 200
        assert target in [item["id"] for item in found.json()["items"]]

        added = client.post(
            "/api/admin/eval/cases",
            json={"query": "Orion база данных", "expected_ids": [target], "note": "t"},
            headers=owner,
        )
        assert added.status_code == 200

        listed = client.get("/api/admin/eval/cases", headers=owner)
        assert listed.json()["count"] == 1

        run = client.post("/api/admin/eval/run", json={}, headers=owner)
        assert run.status_code == 200
        assert run.json()["report"]["recall_at_k"] == 1.0

        case_id = listed.json()["items"][0]["id"]
        deleted = client.delete(
            f"/api/admin/eval/cases/{case_id}", params={"user_id": LEGACY_OWNER_USER_ID}, headers=owner
        )
        assert deleted.status_code == 200
        assert client.get("/api/admin/eval/cases", headers=owner).json()["count"] == 0

        actions = [row["action"] for row in app.state.storage.list_audit_log(None, limit=50)]
        assert {"admin.eval.case_add", "admin.eval.run", "admin.eval.case_delete"} <= set(actions)


# --- mining the gold set from confirmed feedback --------------------------


def _store_ko(storage, user_id, text="Atlas работает на PostgreSQL 16, порт 5432"):
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(new_id("h").encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title="Atlas",
        summary=text,
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _answer_message(storage, user_id, *, search_query, ko_ids):
    conversation = storage.create_conversation(user_id, "t")
    return storage.store_message(
        conversation["id"],
        user_id,
        "assistant",
        "Ответ [K1].",
        metadata={
            "search_query": search_query,
            "knowledge_object_ids": ko_ids,
            "knowledge_citations": {"K1": ko_ids[0]} if ko_ids else {},
            "interaction_mode": "knowledge_work",
        },
    )["id"]


def test_upsert_feedback_eval_case_never_overwrites_manual(storage):
    storage.ensure_user("alice")
    assert storage.upsert_feedback_eval_case("alice", " база  atlas ", ["ko_b", "ko_a"]) is True
    case = storage.list_eval_cases("alice")[0]
    assert case["query"] == "база atlas" and case["source"] == "feedback"
    assert case["expected_ids"] == ["ko_a", "ko_b"]  # deduped, sorted
    # Refreshing a feedback case updates its expected set.
    assert storage.upsert_feedback_eval_case("alice", "база atlas", ["ko_a"]) is True
    assert storage.list_eval_cases("alice")[0]["expected_ids"] == ["ko_a"]
    # A hand-curated case for the same query is never clobbered.
    storage.add_eval_case("alice", "ручной", ["ko_manual"], source="manual")
    assert storage.upsert_feedback_eval_case("alice", "ручной", ["ko_evil"]) is False
    manual = next(c for c in storage.list_eval_cases("alice") if c["query"] == "ручной")
    assert manual["expected_ids"] == ["ko_manual"] and manual["source"] == "manual"
    # Empty query or empty ids are ignored.
    assert storage.upsert_feedback_eval_case("alice", "", ["ko"]) is False
    assert storage.upsert_feedback_eval_case("alice", "q", []) is False


@pytest.mark.asyncio
async def test_positive_answer_feedback_mines_eval_case(settings, storage):
    from friday.agent_runtime import AgentRuntime
    from friday.storage.models import FeedbackType

    storage.ensure_user("alice")
    ko = _store_ko(storage, "alice")
    message_id = _answer_message(storage, "alice", search_query="ip сервера atlas", ko_ids=[ko])
    runtime = AgentRuntime(settings, storage)

    await runtime.record_feedback("alice", "answer", message_id, FeedbackType.ANSWER_USEFULNESS, 1.0)
    cases = {case["query"]: case for case in storage.list_eval_cases("alice")}
    assert "ip сервера atlas" in cases
    assert cases["ip сервера atlas"]["expected_ids"] == [ko]
    assert cases["ip сервера atlas"]["source"] == "feedback"


@pytest.mark.asyncio
async def test_feedback_mining_skips_negative_disabled_and_untethered(settings, storage):
    import dataclasses

    from friday.agent_runtime import AgentRuntime
    from friday.storage.models import FeedbackType

    storage.ensure_user("alice")
    ko = _store_ko(storage, "alice")

    # Negative feedback -> no case.
    negative_msg = _answer_message(storage, "alice", search_query="negative q", ko_ids=[ko])
    await AgentRuntime(settings, storage).record_feedback(
        "alice", "answer", negative_msg, FeedbackType.ANSWER_USEFULNESS, -1.0
    )
    assert not any(c["query"] == "negative q" for c in storage.list_eval_cases("alice"))

    # Positive but the feature is disabled -> no case.
    disabled = dataclasses.replace(settings, eval_mine_from_feedback=False)
    off_msg = _answer_message(storage, "alice", search_query="disabled q", ko_ids=[ko])
    await AgentRuntime(disabled, storage).record_feedback(
        "alice", "answer", off_msg, FeedbackType.ANSWER_USEFULNESS, 1.0
    )
    assert not any(c["query"] == "disabled q" for c in storage.list_eval_cases("alice"))

    # Positive but the answer cited no KO (e.g. tool-only) -> nothing to expect.
    untethered = _answer_message(storage, "alice", search_query="untethered q", ko_ids=[])
    await AgentRuntime(settings, storage).record_feedback(
        "alice", "answer", untethered, FeedbackType.ANSWER_USEFULNESS, 1.0
    )
    assert not any(c["query"] == "untethered q" for c in storage.list_eval_cases("alice"))

    # A synthetic contextualized follow-up (multi-line, truncatable) is skipped.
    followup = _answer_message(
        storage, "alice", search_query="что там про базу\nFollow-up: а порт?", ko_ids=[ko]
    )
    await AgentRuntime(settings, storage).record_feedback(
        "alice", "answer", followup, FeedbackType.ANSWER_USEFULNESS, 1.0
    )
    assert not any("Follow-up" in c["query"] for c in storage.list_eval_cases("alice"))

    # A trivially short/generic query is skipped.
    short = _answer_message(storage, "alice", search_query="привет", ko_ids=[ko])
    await AgentRuntime(settings, storage).record_feedback(
        "alice", "answer", short, FeedbackType.ANSWER_USEFULNESS, 1.0
    )
    assert not any(c["query"] == "привет" for c in storage.list_eval_cases("alice"))


def test_is_mineable_eval_query_filters_followups_and_generic():
    from friday.agent_runtime import _is_mineable_eval_query

    assert _is_mineable_eval_query("ip сервера atlas") is True
    assert _is_mineable_eval_query("привет") is False  # too short/generic
    assert _is_mineable_eval_query("что там\nFollow-up: а когда?") is False  # synthetic follow-up
    assert _is_mineable_eval_query("x" * 501) is False  # past the store cap
    assert _is_mineable_eval_query("") is False


# --- the harness must not contaminate what it measures ---------------------


@pytest.mark.asyncio
async def test_eval_does_not_write_usage_counters(settings, storage):
    """The gold-set run must leave the ranking signals it measures untouched.

    ``HybridSearcher.search`` records a retrieval counter for its top hits, and
    ``usage_signal`` reads that same counter back into the blend — so every eval run
    was nudging the signal it is supposed to be measuring, and the two arms of an A/B
    depended on which one ran first.
    """
    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7 в дата-центре.", "Atlas IP")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])

    before = storage.get_knowledge_usage("alice", [target])
    await run_eval(storage, None, settings, "alice", k=5)
    after = storage.get_knowledge_usage("alice", [target])

    assert after.get(target, {}).get("retrieval_count", 0) == before.get(target, {}).get("retrieval_count", 0)


def test_manual_case_survives_the_listing_window(storage):
    """A hand-curated case must not be pushed out of the gold set by mined ones."""
    storage.ensure_user("alice")
    storage.add_eval_case("alice", "ручной эталонный запрос", ["ko_manual"])
    for index in range(1100):
        storage.upsert_feedback_eval_case("alice", f"намайненный запрос номер {index}", [f"ko_{index}"])

    queries = {case["query"] for case in storage.list_eval_cases("alice")}
    assert "ручной эталонный запрос" in queries


# --- ablation --------------------------------------------------------------


def test_sign_test_matches_the_exact_binomial():
    from friday.eval import _sign_test_p

    assert _sign_test_p(6, 0) == pytest.approx(0.03125)
    assert _sign_test_p(5, 0) == pytest.approx(0.0625)
    assert _sign_test_p(0, 0) == 1.0
    assert _sign_test_p(3, 3) == 1.0


def test_ablation_seam_is_neutral_by_default(storage, settings):
    """Turning the seam on with nothing ablated must reproduce the shipped ranking."""
    from friday.retrieval import ABLATABLE_SIGNALS, HybridSearcher

    assert HybridSearcher(storage, None)._ablate == frozenset()  # noqa: SLF001
    assert HybridSearcher(storage, None, ablate=())._ablate == frozenset()  # noqa: SLF001
    # Only genuinely independent weights are offered for ablation.
    assert set(ABLATABLE_SIGNALS) == {
        "feedback",
        "usage",
        "kind_alignment",
        "fts_bonus",
        "noise_penalty",
    }


@pytest.mark.asyncio
async def test_ablation_refuses_a_verdict_on_a_small_gold_set(settings, storage):
    """Three cases cannot separate signal from noise — the honest answer is a refusal."""
    from friday.eval import compare_signal_ablation

    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7 в дата-центре.", "Atlas IP")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])

    report = await compare_signal_ablation(storage, None, settings, "alice", k=5)
    assert report["underpowered"] is True
    assert report["min_cases_for_a_verdict"] == 12
    assert {row["verdict"] for row in report["ranked"]} == {"insufficient_evidence"}
    assert "reason" in report
    # Entangled signals are named with a reason instead of given a fake number.
    assert "embedding" in report["not_measured"]
    assert all("delta_recall" not in str(key) for key in report["not_measured"])


@pytest.mark.asyncio
async def test_ablation_does_not_move_the_regression_baseline(settings, storage):
    from friday.eval import compare_signal_ablation, run_eval

    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7.", "Atlas IP")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])
    await run_eval(storage, None, settings, "alice", k=5)
    baseline = storage.kv_get("eval:last_run:alice")

    await compare_signal_ablation(storage, None, settings, "alice", k=5)
    assert storage.kv_get("eval:last_run:alice") == baseline


@pytest.mark.asyncio
async def test_regression_is_not_flagged_across_a_k_change(settings, storage):
    """recall@5 and recall@10 are different metrics; comparing them is a phantom."""
    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7.", "Atlas IP")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])

    await run_eval(storage, None, settings, "alice", k=10)
    second = await run_eval(storage, None, settings, "alice", k=5)
    assert second["regression"]["regressed"] is False
    assert second["regression"]["reason"] == "k changed"


# --- gold-set hygiene ------------------------------------------------------


@pytest.mark.asyncio
async def test_run_eval_reports_gold_set_health(settings, storage):
    """A case whose objects were deleted must be distinguishable from bad search."""
    storage.ensure_user("alice")
    alive = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7.", "Atlas IP")
    dead = _store(storage, "alice", "Старый сервер Borealis.", "Borealis")
    storage.add_eval_case("alice", "IP сервера Atlas", [alive])
    storage.add_eval_case("alice", "адрес Borealis", [dead])
    storage.soft_delete_knowledge_object(dead, "alice")

    report = await run_eval(storage, None, settings, "alice", k=5)
    assert report["gold_set"]["stale"] == 1
    assert report["gold_set"]["stale_manual"] == 1


def test_prune_never_deletes_a_manual_case(storage):
    storage.ensure_user("alice")
    storage.add_eval_case("alice", "ручной кейс про мёртвый объект", ["ko_dead"])
    storage.upsert_feedback_eval_case("alice", "намайненный кейс про мёртвый объект", ["ko_dead"])

    pruned = storage.prune_eval_cases("alice")
    queries = {case["query"] for case in storage.list_eval_cases("alice")}
    assert "ручной кейс про мёртвый объект" in queries
    assert "намайненный кейс про мёртвый объект" not in queries
    assert pruned["deleted_dead"] == 1
    assert storage.eval_case_health("alice")["stale_manual"] == 1


def test_mined_cases_are_capped_per_user(storage):
    from friday.storage import EVAL_MINED_CASE_CAP

    storage.ensure_user("alice")
    live = _store(storage, "alice", "Живой объект для эталонов.", "Живой")
    for index in range(EVAL_MINED_CASE_CAP + 50):
        storage.upsert_feedback_eval_case("alice", f"намайненный запрос номер {index}", [live])

    pruned = storage.prune_eval_cases("alice")
    assert pruned["deleted_over_cap"] == 50
    assert pruned["kept_mined"] == EVAL_MINED_CASE_CAP


@pytest.mark.asyncio
async def test_baseline_reanchors_after_a_k_change(settings, storage):
    """Refusing one comparison is right; refusing every future one is a dead watchdog.

    Returning early from the k guard skipped the write that stores the new baseline, so
    the old k stayed forever and every later run took the same branch — regression
    detection silently reported "no regression" no matter how far recall fell.
    """
    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7.", "Atlas IP")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])

    await run_eval(storage, None, settings, "alice", k=10)
    second = await run_eval(storage, None, settings, "alice", k=5)
    assert second["regression"]["reason"] == "k changed"

    # The run at the new k became the baseline, so the NEXT one compares normally.
    third = await run_eval(storage, None, settings, "alice", k=5)
    assert third["regression"]["previous_recall"] is not None
    assert "reason" not in third["regression"]


@pytest.mark.asyncio
async def test_ablation_arms_are_deduplicated(settings, storage):
    """Each arm is a full pass over the gold set; a repeated name must not multiply it."""
    from friday.eval import compare_signal_ablation

    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7.", "Atlas IP")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])

    report = await compare_signal_ablation(
        storage, None, settings, "alice", k=5, signals=["usage", "usage", "usage", "нет-такого"]
    )
    assert [row["signal"] for row in report["ranked"]] == ["usage"]


def test_ablation_reports_reordering_that_recall_cannot_see():
    """recall@k only asks whether the object is INSIDE the top k, so it is blind to
    reordering within it — which is exactly where the 0.05/0.028/0.018 weights work.
    A signal that only reorders must not be reported as having no effect."""
    from friday.eval import _MATERIAL_MRR, _sign_test_p

    # Membership unchanged (recall delta 0), order moved consistently across cases.
    assert _sign_test_p(0, 0) == 1.0  # nothing moved -> no evidence
    assert _sign_test_p(8, 0) < 0.05  # every case moved the same way -> significant
    assert _MATERIAL_MRR == 0.01


@pytest.mark.asyncio
async def test_ablation_row_carries_both_metrics(settings, storage):
    from friday.eval import compare_signal_ablation

    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7.", "Atlas IP")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])

    report = await compare_signal_ablation(storage, None, settings, "alice", k=5, signals=["usage"])
    row = report["ranked"][0]
    # Both metrics and both p-values travel together, so a reordering effect is visible
    # even when the verdict (correctly) refuses to conclude from it.
    for key in ("delta_recall", "delta_mrr", "p_value", "p_value_mrr", "rank_better", "rank_worse"):
        assert key in row, key
