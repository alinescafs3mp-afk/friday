"""Entity duplicate review must not stop at the browse-page ceiling.

``list_entities`` is intentionally a bounded UI/listing surface.  The duplicate
scanner used that surface with ``limit=5000``, so an entity after the alphabetical
boundary could never be compared with one before it.  The synthetic names below
give every filler its own blocking keys; the test measures completeness rather
than spending the pair budget on an artificial duplicate cluster.
"""

from __future__ import annotations

import json

from friday.storage.models import new_id, utc_now


def test_duplicate_scan_reaches_an_entity_after_the_listing_ceiling(storage) -> None:
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    now = utc_now()
    shared_alias = "Общий узел"

    def row(
        name: str,
        aliases: list[str],
        *,
        user_id: str = "alice",
        canonical: int = 1,
        deleted_at: str | None = None,
    ) -> tuple[str | int | None, ...]:
        return (
            new_id("ent"),
            user_id,
            name,
            name.casefold(),
            "project",
            "",
            json.dumps(aliases, ensure_ascii=False),
            "{}",
            1,
            now,
            now,
            canonical,
            deleted_at,
        )

    first = row("AAAAAA", [shared_alias])
    # Six repeats keep each filler out of the short-name exhaustive block, while
    # a distinct CJK character gives it unique token and bigram keys.
    fillers = [row(chr(0x4E00 + index) * 6, []) for index in range(4_999)]
    last = row("\u9fff" * 6, [shared_alias])
    foreign = row("ZZZZZZ", [shared_alias], user_id="bob")
    merged = row("YYYYYY", [shared_alias], canonical=0)
    tombstone = row("XXXXXX", [shared_alias], deleted_at=now)
    with storage.transaction() as conn:
        conn.executemany(
            "INSERT INTO entities(id,user_id,name,normalized_name,entity_type,description,"
            "aliases_json,metadata_json,version,created_at,updated_at,canonical,deleted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [first, *fillers, last, foreign, merged, tombstone],
        )

    assert storage.count_entities("alice") == 5_001
    first_page_ids = {str(item["id"]) for item in storage.list_entities("alice", limit=5_000)}
    assert str(first[0]) in first_page_ids and str(last[0]) not in first_page_ids

    candidates, report = storage.sweep_entity_duplicates("alice", min_confidence=0.99, max_pairs=10)
    pairs = {frozenset((candidate.entity_a_id, candidate.entity_b_id)) for candidate in candidates}

    # Mutation: restoring ``list_entities(user_id, limit=5000)`` in
    # ``_duplicate_pass`` makes the assertion fail because ``last`` is invisible.
    assert report["entities"] == 5_001
    assert report["pairs_examined"] == 1
    assert report["complete"] is True
    assert pairs == {frozenset((str(first[0]), str(last[0])))}
