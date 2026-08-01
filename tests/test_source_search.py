"""Source-text search, and the verdict it must not overturn.

`raw_objects` holds the original ingested characters; the Knowledge Object holds a
normalised, often summarised version. Measured on the owner's database, **93% of
ingested characters** lived only in the former and no index covered them — an exact
phrase from a PDF was unfindable once review had condensed it.

The complication is the reason this file exists. On that same database the Inbox
breakdown is 65 ignored / 1 classified: nearly all of that unreachable text is
material the owner EXPLICITLY REJECTED. DATA_LIFECYCLE §3 makes "игнорировать" a
verdict, and this project has already shipped three separate paths that resurrected
rejected material. Making raw text searchable without honouring the verdict would
repeat that at the largest scale yet.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id

PHRASE = "autovacuum_vacuum_scale_factor"


def _ingest(storage, user_id: str, text: str, *, status: InboxStatus | None) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    if status is not None:
        storage.store_inbox_item(
            InboxItem(id=new_id("inbox"), user_id=user_id, raw_object_id=raw.id, status=status)
        )
    return raw.id


def test_source_text_is_searchable_and_the_verdict_is_obeyed(storage):
    storage.ensure_user("owner")
    pending = _ingest(storage, "owner", f"черновик {PHRASE} на проверке", status=InboxStatus.PENDING)
    classified = _ingest(storage, "owner", f"принято {PHRASE} в работу", status=InboxStatus.CLASSIFIED)
    archived = _ingest(storage, "owner", f"убрано из inbox {PHRASE}", status=InboxStatus.ARCHIVED)
    rejected = _ingest(storage, "owner", f"отвергнуто {PHRASE} совсем", status=InboxStatus.IGNORED)
    orphan = _ingest(storage, "owner", f"без inbox-строки {PHRASE}", status=None)

    found = {item["id"] for item in storage.search_raw_objects("owner", PHRASE, limit=50)}

    # Awaiting a decision, approved, and Inbox-tidied material is reachable.
    assert pending in found
    assert classified in found
    assert archived in found
    assert orphan in found
    # The verdict stands.
    assert rejected not in found, "search resurrected material the reviewer rejected"


def test_a_soft_deleted_source_is_not_reachable(storage):
    storage.ensure_user("owner")
    raw_id = _ingest(storage, "owner", f"будет удалено {PHRASE}", status=InboxStatus.PENDING)
    assert any(item["id"] == raw_id for item in storage.search_raw_objects("owner", PHRASE))

    # No public soft-delete for a Raw Object (purge removes it outright), so mark
    # it the way the column is meant to be used and check the query honours it.
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET deleted_at=? WHERE id=?", ("2026-07-27T00:00:00Z", raw_id))
    assert not any(item["id"] == raw_id for item in storage.search_raw_objects("owner", PHRASE))


def test_source_search_is_tenant_scoped(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    _ingest(storage, "alice", f"личное {PHRASE} алисы", status=InboxStatus.PENDING)
    assert storage.search_raw_objects("alice", PHRASE)
    assert storage.search_raw_objects("bob", PHRASE) == []


def test_the_index_is_only_ever_read_through_the_filtered_helper():
    """Structural, because a forgotten filter is exactly how the previous three went.

    `raw_fts` holds terms derived from EVERY raw object, rejected ones included — a
    deliberate choice, so that returning an ignored item to pending makes it
    reachable again without an index rebuild. The price is that a second query
    against `raw_fts` without the verdict filter would expose rejected material, so
    there must not be one.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).parent.parent / "friday"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "raw_fts" not in source:
            continue
        # The schema declares it; storage/_intake.py is the one reader.
        if path.name in {"_base.py", "_core.py", "_intake.py"}:
            continue
        offenders.append(str(path.relative_to(root)))
    assert not offenders, f"raw_fts is queried outside the filtered helper: {offenders}"

    intake = (root / "storage" / "_intake.py").read_text(encoding="utf-8")
    tree = ast.parse(intake)
    readers = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "raw_fts" in ast.get_source_segment(intake, node)
    ]
    assert readers == ["search_raw_objects"], f"a second reader of raw_fts appeared: {readers}"


