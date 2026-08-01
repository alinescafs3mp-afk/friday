"""Six places where a number was the length of a truncated page, not a count.

The «Качество» tiles are the sharpest case, because one of them saturates BELOW its
own limit and therefore cannot be recognised as truncated by looking at it. The
listing takes 500 rows in `importance ASC` order and filters in python afterwards, so
protected file-derived objects — which sit at importance 0 — are fed first and eat the
window. Measured during the audit: 900 real candidates showed as 200.

The conversation transcript had a second defect underneath the first: its sort was not
merely unstable but actively wrong. `created_at` is written to second precision, so a
question and its answer share one, and an outer `ORDER BY created_at ASC` around a
newest-first subquery preserves that inner order instead of reversing it — every pair
came back answer-first. That same history feeds the agent's prompt.
"""

from __future__ import annotations

import hashlib

from friday.storage.models import KnowledgeObject, RawObject, new_id


def _stale(storage, user_id: str, title: str, *, importance: float, protected: bool = False) -> str:
    content = f"Содержимое {title}"
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="file" if protected else "text",
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="file" if protected else "text",
        title=title,
        importance=importance,
        quality_score=0.2,
        promotion_score=0.2,
    )
    storage.store_knowledge_object(ko)
    storage.execute(
        "UPDATE knowledge_objects SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (ko.id,)
    )
    storage.commit()
    return ko.id


# --- «Качество»: плитка, которая насыщалась ниже собственного лимита -------


def test_the_lifecycle_tile_counts_all_candidates_not_one_page(storage):
    storage.ensure_user("alice")
    real = {_stale(storage, "alice", f"Устаревшая {index}", importance=0.1) for index in range(9)}

    assert storage.count_lifecycle_candidates("alice") == len(real)
    page = storage.list_lifecycle_candidates("alice", limit=3)
    assert len(page) == 3, "the page did not honour its own limit"


def test_protected_objects_do_not_eat_the_window(storage):
    """The exact shape that made 900 read as 200: protected rows sort first."""
    storage.ensure_user("alice")
    for index in range(6):
        _stale(storage, "alice", f"Файл {index}", importance=0.0, protected=True)
    real = {_stale(storage, "alice", f"Заметка {index}", importance=0.1) for index in range(5)}

    # A page of three is smaller than the protected prefix — the old shape would have
    # returned nothing at all and reported it as the count.
    assert storage.count_lifecycle_candidates("alice") == len(real)
    page = storage.list_lifecycle_candidates("alice", limit=3)
    assert len(page) == 3
    assert {str(item["knowledge_object"]["id"]) for item in page} <= real


def test_walking_the_lifecycle_pages_yields_each_candidate_once(storage):
    storage.ensure_user("alice")
    real = {_stale(storage, "alice", f"Устаревшая {index}", importance=0.1) for index in range(11)}
    total = storage.count_lifecycle_candidates("alice")
    assert total == 11

    seen: list[str] = []
    for offset in range(0, total, 4):
        page = storage.list_lifecycle_candidates("alice", limit=4, offset=offset)
        seen.extend(str(item["knowledge_object"]["id"]) for item in page)
    assert len(seen) == len(set(seen)), "a candidate appeared on two pages"
    assert set(seen) == real


def test_the_apply_guard_sees_every_candidate(storage):
    """`require_candidate` must not reject a row just because it is on a later page."""
    storage.ensure_user("alice")
    real = {_stale(storage, "alice", f"Устаревшая {index}", importance=0.1) for index in range(11)}
    everything = {str(item["knowledge_object"]["id"]) for item in storage.all_lifecycle_candidates("alice")}
    assert everything == real


# --- транскрипт: порядок был не просто неустойчивым, а неверным ------------


def _exchange(storage, conversation_id: str, user_id: str, turn: int) -> None:
    """A question and its answer inside one second, as real turns are."""
    storage.store_message(conversation_id, user_id, "user", f"Вопрос {turn}")
    storage.store_message(conversation_id, user_id, "assistant", f"Ответ {turn}")


def test_a_question_comes_before_its_answer(storage):
    storage.ensure_user("alice")
    conversation = storage.create_conversation("alice", "Диалог")["id"]
    for turn in range(6):
        _exchange(storage, conversation, "alice", turn)

    rows = storage.get_conversation_messages(conversation, user_id="alice", limit=100)
    contents = [row["content"] for row in rows]
    for turn in range(6):
        question = contents.index(f"Вопрос {turn}")
        answer = contents.index(f"Ответ {turn}")
        assert question < answer, f"turn {turn} came back answer-first: {contents}"


def test_the_transcript_reports_how_many_messages_there_are(storage):
    storage.ensure_user("alice")
    conversation = storage.create_conversation("alice", "Диалог")["id"]
    for turn in range(7):
        _exchange(storage, conversation, "alice", turn)

    assert storage.count_messages(conversation, user_id="alice") == 14
    window = storage.get_conversation_messages(conversation, user_id="alice", limit=4)
    assert len(window) == 4
    # No offset means the TAIL, as it always did.
    assert window[-1]["content"] == "Ответ 6"


def test_the_beginning_of_a_long_conversation_is_reachable(storage):
    storage.ensure_user("alice")
    conversation = storage.create_conversation("alice", "Диалог")["id"]
    for turn in range(7):
        _exchange(storage, conversation, "alice", turn)

    head = storage.get_conversation_messages(conversation, user_id="alice", limit=4, offset=0)
    assert head[0]["content"] == "Вопрос 0", "the start of the conversation is still unreachable"

    total = storage.count_messages(conversation, user_id="alice")
    seen: list[str] = []
    for offset in range(0, total, 4):
        seen.extend(
            row["content"]
            for row in storage.get_conversation_messages(
                conversation, user_id="alice", limit=4, offset=offset
            )
        )
    assert len(seen) == total
    assert len(set(seen)) == total, "a message appeared on two pages"