def test_the_agent_and_the_retriever_cannot_reach_source_text():
    """Source text is provenance, not recall.

    The place where resurrected material would do the most damage is an agent
    quoting it as fact, so the helper is deliberately absent from HybridSearcher,
    the agent context builder and the tool registry.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent / "friday"
    for relative in ("retrieval/__init__.py", "agent_runtime/__init__.py", "execution_kernel/__init__.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "search_raw_objects" not in source, f"{relative} reached into source text"


def test_source_search_over_http_excludes_rejected_material(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        from friday.permissions import LEGACY_OWNER_USER_ID

        storage.ensure_user(LEGACY_OWNER_USER_ID)
        kept = _ingest(storage, LEGACY_OWNER_USER_ID, f"оставлено {PHRASE}", status=InboxStatus.PENDING)
        _ingest(storage, LEGACY_OWNER_USER_ID, f"отклонено {PHRASE}", status=InboxStatus.IGNORED)

        owner = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.get("/api/knowledge/sources", params={"q": PHRASE}, headers=owner)
        assert response.status_code == 200
        body = response.json()
        assert [item["id"] for item in body["items"]] == [kept]
        assert body["excludes"] == "ignored"

        # Unauthenticated callers get nothing.
        assert client.get("/api/knowledge/sources", params={"q": PHRASE}).status_code == 401


def test_one_rejection_hides_the_source_even_among_several_inbox_rows(storage):
    """A Raw Object can carry SEVERAL Inbox rows, and a join let it through.

    `ingest_text` returns the existing raw object on an idempotent replay while
    still creating a review row, so `raw_object_id` is not unique in `inbox`. The
    first version of this query joined on the row and admitted the object whenever
    any single row was not the rejection — reproduced, and it returned rejected
    text. The test is `NOT EXISTS ... status='ignored'`: any rejection hides it.
    """
    storage.ensure_user("owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id="owner",
        source="upload",
        source_ref=new_id("src"),
        raw_content=f"две строки inbox {PHRASE}",
        content_type="text",
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id="owner", raw_object_id=raw.id, status=InboxStatus.IGNORED)
    )
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id="owner", raw_object_id=raw.id, status=InboxStatus.PENDING)
    )
    assert (
        storage.execute("SELECT COUNT(*) AS c FROM inbox WHERE raw_object_id=?", (raw.id,)).fetchone()["c"]
        == 2
    )

    assert storage.search_raw_objects("owner", PHRASE) == []


def test_the_index_is_rebuilt_over_rows_that_predate_it(settings, tmp_path):
    """An external-content FTS table created over existing rows starts EMPTY.

    The rebuild is guarded on "did this table already exist", and probing that
    AFTER running the DDL always answers yes — so the guard skipped the rebuild and
    left an index that reports rows and matches nothing. Caught only by searching a
    copy of the owner's real database, where every query returned zero.
    """
    from dataclasses import replace

    from friday.storage import FridayStorage

    database = tmp_path / "predates.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    try:
        first.ensure_user("owner")
        _ingest(first, "owner", f"записано до индекса {PHRASE}", status=InboxStatus.PENDING)
        # Drop the index and its triggers: the state a schema-16 database is in.
        with first.transaction() as conn:
            conn.execute("DROP TABLE IF EXISTS raw_fts")
            for name in ("raw_objects_ai", "raw_objects_ad", "raw_objects_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute("UPDATE schema_meta SET value='16' WHERE key='schema_version'")
    finally:
        first.close(final=True)

    migrated = FridayStorage(replace(settings, database_path=database))
    try:
        assert migrated.search_raw_objects("owner", PHRASE), "the index was not rebuilt over existing rows"
    finally:
        migrated.close(final=True)