# --- пользователи и трейс --------------------------------------------------


def test_the_user_list_reports_a_real_total(storage):
    for index in range(5):
        storage.ensure_user(f"usr_{index}")
    assert storage.count_users() >= 5
    assert storage.count_users() == len(storage.list_users(limit=5000))


def test_the_trace_target_is_checked_before_writing():
    """Leaving the section mid-request gave a toast with an English TypeError."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "friday" / "admin_ui" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    body = source[source.index("actions.explainSearch") :][:600]
    assert "if(!box)return" in body


# --- аудит: журнал, который сам себе дописывает строки ---------------------


def test_the_audit_page_walks_a_fixed_snapshot(storage):
    """Reading the audit log writes to the audit log; the anchor keeps the window still."""
    from friday.storage.models import AuditEntry

    storage.ensure_user("alice")
    for index in range(10):
        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id="alice",
                action=f"seed.{index:02d}",
                target_type="test",
                target_id=f"seed-{index}",
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
    anchor = storage.list_audit_log("alice", limit=1)[0]["created_at"]
    before_total = storage.count_audit_log("alice", before=anchor)

    # …and now the reading writes its own rows, as the real route does. Stamped a
    # second later because that is what a page turn is: `created_at` has one-second
    # resolution, so the anchor bounds everything after that second, not within it.
    for _ in range(5):
        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id="alice",
                action="admin.audit.read",
                target_type="test",
                target_id="reader",
                created_at="2026-01-01T00:00:05+00:00",
            )
        )

    assert storage.count_audit_log("alice", before=anchor) == before_total, "the total moved under the reader"
    assert storage.count_audit_log("alice") > before_total, "the anchor is not filtering anything"

    page = storage.list_audit_log("alice", limit=50, before=anchor)
    assert all(entry["action"] != "admin.audit.read" for entry in page)


def test_the_lifecycle_count_does_not_go_through_a_page():
    """Structural, because the difference only shows above 500 objects.

    Counting by `len(list_lifecycle_candidates(limit=500))` is exactly the defect: it
    agrees with the truth on any small corpus and silently saturates on a real one, so
    a test that seeds a dozen rows cannot tell the two apart. What it can check is that
    the count walks the full set rather than a page.
    """
    import inspect

    from friday.storage._knowledge import KnowledgeMixin

    body = inspect.getsource(KnowledgeMixin.count_lifecycle_candidates)
    assert "_lifecycle_candidates" in body
    assert "list_lifecycle_candidates" not in body, "the count is measuring a page again"
    assert "limit" not in body, "a count must not take a page size"


# --- блок Feedback loop и дайджест рефлексии ------------------------------


def _feedback(storage, user_id: str, target: str, *, kind: str, score: float) -> None:
    from friday.storage.models import FeedbackItem, FeedbackType

    storage.store_feedback(
        FeedbackItem(
            id=new_id("fb"),
            user_id=user_id,
            target_type="classification" if kind == "classification" else "answer",
            target_id=target,
            feedback_type=(
                FeedbackType.CLASSIFICATION if kind == "classification" else FeedbackType.ANSWER_USEFULNESS
            ),
            score=score,
        )
    )


def test_the_feedback_tiles_count_rather_than_measure_a_page(storage):
    storage.ensure_user("alice")
    for index in range(7):
        _feedback(storage, "alice", f"cls-{index}", kind="classification", score=1.0 if index % 2 else -1.0)
    for index in range(4):
        _feedback(storage, "alice", f"ans-{index}", kind="answer", score=1.0)

    assert storage.count_feedback_state("alice") == 11
    assert storage.count_feedback_state("alice", feedback_type="classification") == 7
    negative = storage.count_feedback_state("alice", feedback_type="classification", negative_only=True)
    assert negative == 4, "the negative filter does not select what the python did"


def test_the_negative_filter_matches_the_python_it_replaced(storage):
    """`score < 0` against `float(item.get("score") or 0) < 0` — including exact zero."""
    storage.ensure_user("alice")
    for index, score in enumerate((-1.0, -0.5, 0.0, 0.5, 1.0)):
        _feedback(storage, "alice", f"cls-{index}", kind="classification", score=score)

    rows = storage.get_feedback_state("alice", feedback_type="classification", limit=100)
    by_python = sum(1 for item in rows if float(item.get("score") or 0) < 0)
    by_sql = storage.count_feedback_state("alice", feedback_type="classification", negative_only=True)
    assert by_sql == by_python == 2


def test_the_reflection_digest_counts_instead_of_paging(storage):
    """Four numbers the owner reads as fact; each used to stop at its own limit."""
    import inspect

    from friday.organs import reflection

    body = inspect.getsource(reflection.build_reflection)
    for measured in (
        "len(storage.list_inbox",
        "len(storage.list_relation_candidates",
        "len(storage.list_knowledge_conflicts",
        "len(storage.list_lifecycle_candidates",
    ):
        assert measured not in body, f"{measured} still measures a page"
    for counted in (
        "count_inbox",
        "count_relation_candidates",
        "count_knowledge_conflicts",
        "count_lifecycle_candidates",
    ):
        assert counted in body, f"{counted} is not used"


def test_the_lifecycle_candidates_route_reports_a_total(settings, storage):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=owner).json()["items"][0]["id"]
        for index in range(6):
            _stale(storage, user_id, f"Устаревшая {index}", importance=0.1)

        page = client.get(f"/api/admin/lifecycle/candidates?user_id={user_id}&limit=2", headers=owner).json()
        assert page["count"] == 2
        assert page["total"] == 6, f"the route reported {page['total']} for 6 candidates"
        assert page["offset"] == 0
