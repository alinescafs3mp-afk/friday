"""Storage methods for Knowledge Objects, versions, usage and conflicts.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import unicodedata
import zlib
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from time import monotonic
from typing import cast

from friday.storage._base import (
    _SEARCH_TEXT_LEN_SQL,
    LOGGER,
    UTC,
    Any,
    EntityResolutionCandidate,
    EntityType,
    KnowledgeObject,
    LifecycleStage,
    SequenceMatcher,
    StorageShared,
    _is_entity_identifier,
    _json_load,
    _snapshot,
    datetime,
    hashlib,
    json,
    math,
    new_id,
    normalize_entity_name,
    os,
    pack_snapshot,
    re,
    sqlite3,
    timedelta,
    unpack_snapshot,
    utc_now,
)
from friday.storage._privacy import (
    _exact_uploader_knowledge_dependency,
    _not_audio_document,
    _not_private_entity_material_dependency,
    _not_private_knowledge_dependency,
    _not_private_knowledge_structure_dependency,
    _not_private_raw_dependency,
    _not_private_relation_dependency,
)

# FTS MATCH accepts a bounded number of terms, and a natural-language question is
# mostly function words. The budget is the ceiling; WHICH terms survive it is the
# whole question, and the answer lives in `_fts_terms` below.
#
# Twenty-four, not the previous twelve. Twelve was never measured against a query
# that overflows it: `tools/retrieval_bench.py` has at most seven distinct tokens
# per query, so its branch never ran there and raising the number provably cannot
# move that benchmark. Measured where it does run — 60 questions of sixteen filler
# words plus one term that occurs in exactly one document of a 342-document corpus,
# placed last — the target reaches the top ten 9 times of 60 at twelve terms and
# 12 of 60 at twenty-four. Thirty-six gains nothing further.
#
# Cost is flat: median FTS latency on that corpus is 47 ms at six terms and 55 ms at
# thirty-six, inside run-to-run noise.
#
# The honest size of the win is three questions in sixty, not the twenty-one an
# earlier estimate suggested. The ceiling of 12/60 even with an unlimited budget
# says the real constraint is elsewhere: sixteen generic words drown the specific
# one in bm25 regardless of whether it reaches the index.
_FTS_TERM_BUDGET = 24


# Below this length a name has too few characters for trigram blocking to be
# meaningful, so short names of the same type are compared against each other
# exhaustively. There are few of them and n² over few is nothing.
_SHORT_NAME_CHARS = 6
# Ceiling on evaluated pairs. Four SequenceMatcher calls per surviving pair put
# this at a few seconds — a scan reachable from an HTTP route needs an answer, not
# an eventual one. Reaching it is a WARNING, never silent.
_MAX_DUPLICATE_PAIRS = 200_000
# Evidence strength of the key that introduced a pair, strongest first. Used only
# to decide what the ceiling drops.
_KEY_RANK = {"variant": 0, "token": 1, "acronym": 2, "short": 3, "bigram": 4}
_ENTITY_CARD_TITLE_MAX_CHARS = 240
_ENTITY_CARD_SUMMARY_MAX_CHARS = 500
_ENTITY_CARD_TAGS_MAX_BYTES = 8_192
_ENTITY_CARD_METADATA_MAX_BYTES = 8_192
_ENTITY_SUMMARY_TAG_MAX_CHARS = 120
_ENTITY_SUMMARY_TAG_LIMIT = 100
_KNOWLEDGE_CONFLICT_PAGE_MAX = 501
_KNOWLEDGE_CONFLICT_EVIDENCE_MAX_BYTES = 8_192
_PUBLIC_ENTITY_LINK_PAGE_MAX = 500
_PUBLIC_KNOWLEDGE_VERSION_SNAPSHOT_MAX_BYTES = 1_048_576
_MENTION_VALIDATION_SECRET = os.urandom(32)
_PUBLIC_KNOWLEDGE_VERSION_NESTED_DEPTH = 8
_PUBLIC_KNOWLEDGE_VERSION_TEXT_BUDGET = 4 * 1_048_576

_PUBLIC_ENTITY_LINK_COLUMNS = """substr(l.id,1,160) AS id,
                   substr(l.knowledge_object_id,1,160) AS knowledge_object_id,
                   substr(l.entity_id,1,160) AS entity_id,
                   substr(l.status,1,40) AS status,
                   l.confidence,
                   substr(l.created_at,1,64) AS created_at,
                   substr(COALESCE(l.reviewed_at,''),1,64) AS reviewed_at,
                   substr(e.name,1,240) AS entity_name,
                   substr(e.entity_type,1,80) AS entity_type,
                   substr(k.title,1,240) AS knowledge_title,
                   substr(k.lifecycle_stage,1,80) AS knowledge_lifecycle,
                   CASE WHEN COALESCE(l.evidence_json,'') NOT IN ('', '{}', '[]', 'null')
                        THEN 1 ELSE 0 END AS evidence_present,
                   MIN(length(CAST(COALESCE(l.evidence_json,'') AS BLOB)),1000000000)
                       AS evidence_bytes"""


def _safe_tags_json_expression(alias: str = "k") -> str:
    """A bounded JSON array expression safe to hand to ``json_each``."""

    return (
        f"CASE WHEN length(CAST(COALESCE({alias}.tags_json,'') AS BLOB))"
        f"<={_ENTITY_CARD_TAGS_MAX_BYTES} THEN CASE WHEN json_valid({alias}.tags_json) "
        f"THEN CASE WHEN json_type({alias}.tags_json)='array' "
        f"THEN {alias}.tags_json ELSE '[]' END ELSE '[]' END ELSE '[]' END"
    )


def _snapshot_contains_private_entity_material(
    storage: StorageShared,
    snapshot: Mapping[str, Any],
) -> bool:
    """Inspect decoded and JSON-in-string history without returning private tokens.

    Legacy snapshots contain canonical JSON columns as strings, sometimes encoded
    more than once.  Walking only the outer object leaves ``"metadata_json":
    "{\\"name\\":\\"\\u0418...\\"}"`` opaque.  Each nested decode consumes the
    already-bounded outer text; depth and aggregate text budgets make pathological
    nesting fail closed.
    """

    texts: list[str] = []
    seen_nested: set[str] = set()
    used = 0

    def walk(value: Any, depth: int) -> bool:
        nonlocal used
        if isinstance(value, Mapping):
            return any(walk(str(key), depth) or walk(item, depth) for key, item in value.items())
        if isinstance(value, list):
            return any(walk(item, depth) for item in value)
        if not isinstance(value, str):
            return False
        used += len(value)
        if used > _PUBLIC_KNOWLEDGE_VERSION_TEXT_BUDGET:
            return True
        texts.append(value)
        stripped = value.lstrip()
        if not stripped or stripped[0] not in {"{", "[", '"'} or value in seen_nested:
            return False
        if depth >= _PUBLIC_KNOWLEDGE_VERSION_NESTED_DEPTH:
            return True
        try:
            nested = json.loads(value)
        except (TypeError, ValueError):
            # A JSON-shaped legacy payload which no longer decodes cannot be
            # proven independent from private material.  History is a public
            # projection, so corruption must fail closed rather than reopening
            # an opaque copy of a quarantined fact.
            return True
        seen_nested.add(value)
        return walk(nested, depth + 1)

    if walk(snapshot, 0):
        return True
    haystack = "\0".join(texts)
    folded_haystack = unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", haystack).casefold(),
    )
    rows = storage.execute("SELECT id, name FROM private_entity_material_closure")
    for row in rows:
        entity_id = str(row["id"] or "")
        entity_name = str(row["name"] or "")
        folded_name = unicodedata.normalize(
            "NFC",
            unicodedata.normalize("NFC", entity_name).casefold(),
        )
        if (entity_id and entity_id in haystack) or (folded_name and folded_name in folded_haystack):
            return True
    return False


def _public_knowledge_version_snapshot(
    storage: StorageShared,
    raw_snapshot: Any,
    *,
    user_id: str,
    knowledge_object: Mapping[str, Any],
) -> str | None:
    """Return a readable snapshot only when all structural dependencies stay public."""

    try:
        snapshot_text = unpack_snapshot(raw_snapshot)
    except (TypeError, ValueError, UnicodeError, zlib.error):
        return None
    if not isinstance(snapshot_text, str):
        return None
    if len(snapshot_text.encode("utf-8", errors="replace")) > _PUBLIC_KNOWLEDGE_VERSION_SNAPSHOT_MAX_BYTES:
        return None
    try:
        snapshot = json.loads(snapshot_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None
    identity = (
        ("id", str(knowledge_object.get("id") or "")),
        ("user_id", user_id),
        ("raw_object_id", str(knowledge_object.get("raw_object_id") or "")),
    )
    if any(str(snapshot.get(key) or "") != expected for key, expected in identity):
        return None
    # Historical bodies are copies too. Structural ids alone miss an older body
    # (or nested JSON string) containing a now-private reminder name.
    if _snapshot_contains_private_entity_material(storage, snapshot):
        return None
    entity_id = str(snapshot.get("entity_id") or "")
    if entity_id:
        visible_entity = storage.execute(
            f"""SELECT 1 FROM entities e WHERE e.id=? AND e.user_id=?
                  AND {_not_private_entity_material_dependency("e")} LIMIT 1""",  # nosec B608
            (entity_id, user_id),
        ).fetchone()
        if visible_entity is None:
            return None
    superseded_by = str(snapshot.get("superseded_by_id") or "")
    if superseded_by:
        visible_successor = storage.execute(
            f"""SELECT 1 FROM knowledge_objects successor
                 WHERE successor.id=? AND successor.user_id=?
                   AND {_not_private_knowledge_dependency("successor")} LIMIT 1""",  # nosec B608
            (superseded_by, user_id),
        ).fetchone()
        if visible_successor is None:
            return None
    return snapshot_text


def _bounded_recent_knowledge_title_rows(
    storage: StorageShared,
    user_id: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Small title-only projection for reflection; never materialize document bodies."""

    rows = storage.execute(
        f"""SELECT substr(k.id,1,160) AS id, substr(k.title,1,240) AS title,
                  substr(k.knowledge_kind,1,80) AS knowledge_kind,
                  substr(k.created_at,1,64) AS created_at
             FROM knowledge_objects k
            WHERE k.user_id=? AND k.deleted_at IS NULL
              AND {_not_private_knowledge_dependency("k")}
            ORDER BY k.importance DESC, k.updated_at DESC, k.id DESC LIMIT ?""",  # nosec B608
        (user_id, max(1, min(int(limit), 50))),
    ).fetchall()
    return [dict(row) for row in rows]


def _bounded_public_knowledge_entity_links(
    storage: StorageShared,
    user_id: str,
    knowledge_object_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Public lineage links without reviewer identity or extractor evidence."""

    rows = storage.execute(
        f"""SELECT {_PUBLIC_ENTITY_LINK_COLUMNS}
              FROM knowledge_entity_links l
              JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                   AND {_not_private_entity_material_dependency("e")}
              JOIN knowledge_objects k
                ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                   AND {_not_private_knowledge_dependency("k")}
             WHERE l.user_id=? AND l.knowledge_object_id=?
             ORDER BY CASE l.status
                        WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                      l.confidence DESC, l.created_at DESC, l.id
             LIMIT ?""",  # nosec B608
        (
            user_id,
            knowledge_object_id,
            max(1, min(int(limit), _PUBLIC_ENTITY_LINK_PAGE_MAX)),
        ),
    ).fetchall()
    return [_public_knowledge_entity_link_card(row) for row in rows]


def _public_knowledge_entity_link_card(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    card = {
        key: data[key]
        for key in (
            "id",
            "knowledge_object_id",
            "entity_id",
            "status",
            "confidence",
            "created_at",
            "reviewed_at",
            "entity_name",
            "entity_type",
            "knowledge_title",
            "knowledge_lifecycle",
        )
        if key in data
    }
    raw_confidence = data.get("confidence")
    card["confidence"] = (
        max(0.0, min(float(raw_confidence), 1.0))
        if isinstance(raw_confidence, (int, float))
        and not isinstance(raw_confidence, bool)
        and math.isfinite(float(raw_confidence))
        else 0.0
    )
    card["evidence"] = {
        "present": bool(data.get("evidence_present")),
        "bytes": max(0, min(int(data.get("evidence_bytes") or 0), 1_000_000_000)),
    }
    return card


def _bounded_public_knowledge_entity_link_by_id(
    storage: StorageShared,
    user_id: str,
    link_id: str,
) -> dict[str, Any] | None:
    row = storage.execute(
        f"""SELECT {_PUBLIC_ENTITY_LINK_COLUMNS}
              FROM knowledge_entity_links l
              JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                   AND {_not_private_entity_material_dependency("e")}
              JOIN knowledge_objects k
                ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                   AND {_not_private_knowledge_dependency("k")}
             WHERE l.user_id=? AND l.id=? LIMIT 1""",  # nosec B608
        (user_id, link_id),
    ).fetchone()
    return _public_knowledge_entity_link_card(row) if row else None


def _bounded_knowledge_conflict_rows(
    storage: StorageShared,
    user_id: str,
    *,
    status: str | None = "suggested",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Conflict review cards with structural, never verbatim, evidence."""

    clauses = ["c.user_id=?"]
    params: list[Any] = [user_id]
    if status:
        if status not in {"suggested", "confirmed", "dismissed", "resolved"}:
            raise ValueError("Invalid conflict status")
        clauses.append("c.status=?")
        params.append(status)
    bounded = max(1, min(int(limit), _KNOWLEDGE_CONFLICT_PAGE_MAX))
    params.extend((bounded, max(0, int(offset))))
    rows = storage.execute(
        f"""SELECT substr(c.id, 1, 160) AS id,
                   substr(c.knowledge_a_id, 1, 160) AS knowledge_a_id,
                   substr(c.knowledge_b_id, 1, 160) AS knowledge_b_id,
                   substr(c.conflict_type, 1, 80) AS conflict_type,
                   c.confidence,
                   substr(c.status, 1, 40) AS status,
                   substr(c.created_at, 1, 64) AS created_at,
                   substr(COALESCE(c.reviewed_at, ''), 1, 64) AS reviewed_at,
                   substr(a.title, 1, 240) AS knowledge_a_title,
                   substr(a.summary, 1, 500) AS knowledge_a_summary,
                   substr(a.lifecycle_stage, 1, 80) AS knowledge_a_stage,
                   substr(COALESCE(a.superseded_by_id, ''), 1, 160)
                       AS knowledge_a_superseded_by,
                   substr(b.title, 1, 240) AS knowledge_b_title,
                   substr(b.summary, 1, 500) AS knowledge_b_summary,
                   substr(b.lifecycle_stage, 1, 80) AS knowledge_b_stage,
                   substr(COALESCE(b.superseded_by_id, ''), 1, 160)
                       AS knowledge_b_superseded_by,
                   CASE WHEN COALESCE(c.evidence_json, '') NOT IN ('', '{{}}', '[]', 'null')
                        THEN 1 ELSE 0 END AS evidence_present,
                   MIN(length(CAST(COALESCE(c.evidence_json, '') AS BLOB)), 1000000000)
                       AS evidence_bytes,
                   MIN(length(COALESCE(c.resolution_note, '')), 1000000000)
                       AS resolution_note_chars
              FROM knowledge_conflicts c
              JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                   AND a.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("a")}
              JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
                   AND b.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("b")}
             WHERE {" AND ".join(clauses)}
             ORDER BY c.confidence DESC, c.created_at DESC, c.id
             LIMIT ? OFFSET ?""",  # nosec B608
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _bounded_knowledge_conflict_by_id(
    storage: StorageShared,
    user_id: str,
    conflict_id: str,
) -> dict[str, Any] | None:
    """One tenant-scoped conflict card for decision preconditions."""

    row = storage.execute(
        f"""SELECT substr(c.id, 1, 160) AS id,
                  substr(c.knowledge_a_id, 1, 160) AS knowledge_a_id,
                  substr(c.knowledge_b_id, 1, 160) AS knowledge_b_id,
                  substr(c.conflict_type, 1, 80) AS conflict_type,
                  c.confidence, substr(c.status, 1, 40) AS status,
                  substr(c.created_at, 1, 64) AS created_at,
                  substr(COALESCE(c.reviewed_at, ''), 1, 64) AS reviewed_at,
                  substr(a.title, 1, 240) AS knowledge_a_title,
                  substr(a.summary, 1, 500) AS knowledge_a_summary,
                  substr(a.lifecycle_stage, 1, 80) AS knowledge_a_stage,
                  substr(COALESCE(a.superseded_by_id, ''), 1, 160)
                      AS knowledge_a_superseded_by,
                  substr(b.title, 1, 240) AS knowledge_b_title,
                  substr(b.summary, 1, 500) AS knowledge_b_summary,
                  substr(b.lifecycle_stage, 1, 80) AS knowledge_b_stage,
                  substr(COALESCE(b.superseded_by_id, ''), 1, 160)
                      AS knowledge_b_superseded_by,
                  CASE WHEN COALESCE(c.evidence_json, '') NOT IN ('', '{{}}', '[]', 'null')
                       THEN 1 ELSE 0 END AS evidence_present,
                  MIN(length(CAST(COALESCE(c.evidence_json, '') AS BLOB)), 1000000000)
                      AS evidence_bytes,
                  MIN(length(COALESCE(c.resolution_note, '')), 1000000000)
                      AS resolution_note_chars
             FROM knowledge_conflicts c
             JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                  AND a.deleted_at IS NULL
                  AND {_not_private_knowledge_dependency("a")}
             JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
                  AND b.deleted_at IS NULL
                  AND {_not_private_knowledge_dependency("b")}
            WHERE c.user_id=? AND c.id=? LIMIT 1""",  # nosec B608
        (user_id, conflict_id),
    ).fetchone()
    return dict(row) if row else None


def _ratio_ceiling(left: str, right: str, left_counts: Counter[str], right_counts: Counter[str]) -> float:
    """Highest ``SequenceMatcher.ratio()`` these two strings can reach. Exact.

    ``ratio()`` is ``2·M/(len(a)+len(b))``, and the matched characters form a common
    subsequence, so ``M`` is bounded twice over: by the shorter string, and by the
    size of the two strings' character multiset intersection. The second bound is
    the one that pays — same-length names defeat the first entirely, while the
    multiset bound prunes them for a third of what one ``SequenceMatcher`` call
    costs. Both are upper bounds, so nothing that could have qualified is skipped.
    """
    total = len(left) + len(right)
    if not total:
        return 0.0
    shared = sum((left_counts & right_counts).values())
    return 2.0 * min(len(left), len(right), shared) / total


def _blocking_keys(entity_type: str, variants: Sequence[str]) -> set[tuple[str, ...]]:
    """Cheap keys such that any pair that could score ≥ ~0.5 shares at least one.

    Derived from the scoring below rather than guessed. Ignoring the ≤0.14 context
    boost, which cannot carry a pair on its own, a candidate needs one of:

    * a shared normalized variant           → ``exact_alias`` (0.995)
    * an identical token set                → 0.94, and it implies a shared token
    * ``token_jaccard ≥ 0.40``              → a shared token
    * matching acronyms                     → 0.82
    * ``name_similarity ≥ 0.51`` and friends → half the shorter name's characters
      match in order, which for a name of six characters or more forces a shared
      character trigram.

    The last one is the only approximation, and it is bounded: names under
    ``_SHORT_NAME_CHARS`` skip trigrams and land in one exhaustive per-type bucket.
    """
    keys: set[tuple[str, ...]] = set()
    for variant in variants:
        keys.add(("variant", entity_type, variant))
        for token in variant.split():
            keys.add(("token", entity_type, token))
    name = variants[0] if variants else ""
    tokens = [token for token in name.split() if token]
    if len(tokens) >= 2:
        keys.add(("acronym", entity_type, "".join(token[0] for token in tokens).casefold()))
    compact = name.replace(" ", "")
    if len(compact) < _SHORT_NAME_CHARS:
        # Too few characters for an n-gram to mean anything; these all meet.
        keys.add(("short", entity_type))
    for offset in range(len(compact) - 1):
        # Bigrams, not trigrams — measured, not assumed. Trigrams lost 2% of the
        # exhaustive scan's proposals: «Орион» vs «Орион2 1» (short and long names
        # landed in disjoint bucket families) and «ООО 24» vs «ОСОО 40» (similar
        # strings sharing no three consecutive characters). Bigrams recover every
        # one of those, and short names keep their exhaustive bucket *as well as*
        # their bigrams so they still meet longer neighbours.
        keys.add(("bigram", entity_type, compact[offset : offset + 2]))
    return keys


def _score_or(value: Any, default: float = 0.5) -> float:
    """A stored score, clamped — where MISSING and ZERO are not the same thing.

    `float(value or 0.5)` reads a stored 0.0 as 0.5 because zero is falsy, so the one
    value that should weigh most toward lifecycle review was the one the scan skipped.
    """
    if value is None:
        return default
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _lifecycle_protection_reasons(item: dict[str, Any], days_threshold: int) -> list[str]:
    """Why this object must not be archived automatically. Empty means it may be.

    Extracted so the SELECTIVE archive answers to exactly the same rules the
    read-only candidate scan does. They used to disagree: `list_lifecycle_candidates`
    protected file-derived, explicitly saved, positively rated and recently used
    knowledge, and `deprecate_stale_knowledge` archived on `importance < 0.3` alone,
    with none of it.
    """
    metadata = _json_load(item.get("metadata_json"), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    assessment = metadata.get("promotion_assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    reasons: list[str] = []
    if item.get("content_type") == "file" or metadata.get("source_filename"):
        reasons.append("file-derived knowledge")
    if assessment.get("reason") in {"explicit save intent", "human review"}:
        reasons.append("explicitly saved or reviewed")
    if int(item.get("positive_feedback_count") or 0) > int(item.get("negative_feedback_count") or 0):
        reasons.append("positive user feedback")
    recent_cutoff = datetime.now(UTC) - timedelta(days=max(7, int(days_threshold) // 3))
    for field, label in (
        ("last_used_at", "recently used in an answer"),
        ("last_retrieved_at", "recently retrieved"),
    ):
        timestamp = str(item.get(field) or "")
        if not timestamp:
            continue
        with suppress(ValueError):
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed >= recent_cutoff:
                reasons.append(label)
    return reasons


def _valid_lifecycle_stage(value: Any) -> str:
    """Reject a lifecycle stage that is not one of the four.

    The DDL constrains importance, quality_score and promotion_score with CHECK, but
    not this column, and ``update_knowledge_fields`` passed whatever PATCH supplied
    straight through. Both ``"Active"`` (wrong case) and ``"totally-bogus"``
    persisted: ``get_lifecycle_stats`` then reported a stage nobody defined, and the
    object matched no lifecycle filter, so it fell out of every governance scan while
    still answering searches. A typo in one request quietly removed an object from
    oversight.
    """
    stage = str(getattr(value, "value", value) or "").strip().casefold()
    allowed = {item.value for item in LifecycleStage}
    if stage not in allowed:
        raise ValueError(f"lifecycle_stage must be one of {sorted(allowed)}")
    return stage


# Both directions matter, and only one of them is free here.
#
# Query written with `ё`, document with `е`: folding the term covers it, one extra
# alternative on the words the person actually typed that way. That is the case
# measured on the owner's own data.
#
# Query with `е`, document with `ё`: the index holds the text as it was written, so
# reaching it from here would mean GUESSING where the `ё` goes — `кластер` becomes
# `кластёр` on every Russian query, doubling the term list with words nobody wrote.
# Folding the indexed payload in the triggers would be the real fix, and it is a
# trap: FTS5's own `rebuild` re-reads the content table and would silently restore
# the unfolded text, leaving an index that disagrees with its own writer.
#
# It is also the less urgent direction, because FTS is one recall leg of several:
# `lexical_vector` folds both sides through `tokens_of`, and embeddings never saw
# the letter. Only this leg is spelling-bound.
def _yo_spellings(token: str) -> list[str]:
    """The spellings of one word to ask the index for, folded form last."""
    from friday.retrieval import _YO_FOLD

    folded = token.translate(_YO_FOLD)
    return [token] if folded == token else [token, folded]


def _fts_terms(text: str) -> list[str]:
    """Spend the term budget on the words that select a document, not the first typed.

    The rule was ``re.findall(...)[:12]`` over the raw text, so a question longer
    than twelve tokens lost its tail — and a Russian question front-loads «как»,
    «почему», «в», «на», words every document contains, while the identifier that
    actually names the answer comes last. A 14-term question containing
    ``autovacuum_vacuum_scale_factor`` never got that term to the index at all.

    Stopwords are dropped **only when the query is over budget**, and that
    restraint is measured, not stylistic: dropping them unconditionally moved
    ``tools/retrieval_bench.py`` from **0.583 to 0.458** (paraphrase 0.50→0.17,
    synonym 0.40→0.20). At this stage FTS is a recall stage, and for a paraphrase
    the common words are the *only* lexical bridge to the document. So a query
    within budget keeps every token it had; only one that must lose something
    loses the cheap words instead of the specific one. Text order is preserved —
    reordering by length scored the same 0.458 and buys nothing.

    Tokenisation goes through ``retrieval.tokens_of``: the fifth site still
    rolling its own regex, and the one that made a sentence-final identifier
    (``…scale_factor.``) a different string from the same identifier in a query.
    """
    from friday.morphology import LEXICAL_MIN_STEM_INPUT, stem
    from friday.retrieval import _STOPWORDS, tokens_of

    # Unfolded on purpose: the index stored the text as it was written, so the query
    # has to reach BOTH spellings. `tokens_of` folds `ё` for scoring, which is
    # symmetric because it runs over query and document alike; FTS is the one place
    # where only the query passes through us. Terms are OR-ed by the caller, so a
    # second spelling costs one more alternative and nothing else.
    unique = list(dict.fromkeys(token for token in tokens_of(text, fold_yo=False) if len(token) >= 2))
    if len(unique) <= _FTS_TERM_BUDGET:
        chosen = unique
    else:
        chosen = [token for token in unique if token.casefold() not in _STOPWORDS][:_FTS_TERM_BUDGET]
        if len(chosen) < _FTS_TERM_BUDGET:
            # A long query that is mostly stopwords still gets a full budget.
            taken = set(chosen)
            chosen += [token for token in unique if token not in taken][: _FTS_TERM_BUDGET - len(chosen)]
    # Spellings are added AFTER the budget so a variant never costs a distinct word
    # its slot: the budget counts words, and `чёрных`/`черных` are one word.
    expanded: list[str] = []
    for token in chosen:
        # Слово ЗАМЕНЯЕТСЯ основой с префиксным оператором, а не дополняется ею:
        # бюджет считает слова, и добавление удваивало список.
        #
        # Индекс хранит текст как он написан, а вопрос задают в другом падеже.
        # Замерено на боевом корпусе: «что сказано в акте №77?» не находил
        # только что принятый документ НИ НА КАКОЙ позиции — в документе слово
        # «акт», в вопросе «акте», и до пула кандидатов документ не доходил
        # вовсе. «акт*» покрывает обе формы сразу. Это стадия recall: лишнее
        # отсеет вес, а пропущенного не вернёт уже никто. На золотом наборе из
        # 78 эталонов recall@10 не изменился (0.7179), MRR вырос 0.4283 → 0.4293.
        # Основа строится для КАЖДОГО написания. Индекс хранит написанное: если
        # в документе «чёрных», а основу взять только от «черных», префикс
        # «черн*» его не найдёт — ровно та поломка, ради которой ё-варианты
        # здесь и появились.
        roots: list[str] = []
        for spelling in _yo_spellings(token):
            folded = spelling.casefold()
            root = stem(folded.replace("ё", "е"), LEXICAL_MIN_STEM_INPUT)
            if len(root) < 3 or root == folded:
                roots = []
                break
            # Основа считается на «е»-написании (стеммер знает только его), а
            # искать надо в том написании, которое пришло.
            roots.append(root if "ё" not in folded else _restore_yo(folded, root))
        if roots:
            expanded.extend(f"{root}*" for root in dict.fromkeys(roots))
            continue
        expanded.extend(_yo_spellings(token))
    return list(dict.fromkeys(expanded))


def _restore_yo(word: str, root: str) -> str:
    """Основа в том написании, в котором пришло слово.

    Стеммер знает только «е», поэтому основа считается на приведённой форме, а
    искать надо в исходной: документ с «чёрных» не найдётся по префиксу «черн*».
    """
    return word[: len(root)] if len(word) >= len(root) else root


def _json_dict_safe(value: Any) -> dict[str, Any]:
    """Словарь из поля, которое в снимке может быть и строкой, и словарём."""
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _aliases_of(entity: dict[str, Any]) -> list[str]:
    """Псевдонимы сущности из JSON-поля; битое значение — не повод падать в обходе."""
    raw = entity.get("aliases_json")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _is_declared_person(entity: dict[str, Any]) -> bool:
    """Человек, чьё имя было ОБЪЯВЛЕНО отчеством, а не угадано по форме слова.

    Отдельной функцией, а не выражением по месту: её мутирует тест, и она же отвечает
    в одном месте на вопрос «почему этот узел не предлагают сливать». Обоснование и
    числа — у единственного её вызова в `find_duplicate_candidates`.
    """
    if str(entity.get("entity_type") or "") != EntityType.PERSON.value:
        return False
    metadata = entity.get("metadata_json")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return False
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("extraction_method") or "") == "explicit_person_patronymic"


_NUMBER_RE = re.compile(r"\d+")
# Окончания русских отчеств. Совпадение по ним ничего не говорит о тождестве:
# у двух разных людей отчество совпадает сплошь и рядом.
_PATRONYMIC_RE = re.compile(r"(ович|евич|ьевич|овна|евна|ична|инична|оглы|кызы)$", re.I)


class KnowledgeMixin(StorageShared):
    def get_knowledge_by_raw(self, raw_id: str, user_id: str) -> dict[str, Any] | None:
        # The LIVE Knowledge Object for a Raw Object: soft-deleted rows (e.g. a
        # promotion-race loser's orphan, or an ignored KO) are excluded so callers
        # never adopt a hidden object as the canonical one.
        row = self.execute(
            "SELECT k.* FROM knowledge_objects k "
            "WHERE k.raw_object_id=? AND k.user_id=? AND k.deleted_at IS NULL "
            f"AND {_not_private_knowledge_dependency('k')} "  # nosec B608
            "ORDER BY k.version DESC LIMIT 1",
            (raw_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _ko_snapshot(obj: KnowledgeObject | dict[str, Any]) -> dict[str, Any]:
        return obj.to_row() if isinstance(obj, KnowledgeObject) else dict(obj)

    # Сколько последних версий объекта хранится полным текстом. Откат и diff
    # почти всегда смотрят на свежие; старшие сжимаются на месте, при записи
    # НОВОЙ версии этого же объекта — локально, без глобального обхода, поэтому
    # массовое ре-обогащение уплотняет свой хвост само по мере работы.
    _VERSIONS_KEEP_FULL = 3

    def _store_ko_version(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO knowledge_object_versions
               (id, user_id, knowledge_object_id, version, snapshot_json, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                new_id("kov"),
                row["user_id"],
                row["id"],
                int(row.get("version", 1)),
                _snapshot(row),
                utc_now(),
            ),
        )
        # `typeof(...)='text'` отбирает ещё не сжатые; LIMIT -1 OFFSET N — все
        # строки за пределами N новейших. В установившемся режиме здесь одна
        # строка на правку.
        stale = conn.execute(
            """SELECT id, snapshot_json FROM knowledge_object_versions
               WHERE knowledge_object_id=? AND user_id=? AND typeof(snapshot_json)='text'
               ORDER BY version DESC LIMIT -1 OFFSET ?""",
            (row["id"], row["user_id"], self._VERSIONS_KEEP_FULL),
        ).fetchall()
        for old in stale:
            conn.execute(
                "UPDATE knowledge_object_versions SET snapshot_json=? WHERE id=?",
                (pack_snapshot(str(old["snapshot_json"])), old["id"]),
            )

    def store_knowledge_object(self, obj: KnowledgeObject) -> KnowledgeObject:
        self.ensure_user(obj.user_id)
        raw = self.get_raw_object(obj.raw_object_id, obj.user_id)
        if not raw:
            raise ValueError("KnowledgeObject requires a RawObject owned by the same user")
        row = obj.to_row()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_objects(id, user_id, raw_object_id, entity_id, content,
                   content_type, title, summary, tags_json, metadata_json, knowledge_kind,
                   importance, quality_score, promotion_score, lifecycle_stage, version,
                   superseded_by_id, created_at, updated_at, deleted_at)
                   VALUES(:id, :user_id, :raw_object_id, :entity_id, :content,
                   :content_type, :title, :summary, :tags_json, :metadata_json, :knowledge_kind,
                   :importance, :quality_score, :promotion_score, :lifecycle_stage, :version,
                   :superseded_by_id, :created_at, :updated_at, :deleted_at)""",
                row,
            )
            visible = conn.execute(
                f"""SELECT 1 FROM knowledge_objects k WHERE k.id=? AND k.user_id=?
                      AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (obj.id, obj.user_id),
            ).fetchone()
            if visible is None:
                raise ValueError("Knowledge object fields reference private graph material")
            self._store_ko_version(conn, row)
        return obj

    def update_knowledge_object(self, obj: KnowledgeObject) -> KnowledgeObject:
        # The version is READ INSIDE the transaction that writes it. Reading first
        # and locking afterwards let two editors both see version 1, both compute 2,
        # and the loser's UPDATE vanish — along with its snapshot, since
        # `_store_ko_version` is INSERT OR IGNORE on (object, version).
        with self.transaction() as conn:
            existing_row = conn.execute(
                f"""SELECT k.* FROM knowledge_objects k
                     WHERE k.id=? AND k.user_id=?
                       AND {_not_private_knowledge_structure_dependency("k")}""",  # nosec B608
                (obj.id, obj.user_id),
            ).fetchone()
            existing = dict(existing_row) if existing_row else None
            if existing is None:
                raise ValueError("Knowledge object not found for user")
            obj.version = max(int(existing.get("version", 1)) + 1, int(obj.version))
            obj.updated_at = utc_now()
            row = obj.to_row()
            conn.execute(
                """UPDATE knowledge_objects SET entity_id=:entity_id, content=:content,
                   content_type=:content_type, title=:title, summary=:summary, tags_json=:tags_json,
                   metadata_json=:metadata_json, knowledge_kind=:knowledge_kind,
                   importance=:importance, quality_score=:quality_score,
                   promotion_score=:promotion_score, lifecycle_stage=:lifecycle_stage, version=:version,
                   superseded_by_id=:superseded_by_id, updated_at=:updated_at, deleted_at=:deleted_at
                   WHERE id=:id AND user_id=:user_id""",
                row,
            )
            visible = conn.execute(
                f"""SELECT 1 FROM knowledge_objects k WHERE k.id=? AND k.user_id=?
                      AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (obj.id, obj.user_id),
            ).fetchone()
            if visible is None:
                raise ValueError("Knowledge object fields reference private graph material")
            self._store_ko_version(conn, row)
        return obj

    def knowledge_missing_document_date(
        self, *, user_id: str | None = None, limit: int = 500, after_rowid: int = 0
    ) -> list[dict[str, Any]]:
        """Объекты из файлов, у которых собственной даты документа ещё нет.

        Нужен разовый проход: дату из провенанса файла начали снимать при приёме,
        а корпус уже загружен — у владельца 1537 объектов с датой создания «день
        импорта» и без собственной. Файлы лежат content-addressed и никуда не
        делись, поэтому дату можно достать, не трогая сами документы.

        Отдаётся только то, что нужно проходу: идентификатор, арендатор и путь к
        файлу. Тела не читаются — обход по всему корпусу с `content` уже однажды
        стоил 45 МБ на страницу из пятидесяти строк.

        КУРСОР `after_rowid` — не удобство, а условие завершимости. Объект, у
        которого даты в файле нет, остаётся «без даты» навсегда, поэтому выборка
        «первые N без даты» возвращает его снова и снова: проход, дошедший до
        такой пачки, видел одних и тех же и останавливался, считая, что корпус
        кончился. Повторный запуск начинал с той же головы. Курсор по `rowid`
        (не LIMIT/OFFSET: `id` здесь uuid4, порядок по нему случаен) делает
        страницы непересекающимися — тот же приём, что у `knowledge_bodies_after`.
        """
        clauses = [
            "k.deleted_at IS NULL",
            _not_private_knowledge_dependency("k"),
            "json_extract(k.metadata_json,'$.document_date') IS NULL",
            "r.content_type='file'",
            "json_extract(r.metadata_json,'$.stored_path') IS NOT NULL",
        ]
        params: list[Any] = []
        if after_rowid:
            clauses.append("k.rowid > ?")
            params.append(int(after_rowid))
        if user_id:
            clauses.append("k.user_id=?")
            params.append(user_id)
        params.append(max(1, min(int(limit), 5000)))
        rows = self.execute(
            "SELECT k.rowid AS position, k.id AS id, k.user_id AS user_id, "
            "json_extract(r.metadata_json,'$.stored_path') AS stored_path "
            "FROM knowledge_objects k JOIN raw_objects r ON r.id=k.raw_object_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY k.rowid LIMIT ?",  # nosec B608 - фиксированные условия
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def knowledge_bodies_after(
        self, *, after_rowid: int = 0, user_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Тела знаний страницами по возрастанию `rowid` — для разовых проходов по корпусу.

        Курсор именно по `rowid`, а не по `LIMIT/OFFSET`: страницы должны быть
        непересекающимися и не терять строк, а `id` в этой схеме — `uuid4`, то есть
        сортировка по нему случайна. Ту же ошибку уже ловили в проекте: хвост
        сортировки по случайному идентификатору сделал недетерминированным состав
        пачки, и тест замигал.

        Тело здесь брать ПРИХОДИТСЯ — проход ищет в самом тексте, — поэтому страница
        маленькая по умолчанию. Обход всего корпуса с `SELECT k.*` однажды стоил
        45 МБ на пятьдесят строк; тут выбираются три поля из нужных, а не звёздочка.
        """
        clauses = [
            "k.deleted_at IS NULL",
            "k.rowid > ?",
            _not_private_knowledge_dependency("k"),
        ]
        params: list[Any] = [max(0, int(after_rowid))]
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        params.append(max(1, min(int(limit), 1000)))
        rows = self.execute(
            # `summary` и `knowledge_kind` добавлены сюда потому, что их УЖЕ
            # искал потребитель: `_matches` в органе мониторов складывает
            # haystack из пяти полей, а выборка отдавала три — два молча были
            # пустыми, и монитор со словом, стоящим только в кратком содержании,
            # не срабатывал ни разу. Совпадение там требует ВСЕ слова запроса,
            # так что одно недостающее поле гасит правило целиком.
            #
            # Оба поля короткие, тело документа рядом на порядки больше — цена
            # страницы не меняется.
            "SELECT k.rowid AS rowid, k.id AS id, k.user_id AS user_id, k.title AS title, "
            "k.summary AS summary, k.knowledge_kind AS knowledge_kind, "
            "k.tags_json AS tags_json, k.content AS content "
            f"FROM knowledge_objects k WHERE {' AND '.join(clauses)} "  # nosec B608 - fixed clauses
            "ORDER BY k.rowid LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def decided_entity_links(self, user_id: str, ko_id: str) -> set[str]:
        """Сущности, привязка которых к этому знанию УЖЕ решена человеком.

        `link_knowledge_entity` перезаписывает статус по `ON CONFLICT`, поэтому
        разовый проход, идущий по всему корпусу, молча вернул бы отклонённой
        человеком связи статус `accepted`. Решение человека — вершина в этой
        системе; проход обязан такие пары обходить, а не «освежать».
        """
        rows = self.execute(
            "SELECT entity_id FROM knowledge_entity_links "
            "WHERE user_id=? AND knowledge_object_id=? "
            "AND (reviewed_by IS NOT NULL OR status='rejected')",
            (user_id, ko_id),
        ).fetchall()
        return {str(row["entity_id"]) for row in rows}

    def entity_links_touched_by_a_person(self, entity_id: str, user_id: str) -> bool:
        """Смотрел ли человек хоть одну привязку этого узла.

        Тот же вопрос, что у `decided_entity_links`, но с другой стороны — не «какие
        сущности решены у этого документа», а «трогали ли этот узел вообще». Нужен
        чистке графа: снести узел, про который человек уже высказался, значит стереть
        его решение молча.
        """
        row = self.execute(
            "SELECT 1 FROM knowledge_entity_links "
            "WHERE user_id=? AND entity_id=? "
            "AND (reviewed_by IS NOT NULL OR status='rejected') LIMIT 1",
            (user_id, entity_id),
        ).fetchone()
        return row is not None

    def set_document_date(self, ko_id: str, user_id: str, document_date: str) -> bool:
        """Записать собственную дату документа в метаданные, не создавая версию.

        Намеренно НЕ через `update_knowledge_fields`: это не правка знания, а
        дозапись провенанса, который был утрачен при приёме. Версия здесь означала
        бы, что человек что-то менял, и засорила бы историю правок на полутора
        тысячах объектов разом.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE knowledge_objects AS k SET metadata_json="
                "json_set(COALESCE(metadata_json,'{}'), '$.document_date', ?) "
                f"WHERE id=? AND user_id=? AND deleted_at IS NULL "
                f"AND {_not_private_knowledge_dependency('k')}",  # nosec B608
                (document_date, ko_id, user_id),
            )
        return bool(cursor.rowcount)

    def update_knowledge_fields(self, ko_id: str, user_id: str, **fields: Any) -> dict[str, Any] | None:
        """Merge ``fields`` into a Knowledge Object and version the result.

        Read, merge and write happen inside ONE transaction. They used to be three
        separate steps with the lock taken only for the last one, which is a
        read-modify-write race in the plainest form: two editors both read version 1,
        both compute 2, and the second UPDATE overwrites the first. The snapshot is
        lost with it, because ``_store_ko_version`` is ``INSERT OR IGNORE`` on
        ``(knowledge_object_id, version)`` and the duplicate version is dropped in
        silence. Reproduced with six concurrent edits: final version **3 instead of
        7**, three snapshots instead of seven, no error raised anywhere — four edits
        and their history simply gone.

        ``transaction()`` is reentrant on the same thread, so the nested
        ``update_knowledge_object`` below does not deadlock.
        """
        with self.transaction() as conn:
            current_row = conn.execute(
                f"""SELECT k.* FROM knowledge_objects k
                     WHERE k.id=? AND k.user_id=?
                       AND {_not_private_knowledge_structure_dependency("k")}""",  # nosec B608
                (ko_id, user_id),
            ).fetchone()
            current = dict(current_row) if current_row else None
            if current is None:
                return None
            tags = fields.get("tags_json", _json_load(current.get("tags_json"), []))
            metadata = fields.get("metadata_json", _json_load(current.get("metadata_json"), {}))
            obj = KnowledgeObject(
                id=current["id"],
                user_id=current["user_id"],
                raw_object_id=current["raw_object_id"],
                entity_id=fields.get("entity_id", current.get("entity_id")),
                content=cast(str, fields.get("content", current.get("content", ""))),
                content_type=cast(str, fields.get("content_type", current.get("content_type", ""))),
                title=cast(str, fields.get("title", current.get("title", ""))),
                summary=cast(str, fields.get("summary", current.get("summary", ""))),
                tags_json=tags if isinstance(tags, list) else _json_load(tags, []),
                metadata_json=metadata if isinstance(metadata, dict) else _json_load(metadata, {}),
                knowledge_kind=str(fields.get("knowledge_kind", current.get("knowledge_kind", "note"))),
                importance=float(cast(Any, fields.get("importance", current.get("importance", 0.5)))),
                quality_score=float(
                    cast(Any, fields.get("quality_score", current.get("quality_score", 0.5)))
                ),
                promotion_score=float(
                    cast(Any, fields.get("promotion_score", current.get("promotion_score", 0.5)))
                ),
                lifecycle_stage=_valid_lifecycle_stage(
                    fields.get("lifecycle_stage", current.get("lifecycle_stage", "active"))
                ),
                version=int(current.get("version", 1)),
                superseded_by_id=fields.get("superseded_by_id", current.get("superseded_by_id")),
                created_at=current.get("created_at", utc_now()),
                updated_at=current.get("updated_at", utc_now()),
                deleted_at=fields.get("deleted_at", current.get("deleted_at")),
            )
            self.update_knowledge_object(obj)
            return self.get_knowledge_object(ko_id, user_id)

    def get_knowledge_object(
        self,
        ko_id: str,
        user_id: str | None = None,
        *,
        uploaded_by: str | None = None,
    ) -> dict[str, Any] | None:
        scope = ""
        scope_params: tuple[str, ...] = ()
        if uploaded_by is not None:
            if not str(uploaded_by).strip():
                return None
            scope = f" AND {_exact_uploader_knowledge_dependency('k')}"
            scope_params = (str(uploaded_by),)
        if user_id is None:
            row = self.execute(
                f"""SELECT k.* FROM knowledge_objects k WHERE k.id=?
                      AND {_not_private_knowledge_dependency("k")}
                      {scope}""",  # nosec B608
                (ko_id, *scope_params),
            ).fetchone()
        else:
            row = self.execute(
                f"""SELECT k.* FROM knowledge_objects k
                     WHERE k.id=? AND k.user_id=?
                       AND {_not_private_knowledge_dependency("k")}
                       {scope}""",  # nosec B608
                (ko_id, user_id, *scope_params),
            ).fetchone()
        return dict(row) if row else None

    def list_knowledge_versions(self, ko_id: str, user_id: str) -> list[dict[str, Any]]:
        knowledge_object = self.get_knowledge_object(ko_id, user_id)
        if knowledge_object is None:
            return []
        rows = self.execute(
            """SELECT * FROM knowledge_object_versions
               WHERE knowledge_object_id=? AND user_id=? ORDER BY version DESC""",
            (ko_id, user_id),
        ).fetchall()
        versions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            # Снимок распаковывается в ЕДИНСТВЕННОМ читателе таблицы: restore,
            # diff, admin API и его экран видят прежний текст независимо от
            # того, сжат ли хвост версии на диске.
            snapshot = _public_knowledge_version_snapshot(
                self,
                item.get("snapshot_json"),
                user_id=user_id,
                knowledge_object=knowledge_object,
            )
            if snapshot is None:
                continue
            item["snapshot_json"] = snapshot
            versions.append(item)
        return versions

    # Поля, которые снимок возвращает. Всё остальное в строке — либо тождество
    # (`id`, `user_id`, `raw_object_id`), либо счётчики жизненного цикла, которые
    # откат менять не должен: возврат к прежнему ТЕКСТУ не отменяет того, что объект
    # с тех пор архивировали или связывали с сущностью.
    _RESTORABLE_FIELDS = (
        "title",
        "summary",
        "content",
        "content_type",
        "tags_json",
        "metadata_json",
        "knowledge_kind",
        "importance",
    )

    def restore_knowledge_version(
        self, ko_id: str, user_id: str, version: int, *, reviewed_by: str | None = None
    ) -> dict[str, Any] | None:
        """Вернуть объект к состоянию из снимка. Это НОВАЯ версия, а не перемотка.

        Версии писались и показывались, а вернуться к ним было нечем: поиск по всему
        пакету (`restore|revert|rollback`) находил только восстановление БАЗЫ из
        бэкапа. При этом машинерия уже была вся — снимок это готовая строка объекта.

        Откат идёт через обычную правку, поэтому создаёт версию N+1 и ничего не
        теряет: если человек откатился по ошибке, он может откатиться обратно. Именно
        так, а не удалением версий: история — это то, ради чего она пишется.

        Живая база показывает, насколько путь правки не хожен: 1538 строк версий на
        1537 объектов, то есть за всё время отредактирован ровно один объект. Первая
        же настоящая ошибка владельца упёрлась бы в отсутствие отката — а редактор
        содержимого в админке это одна textarea с полным текстом документа, в среднем
        на 16.5 тысяч знаков.
        """
        rows = [
            row
            for row in self.list_knowledge_versions(ko_id, user_id)
            if int(row.get("version") or 0) == int(version)
        ]
        if not rows:
            raise LookupError(f"Version {version} not found for {ko_id}")
        try:
            snapshot = json.loads(str(rows[0].get("snapshot_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("Version snapshot is not readable") from exc
        if not isinstance(snapshot, dict):
            raise ValueError("Version snapshot is not an object")
        fields = {name: snapshot[name] for name in self._RESTORABLE_FIELDS if name in snapshot}
        if not fields:
            raise ValueError("Version snapshot carries no restorable fields")
        if reviewed_by:
            # Кто откатил — в метаданные объекта, а не только в аудит: человек,
            # открывший запись через полгода, должен видеть это на ней самой.
            metadata = _json_dict_safe(fields.get("metadata_json"))
            metadata["restored_from_version"] = int(version)
            metadata["restored_by"] = str(reviewed_by)
            fields["metadata_json"] = metadata
        return self.update_knowledge_fields(ko_id, user_id, **fields)

    def diff_knowledge_versions(
        self,
        ko_id: str,
        user_id: str,
        *,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Structured diff between two versions (default: the two most recent)."""
        from friday.versions import diff_snapshots

        versions = self.list_knowledge_versions(ko_id, user_id)  # newest first
        if not versions:
            return None
        by_version = {int(row["version"]): _json_load(row.get("snapshot_json"), {}) for row in versions}
        available = sorted(by_version)
        newest = available[-1]
        target = to_version if to_version is not None else newest
        if target not in by_version:
            return None
        base = from_version
        if base is None:
            earlier = [v for v in available if v < target]
            base = earlier[-1] if earlier else target
        if base not in by_version:
            return None
        return {
            "knowledge_object_id": ko_id,
            "from_version": base,
            "to_version": target,
            "available_versions": available,
            "changes": diff_snapshots(by_version[base], by_version[target]),
        }

    def count_knowledge_objects(self, user_id: str, *, uploaded_by: str | None = None) -> int:
        if uploaded_by is not None and not str(uploaded_by).strip():
            return 0
        scope = ""
        params: list[Any] = [user_id]
        if uploaded_by is not None:
            scope = f" AND {_exact_uploader_knowledge_dependency('k')}"
            params.append(str(uploaded_by))
        row = self.execute(
            f"""SELECT COUNT(*) AS count FROM knowledge_objects k
                 WHERE k.user_id=? AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   {scope}""",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    # Потолок множества окна. Выше него окно перестаёт быть окном: если под него
    # подпадает двадцать тысяч объектов, это не «покажи за март», а весь архив, и
    # честнее не строить множество вовсе, чем строить его усечённым и молча
    # потерять часть (усечение уже ловили у `list_entities`).
    _WINDOW_IDS_MAX = 20_000

    def knowledge_ids_in_window(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        uploaded_by: str | None = None,
    ) -> set[str] | None:
        """Идентификаторы объектов, попадающих в диапазон дат. None — окна нет.

        Предикат берётся из `_knowledge_filter` — того же, что строит список и его
        счётчик. Иметь два определения «попадает в период» значило бы, что поиск и
        листинг однажды разойдутся, и разойдутся молча.

        Смысл диапазона тот же, что в листинге: собственная дата документа ЛИБО
        любая упомянутая в тексте. Возврат — множество, потому что фильтрация идёт
        по кандидатам всех каналов сразу; None означает «фильтровать не по чему»,
        а пустое множество — «в этот период нет ничего», и это разные ответы.
        """
        if not since and not until:
            return None
        where, params = self._knowledge_filter(
            user_id,
            lifecycle_stage=None,
            tag=None,
            entity_id=None,
            since=since,
            until=until,
            uploaded_by=uploaded_by,
        )
        rows = self.execute(
            f"SELECT id FROM knowledge_objects WHERE {where} LIMIT ?",  # nosec B608 - предикат из общего построителя
            (*params, self._WINDOW_IDS_MAX + 1),
        ).fetchall()
        if len(rows) > self._WINDOW_IDS_MAX:
            LOGGER.info(
                "Диапазон дат покрывает больше %d объектов — фильтр по нему не применяется",
                self._WINDOW_IDS_MAX,
            )
            return None
        return {str(row["id"]) for row in rows}

    def list_live_knowledge_ids(self, user_id: str) -> set[str]:
        """Every live object's id, in ONE snapshot.

        The vault prune needs a complete set, and assembling one by paging is not
        the same thing: `list_knowledge_objects` orders by `importance DESC,
        updated_at DESC`, both of which change under concurrent edits, so a row
        can move across a page boundary between two pages and never appear in
        either. The prune then treats that live object as an orphan and deletes
        its note. Ids only, no ordering, no pagination — cheap enough to take
        whole even on a large corpus.
        """
        rows = self.execute(
            f"""SELECT k.id FROM knowledge_objects k
                 WHERE k.user_id=? AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
            (user_id,),
        ).fetchall()
        return {str(row["id"]) for row in rows}

    def _knowledge_filter(
        self,
        user_id: str,
        *,
        lifecycle_stage: str | None,
        tag: str | None,
        entity_id: str | None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        uploaded_by: str | None = None,
    ) -> tuple[str, list[Any]]:
        """The WHERE clause and its parameters, built ONCE for the list and its count.

        Shared on purpose. `count_knowledge_objects` used to count every live object
        of the account while the listing next to it was filtered by tag, lifecycle or
        entity — so a pager built on that pair would have said «1-100 из 3000» over a
        filtered set of twelve. A total that does not answer the same question as the
        page is worse than no total: it makes «Вперёд» wrong in both directions.
        """
        params: list[Any] = [user_id]
        where = "user_id=? AND deleted_at IS NULL AND " + _not_private_knowledge_dependency(
            "knowledge_objects"
        )
        if uploaded_by is not None:
            author = str(uploaded_by)
            if not author.strip():
                where += " AND 0"
            else:
                where += " AND " + _exact_uploader_knowledge_dependency("knowledge_objects")
                params.append(author)
        if lifecycle_stage:
            where += " AND lifecycle_stage=?"
            params.append(lifecycle_stage)
        if tag:
            # ``tags_json`` is a canonical JSON array, so json_each enumerates
            # exact tags; jericho_casefold keeps the match case-insensitive for
            # Cyrillic as well (SQLite's lower() folds ASCII only).
            where += (
                " AND EXISTS (SELECT 1 FROM json_each(knowledge_objects.tags_json)"
                " WHERE jericho_casefold(json_each.value) = jericho_casefold(?))"
            )
            params.append(tag)
        if query and query.strip():
            # Подстрочный поиск по заголовку, сводке и имени файла — ровно то, чем
            # человек ищет глазами. Замерено на корпусе владельца: 1265 различных
            # заголовков на 1537 объектов, средняя длина 28.5 знака, то есть заголовки
            # содержательны. А сортировка по важности вырождена (0.66..0.72 на весь
            # архив, три различных дня в `updated_at`), поэтому листать бесполезно:
            # без строки поиска найти документ руками нельзя вовсе.
            #
            # Именно ПОДСТРОЧНЫЙ, а не FTS: человек помнит обрывок («поверка вес»), и
            # ему нужно совпадение по началу слова, а не по словоформе. Полнотекстовый
            # поиск по телу живёт отдельно и решает другую задачу.
            needle = f"%{query.strip()}%"
            where += (
                " AND (jericho_casefold(COALESCE(title,'')) LIKE jericho_casefold(?)"
                " OR jericho_casefold(COALESCE(summary,'')) LIKE jericho_casefold(?)"
                " OR jericho_casefold(COALESCE(json_extract(metadata_json,'$.filename'),''))"
                " LIKE jericho_casefold(?))"
            )
            params.extend([needle, needle, needle])
        if since or until:
            # «Покажи всё за март 2023» — первое, что спрашивают у архива за годы, и
            # ответить было нечем. Работа при этом уже была сделана и потеряна: даты
            # извлечены и лежат в метаданных у 630 объектов из 1537, в среднем по пять
            # на документ, — и не использовались нигде, ни колонкой, ни индексом, ни
            # параметром листинга.
            #
            # Условие «документ УПОМИНАЕТ дату в диапазоне», а не «дата документа
            # такая». Второго данные не дают: документ называет несколько дат, и какая
            # из них его собственная — неизвестно. Придумывать «главную» значило бы
            # угадывать за человека; упоминание проверяемо и честно.
            #
            # С 0.151.0 к упоминаниям добавлена СОБСТВЕННАЯ дата документа, если она
            # известна из провенанса файла (docProps/core.xml, /CreationDate). Это не
            # угадывание: дату записал редактор при сохранении, а не мы вывели из
            # текста. Условие — дизъюнкция: документ подходит, если в диапазон попала
            # либо его собственная дата, либо любая упомянутая. Сужать до собственной
            # нельзя — она есть далеко не у всех, и «покажи за март» молча потеряло бы
            # всё, что пришло текстом.
            document_date = (
                "jericho_iso_date(json_extract(knowledge_objects.metadata_json,'$.document_date'))"
            )
            own: list[str] = [f"{document_date} IS NOT NULL"]
            own_params: list[Any] = []
            mentioned = (
                " EXISTS (SELECT 1 FROM json_each(knowledge_objects.metadata_json, '$.dates')"
                " WHERE jericho_iso_date(json_each.value) IS NOT NULL"
            )
            mentioned_params: list[Any] = []
            if since:
                own.append(f"{document_date} >= ?")
                own_params.append(since)
                mentioned += " AND jericho_iso_date(json_each.value) >= ?"
                mentioned_params.append(since)
            if until:
                own.append(f"{document_date} <= ?")
                own_params.append(until)
                mentioned += " AND jericho_iso_date(json_each.value) <= ?"
                mentioned_params.append(until)
            mentioned += ")"
            where += f" AND (({' AND '.join(own)}) OR{mentioned})"
            params.extend(own_params)
            params.extend(mentioned_params)
        if entity_id:
            # Browse-by-entity/container: only reviewer-accepted links count.
            where += (
                " AND EXISTS (SELECT 1 FROM knowledge_entity_links l"
                " JOIN entities requested_entity"
                " ON requested_entity.id=l.entity_id AND requested_entity.user_id=l.user_id"
                f" AND {_not_private_entity_material_dependency('requested_entity')}"  # nosec B608
                " WHERE l.knowledge_object_id = knowledge_objects.id"
                " AND l.entity_id=? AND l.user_id=? AND l.status='accepted')"
            )
            params.extend([entity_id, user_id])
        return where, params

    def count_filtered_knowledge_objects(
        self,
        user_id: str,
        *,
        lifecycle_stage: str | None = None,
        tag: str | None = None,
        entity_id: str | None = None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        """How many objects the SAME filters select — the total a page is a page of."""
        where, params = self._knowledge_filter(
            user_id,
            lifecycle_stage=lifecycle_stage,
            tag=tag,
            entity_id=entity_id,
            query=query,
            since=since,
            until=until,
        )
        # ``where`` contains only fixed clauses; all values remain bound parameters.
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM knowledge_objects WHERE {where}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_knowledge_objects(
        self,
        user_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        lifecycle_stage: str | None = None,
        tag: str | None = None,
        entity_id: str | None = None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        uploaded_by: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._knowledge_filter(
            user_id,
            lifecycle_stage=lifecycle_stage,
            tag=tag,
            entity_id=entity_id,
            query=query,
            since=since,
            until=until,
            uploaded_by=uploaded_by,
        )
        params.extend([max(1, min(limit, 5000)), max(0, offset)])
        # ``where`` contains only fixed clauses; all values remain bound parameters.
        rows = self.execute(
            f"SELECT * FROM knowledge_objects WHERE {where} ORDER BY importance DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    # Доля корпуса, выше которой тег перестаёт быть осью навигации. Замерено на архиве
    # владельца: `document` и `application` стоят на 1524 объектах из 1537 — то есть
    # 99%, и выбор такого тега не сужает НИЧЕГО. А показ отсортирован по убыванию
    # частоты, значит на экран попадала строго худшая часть распределения: и чипы в
    # админке, и `/tags` в Telegram возглавляли два тега, приписанные каждому файлу
    # без анализа содержимого.
    #
    # Полезное при этом было и не показывалось: 903 тега из 1693 стоят на 2-77
    # объектах, то есть сужают до пяти процентов базы и меньше.
    #
    # Половина, а не пятая часть: тег на четверти архива всё ещё сужает вчетверо и
    # может быть осмысленным («рядовой» — 334 объекта из 1537). Отсекается только то,
    # что не сужает по существу.
    _TAG_NOISE_SHARE = 0.5
    # Ниже этого числа объектов правило не применяется: на архиве из десяти записей
    # любой тег покроет заметную долю, а листать десять можно и без осей.
    _TAG_NOISE_MIN_CORPUS = 20

    def list_documents_with_entity_suggestions(
        self, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Документы, у которых остались неразобранные предложения сущностей.

        Очереди кандидатов не существовало, и это была не мелочь: проверено по живой
        базе — НИ ОДНА из 109 сущностей и НИ ОДНА из 226 связей не пришла от человека.
        У всех сущностей в метаданных ключи автосоздания при импорте; ключа
        `origin: human_review`, который ставит обработчик подтверждения, нет ни у одной.
        В аудите нет ни одной записи `admin.entity_suggestion.accept`.

        Причина не в том, что человек отказался, а в том, что предложить было негде:
        кандидаты считаются ПО ЗАПРОСУ и нигде не хранятся, поэтому их нельзя было ни
        посчитать, ни показать. На обзоре шесть плиток — знания, сущности, Inbox,
        пользователи, диалоги, сообщения; числа кандидатов среди них нет. В разделе
        «Граф» четыре очереди на проверку, и этой среди них тоже нет. Единственный вход
        — открыть конкретный документ и нажать «Инспекция».

        Число берётся из `entity_suggestion_count`, записанного при приёме (есть у 1532
        объектов из 1537, всего 10 100 предложений, медиана 7 на документ), и
        уменьшается на число уже решённых связей этого документа. Это ОЦЕНКА сверху, а
        не точный остаток: предложение и связь — не одно и то же, и совпадение между
        ними неполное. Точный ответ требует пересчёта по тексту, а он дорог; оценка
        честно называется оценкой в интерфейсе.
        """
        rows = self.execute(
            f"""SELECT k.id AS id, k.title AS title, k.updated_at AS updated_at,
                      CAST(COALESCE(json_extract(k.metadata_json,'$.entity_suggestion_count'), 0) AS INTEGER)
                        AS suggested,
                      (SELECT COUNT(*) FROM knowledge_entity_links l
                        WHERE l.user_id=k.user_id AND l.knowledge_object_id=k.id) AS decided
               FROM knowledge_objects k
               WHERE k.user_id=? AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}
                 AND CAST(COALESCE(json_extract(k.metadata_json,'$.entity_suggestion_count'), 0) AS INTEGER) >
                     (SELECT COUNT(*) FROM knowledge_entity_links l
                       WHERE l.user_id=k.user_id AND l.knowledge_object_id=k.id)
               ORDER BY (CAST(COALESCE(json_extract(k.metadata_json,'$.entity_suggestion_count'), 0) AS INTEGER) -
                        (SELECT COUNT(*) FROM knowledge_entity_links l
                          WHERE l.user_id=k.user_id AND l.knowledge_object_id=k.id)) DESC,
                        k.rowid DESC
               LIMIT ? OFFSET ?""",  # nosec B608
            (user_id, max(1, min(int(limit), 500)), max(0, offset)),
        ).fetchall()
        total = self.execute(
            f"""SELECT COUNT(*) AS count FROM knowledge_objects k
               WHERE k.user_id=? AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}
                 AND CAST(COALESCE(json_extract(k.metadata_json,'$.entity_suggestion_count'), 0) AS INTEGER) >
                     (SELECT COUNT(*) FROM knowledge_entity_links l
                       WHERE l.user_id=k.user_id AND l.knowledge_object_id=k.id)""",  # nosec B608
            (user_id,),
        ).fetchone()
        items = [
            {
                "id": str(row["id"]),
                "title": str(row["title"] or "Без названия"),
                "updated_at": row["updated_at"],
                "pending": max(0, int(row["suggested"]) - int(row["decided"])),
            }
            for row in rows
        ]
        return items, int(total["count"] if total else 0)

    def count_knowledge_tags(self, user_id: str) -> int:
        """Сколько РАЗЛИЧНЫХ тегов в базе — отдельным счётом, без потолка.

        Длина показанной страницы фактом о корпусе не является: команда `/tags`
        просит 25 и печатала их под заголовком «Теги вашей базы знаний», а тегов
        двести. Человек читает список как полный.
        """
        safe_tags = _safe_tags_json_expression("k")
        row = self.execute(
            f"""SELECT COUNT(DISTINCT jericho_casefold(
                       substr(CAST(je.value AS TEXT),1,{_ENTITY_SUMMARY_TAG_MAX_CHARS}))) AS total
                 FROM knowledge_objects k JOIN json_each({safe_tags}) je
                 WHERE k.user_id=? AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   AND je.type='text' AND trim(CAST(je.value AS TEXT))<>''""",  # nosec B608
            (user_id,),
        ).fetchone()
        return int(row["total"]) if row else 0

    def list_knowledge_tags(self, user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Distinct tags with usage counts for browse-by-tag surfaces.

        Tags are stored canonically (deduped, casefold-sorted) per object, so
        one json_each pass yields exact values; grouping is case-insensitive
        with the first-seen spelling kept for display.

        Теги, стоящие больше чем на половине живого корпуса, не возвращаются: они не
        сужают выбор и вытесняют с экрана то, что сужает. Порог применяется только к
        заметному корпусу — см. константы выше.
        """
        safe_tags = _safe_tags_json_expression("k")
        rows = self.execute(
            f"""SELECT substr(CAST(je.value AS TEXT),1,{_ENTITY_SUMMARY_TAG_MAX_CHARS}) AS tag,
                       COUNT(*) AS count
                 FROM knowledge_objects k JOIN json_each({safe_tags}) je
                 WHERE k.user_id=? AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   AND je.type='text' AND trim(CAST(je.value AS TEXT))<>''
                 GROUP BY jericho_casefold(
                     substr(CAST(je.value AS TEXT),1,{_ENTITY_SUMMARY_TAG_MAX_CHARS}))
                 ORDER BY count DESC, jericho_casefold(tag) ASC LIMIT ?""",  # nosec B608
            # С запасом: часть строк отсеется как шум, и без запаса страница вышла бы
            # короче запрошенной ровно на число отсеянных.
            (user_id, max(1, min(int(limit), 1000)) * 4),
        ).fetchall()
        total = self.count_knowledge_objects(user_id)
        ceiling = total * self._TAG_NOISE_SHARE if total >= self._TAG_NOISE_MIN_CORPUS else None
        items: list[dict[str, Any]] = [{"tag": str(row["tag"]), "count": int(row["count"])} for row in rows]
        if ceiling is not None:
            items = [item for item in items if int(item["count"]) <= ceiling]
        return items[: max(1, min(int(limit), 1000))]

    def list_container_entities(self, user_id: str, types: tuple[str, ...]) -> list[dict[str, Any]]:
        """Canonical container entities (projects/collections) with member counts."""
        if not types:
            return []
        placeholders = ",".join("?" for _ in types)
        rows = self.execute(
            "SELECT e.id, substr(e.name, 1, 240) AS name, e.entity_type, ("
            " SELECT COUNT(*) FROM knowledge_entity_links l"
            " JOIN knowledge_objects k ON k.id = l.knowledge_object_id"
            " AND k.user_id = l.user_id"
            " WHERE l.entity_id = e.id AND l.user_id = e.user_id"
            " AND l.status='accepted' AND k.deleted_at IS NULL"
            f" AND {_not_private_knowledge_dependency('k')}"  # nosec B608
            ") AS knowledge_count"
            f" FROM entities e WHERE e.user_id=? AND e.entity_type IN ({placeholders})"  # nosec B608
            " AND e.deleted_at IS NULL AND e.canonical=1"
            f" AND {_not_private_entity_material_dependency('e')}"  # nosec B608
            " ORDER BY knowledge_count DESC, lower(e.name) ASC, e.id LIMIT 1000",
            (user_id, *types),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_entities_by_activity(
        self,
        user_id: str,
        *,
        types: tuple[str, ...] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Canonical entities ranked by how many live knowledge objects link them.

        The signal behind "recurring people/projects" in a user model: an entity
        the user keeps attaching material to. Only accepted links and non-deleted
        knowledge count.
        """
        clauses = [
            "e.user_id=?",
            "e.deleted_at IS NULL",
            "e.canonical=1",
            _not_private_entity_material_dependency("e"),
        ]
        params: list[Any] = [user_id]
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"e.entity_type IN ({placeholders})")  # nosec B608
            params.extend(types)
        params.append(max(1, min(int(limit), 100)))
        rows = self.execute(
            "SELECT substr(e.id,1,160) AS id, substr(e.name,1,240) AS name, "
            "substr(e.entity_type,1,80) AS entity_type,"
            " COUNT(l.id) AS knowledge_count"
            " FROM entities e"
            " JOIN knowledge_entity_links l"
            "   ON l.entity_id = e.id AND l.user_id = e.user_id AND l.status='accepted'"
            " JOIN knowledge_objects k"
            "   ON k.id = l.knowledge_object_id AND k.user_id = l.user_id"
            "  AND k.deleted_at IS NULL"
            f"  AND {_not_private_knowledge_dependency('k')}"  # nosec B608
            f" WHERE {' AND '.join(clauses)}"  # nosec B608
            " GROUP BY e.id ORDER BY knowledge_count DESC, lower(e.name) ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_knowledge(self, user_id: str, *, since_iso: str, limit: int = 10) -> list[dict[str, Any]]:
        """Knowledge created at or after ``since_iso`` — the "what happened lately" window."""
        rows = self.execute(
            f"""SELECT substr(k.id,1,160) AS id, substr(k.title,1,240) AS title,
                      substr(k.knowledge_kind,1,80) AS knowledge_kind,
                      substr(k.created_at,1,64) AS created_at FROM knowledge_objects k
                 WHERE k.user_id=? AND k.deleted_at IS NULL AND k.created_at >= ?
                   AND {_not_private_knowledge_dependency("k")}
                 ORDER BY k.created_at DESC LIMIT ?""",  # nosec B608
            (user_id, since_iso, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_documents_by_own_date(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Документы, упорядоченные по СОБСТВЕННОЙ дате, — материал для хроники.

        Отдельный метод, а не `list_knowledge_objects` с окном: тот сортирует по
        важности и свежести записи, а для ленты нужен порядок по дате документа.
        Берутся только объекты, у которых своя дата есть: упомянутые в тексте даты
        для хронологии не годятся — документ может называть десяток чужих дат, и
        поставить его в ленту по любой из них значит соврать о времени.
        """
        clauses, params = self._own_date_window(user_id, since=since, until=until)
        params.append(max(1, min(int(limit), 500)))
        rows = self.execute(
            "SELECT id, title, knowledge_kind, "
            "jericho_iso_date(json_extract(metadata_json,'$.document_date')) AS document_date "
            f"FROM knowledge_objects WHERE {' AND '.join(clauses)} "  # nosec B608 - фиксированные условия
            "ORDER BY document_date DESC, rowid DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def _own_date_window(
        self, user_id: str, *, since: str | None, until: str | None
    ) -> tuple[list[str], list[Any]]:
        """Условие «документ со своей датой попадает в окно» — одно на всех.

        Список и его счётчик обязаны считать одно и то же: два определения окна
        однажды разъедутся, и разъедутся молча — тот же принцип, что у
        `_knowledge_filter`.
        """
        expression = "jericho_iso_date(json_extract(metadata_json,'$.document_date'))"
        clauses = [
            "user_id=?",
            "deleted_at IS NULL",
            _not_private_knowledge_dependency("knowledge_objects"),
            f"{expression} IS NOT NULL",
        ]
        params: list[Any] = [user_id]
        if since:
            clauses.append(f"{expression} >= ?")
            params.append(since)
        if until:
            clauses.append(f"{expression} <= ?")
            params.append(until)
        return clauses, params

    def count_documents_by_own_date(
        self, user_id: str, *, since: str | None = None, until: str | None = None
    ) -> int:
        """Сколько документов в окне ВСЕГО — без потолка выборки.

        Чат печатал «Показаны первые 10 из M», где M — длина полученного списка, а
        список запрашивался с `limit=11`. То есть на марте с четырьмя сотнями
        документов человек читал «показаны первые 10 из 11»: число выглядит как
        факт о корпусе, а является размером собственной страницы. Ровно та же
        ошибка, что была в карточке объекта («связанных документов: 10» при 314).
        """
        clauses, params = self._own_date_window(user_id, since=since, until=until)
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM knowledge_objects WHERE {' AND '.join(clauses)}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"]) if row else 0

    def knowledge_date_histogram(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        granularity: str = "year",
    ) -> list[dict[str, Any]]:
        """Сколько документов приходится на каждый год, месяц или день окна.

        Считается в SQLite, а не перебором страниц: на корпусе владельца документы
        расходятся на 2000..2026 годы с пиком 521 в 2024-м, и вытащить их все ради
        подсчёта столбиков значило бы гонять полторы тысячи записей за одну картинку.

        Крупность — только из этого списка; она подставляется в SQL как срез строки,
        и брать её из запроса напрямую было бы дырой.
        """
        width = {"year": 4, "month": 7, "day": 10}.get(str(granularity), 4)
        expression = "jericho_iso_date(json_extract(metadata_json,'$.document_date'))"
        clauses = [
            "user_id=?",
            "deleted_at IS NULL",
            _not_private_knowledge_dependency("knowledge_objects"),
            f"{expression} IS NOT NULL",
        ]
        params: list[Any] = [user_id]
        if since:
            clauses.append(f"{expression} >= ?")
            params.append(since)
        if until:
            clauses.append(f"{expression} <= ?")
            params.append(until)
        rows = self.execute(
            f"SELECT substr({expression},1,{width}) AS bucket, COUNT(*) AS count "  # nosec B608
            f"FROM knowledge_objects WHERE {' AND '.join(clauses)} "
            "GROUP BY bucket ORDER BY bucket",
            tuple(params),
        ).fetchall()
        return [{"bucket": str(row["bucket"]), "count": int(row["count"])} for row in rows]

    def count_knowledge_without_own_date(self, user_id: str) -> int:
        """Сколько живых объектов в ленту не попадёт вовсе.

        Хроника строится по собственной дате, а она известна у 88% корпуса. Остальные
        не «нулевые» и не «старые» — они просто невидимы для этого экрана, и экран
        обязан назвать их число сам, иначе читается как полный охват.
        """
        row = self.execute(
            f"""SELECT COUNT(*) AS count FROM knowledge_objects k
                 WHERE k.user_id=? AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   AND jericho_iso_date(
                       json_extract(k.metadata_json,'$.document_date')) IS NULL""",  # nosec B608
            (user_id,),
        ).fetchone()
        return int(row["count"]) if row else 0

    def count_recent_knowledge(self, user_id: str, *, since_iso: str) -> int:
        """Сколько создано с этого момента — счётом, а не длиной страницы.

        `list_recent_knowledge` зажат потолком 200, и профиль человека показывал
        ровно 200 «за 30 дней» всякому, кто перешагнул этот рубеж.
        """
        row = self.execute(
            f"""SELECT COUNT(*) AS count FROM knowledge_objects k
                 WHERE k.user_id=? AND k.deleted_at IS NULL AND k.created_at >= ?
                   AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
            (user_id, since_iso),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_files_received_on(
        self,
        user_id: str,
        *,
        days: Sequence[str],
        utc_offset_minutes: int = 0,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """Файлы, ПРИШЕДШИЕ в названные дни, — материал для архива.

        Владелец 2026-08-03: «Пятница же не умеет архивы собирать? Надо, чтобы
        умела: собрать документы, пришедшие за 10, 13 и 25 число». Дни идут
        списком, а не диапазоном: между 10-м и 25-м лежит две недели чужих
        файлов, и «с 10 по 25» — не то, о чём просили.

        Берутся ИСХОДНЫЕ файлы (`raw_objects.metadata_json.stored_path`), а не
        извлечённый из них текст: человек просил документы, а не пересказ.

        `utc_offset_minutes` переводит метку в сутки ЧЕЛОВЕКА. Без этого «за 25
        число» отдало бы файлы, пришедшие 25-го по Гринвичу, — а вечер 25-го в
        Москве это уже 25-е и там, и там, зато вечер 24-го по МСК попал бы в
        выборку 24-го, но час с 21:00 до полуночи уехал бы в 25-е. Тот же класс,
        что чинили в хронике, напоминаниях и тихих часах.
        """
        wanted = [str(day).strip() for day in days if str(day).strip()]
        if not wanted:
            return []
        shift = f"{int(utc_offset_minutes):+d} minutes"
        placeholders = ",".join("?" for _ in wanted)
        rows = self.execute(
            "SELECT r.id AS raw_id, r.metadata_json AS metadata_json, r.received_at AS received_at,"
            " k.id AS ko_id, k.title AS title, k.knowledge_kind AS knowledge_kind"
            " FROM raw_objects AS r"
            " LEFT JOIN knowledge_objects AS k"
            "   ON k.raw_object_id = r.id AND k.user_id=r.user_id AND k.deleted_at IS NULL"
            f"  AND {_not_private_knowledge_dependency('k')}"  # nosec B608
            " WHERE r.user_id=? AND r.deleted_at IS NULL AND r.content_type='file'"
            f"   AND {_not_audio_document('r')}"  # nosec B608
            f"   AND {_not_private_raw_dependency('r')}"  # nosec B608
            f"   AND date(datetime(r.received_at, ?)) IN ({placeholders})"  # nosec B608 - только плейсхолдеры
            " ORDER BY r.received_at ASC LIMIT ?",
            (user_id, shift, *wanted, max(1, min(int(limit), 2000))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            meta = _json_dict_safe(row["metadata_json"])
            stored = str(meta.get("stored_path") or "")
            if not stored:
                # Запись есть, файла за ней нет. Молча пропустить нельзя — иначе
                # архив тихо недосчитается документов, а человек будет думать,
                # что получил всё.
                continue
            out.append(
                {
                    "raw_id": str(row["raw_id"]),
                    "ko_id": str(row["ko_id"] or ""),
                    "title": str(row["title"] or meta.get("filename") or ""),
                    "filename": str(meta.get("filename") or ""),
                    "stored_path": stored,
                    "mime_type": str(meta.get("mime_type") or ""),
                    "size_bytes": int(meta.get("size_bytes") or 0),
                    "received_at": str(row["received_at"] or ""),
                    "knowledge_kind": str(row["knowledge_kind"] or ""),
                }
            )
        return out

    def count_files_received_on(
        self, user_id: str, *, days: Sequence[str], utc_offset_minutes: int = 0
    ) -> int:
        """Сколько файлов в этих днях ВСЕГО — отдельным счётом, не длиной страницы.

        «Длина страницы — не факт о корпусе»: за ночь 2026-08-01 эта ошибка
        нашлась трижды в разных подсистемах. Архиву она обошлась бы дороже
        обычного — человек унесёт файл с собой, считая его полным.
        """
        wanted = [str(day).strip() for day in days if str(day).strip()]
        if not wanted:
            return 0
        shift = f"{int(utc_offset_minutes):+d} minutes"
        placeholders = ",".join("?" for _ in wanted)
        row = self.execute(
            "SELECT COUNT(*) AS count FROM raw_objects r"
            " WHERE r.user_id=? AND r.deleted_at IS NULL AND r.content_type='file'"
            f"   AND {_not_audio_document('r')}"  # nosec B608
            f"   AND {_not_private_raw_dependency('r')}"  # nosec B608
            f"   AND date(datetime(r.received_at, ?)) IN ({placeholders})",  # nosec B608 - placeholders
            (user_id, shift, *wanted),
        ).fetchone()
        return int(row["count"]) if row else 0

    def list_knowledge_on_this_day(
        self,
        user_id: str,
        *,
        month_day: str,
        before_iso: str,
        limit: int = 10,
        utc_offset_minutes: int = 0,
    ) -> list[dict[str, Any]]:
        """Knowledge captured on the same calendar day (MM-DD) in an earlier year.

        ``created_at`` is an ISO string, so ``strftime('%m-%d', …)`` selects the
        anniversary and the ``created_at < before_iso`` bound keeps only the past.

        `utc_offset_minutes` переводит метку в ВРЕМЯ ЧЕЛОВЕКА перед сравнением.
        Без этого сравнивались две разные шкалы: день приходит из `local_now`
        (у человека уже 3 августа), а `created_at` лежит в UTC (там ещё 2-е).
        Замерено на живом архиве: в это окно (21:00–24:00 UTC при МСК) попадают
        2 записи из 1533 — редко, но годовщина такой записи показалась бы не в
        свой день, а найти причину по одному сообщению в чате невозможно.

        Тот же класс, что уже чинили в тихих часах и напоминаниях: «время —
        время ЧЕЛОВЕКА, а не UTC».
        """
        shift = f"{int(utc_offset_minutes):+d} minutes"
        # Обе половины условия считаются в ОДНИХ сутках — местных. Сдвинуть
        # только выбор месяца-дня было мало: сегодняшняя вечерняя запись
        # проходила границу «строго прошлое» (её UTC-метка меньше местной даты) и
        # показывалась как собственная годовщина в день создания.
        rows = self.execute(
            f"""SELECT substr(k.id,1,160) AS id, substr(k.title,1,240) AS title,
                      substr(k.knowledge_kind,1,80) AS knowledge_kind,
                      substr(k.created_at,1,64) AS created_at FROM knowledge_objects k
                 WHERE k.user_id=? AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   AND strftime('%m-%d', datetime(k.created_at, ?)) = ?
                   AND date(datetime(k.created_at, ?)) < ?
                 ORDER BY k.created_at DESC LIMIT ?""",  # nosec B608
            (user_id, shift, month_day, shift, before_iso, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def soft_delete_knowledge_object(self, ko_id: str, user_id: str | None = None) -> bool:
        """Soft-delete an object while retaining a complete version snapshot."""
        current = self.get_knowledge_object(ko_id, user_id)
        if not current or current.get("deleted_at"):
            return False
        owner = str(current["user_id"])
        updated = self.update_knowledge_fields(
            ko_id,
            owner,
            lifecycle_stage=LifecycleStage.DELETED.value,
            deleted_at=utc_now(),
        )
        return updated is not None

    def vocabulary_terms(self, prefixes: Sequence[str], *, limit: int = 400) -> list[str]:
        """Indexed terms starting with any of ``prefixes`` — the corpus's own words.

        Reads `knowledge_vocab`, a view over the FTS index (no second copy of the
        text). Spelling repair needs to know what words this archive actually
        uses before it dares replace one the user typed, and a range scan on a
        two-letter prefix is the cheap half of that question.

        Corpus-wide rather than per-tenant: `knowledge_vocab` shadows the index,
        which has no user column. That is why a repaired query is only ACCEPTED
        when it finds results for the asking user — a word borrowed from another
        tenant's document simply returns nothing and the original query stands.
        """
        if not self._fts_available or not prefixes:
            return []
        terms: list[str] = []
        remaining = max(1, int(limit))
        for prefix in list(dict.fromkeys(prefixes))[:8]:
            if not prefix:
                continue
            # `prefix + last-code-point` bounds the range without LIKE, so the
            # scan uses the term index rather than reading every row.
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            try:
                rows = self.execute(
                    "SELECT term FROM knowledge_vocab WHERE term >= ? AND term < ? LIMIT ?",
                    (prefix, upper, remaining),
                ).fetchall()
            except sqlite3.OperationalError:
                return []  # older database without the vocab view
            terms.extend(str(row["term"]) for row in rows)
            remaining = max(1, int(limit) - len(terms))
            if len(terms) >= int(limit):
                break
        return terms

    def term_document_frequency(self, terms: Sequence[str]) -> dict[str, int]:
        """Сколько документов корпуса содержат каждое из этих слов.

        Читает `knowledge_vocab` — представление над индексом FTS, а не вторую
        копию текста, поэтому число всегда свежее и стоит одного индексного
        запроса. Нужно там, где решается, СУЖАЕТ ли слово выбор: тег,
        встречающийся почти везде, не отбирает ничего.

        Корпусная, а не арендаторская, по той же причине, что и
        `vocabulary_terms`: у индекса нет столбца пользователя. Для вопроса «это
        слово вообще различает документы» разницы нет, а для отбора по тегу
        считает уже сам отбор.
        """

        if not self._fts_available or not terms:
            return {}
        unique = [term for term in dict.fromkeys(str(item).strip().casefold() for item in terms) if term][
            :200
        ]
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        try:
            # Единственная подставляемая часть — ограниченная строка из «?».
            rows = self.execute(
                f"SELECT term, doc FROM knowledge_vocab WHERE term IN ({placeholders})",  # nosec B608
                tuple(unique),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {str(row["term"]): int(row["doc"] or 0) for row in rows}

    def known_vocabulary(self, terms: Sequence[str]) -> set[str]:
        """Which of ``terms`` are words this corpus actually contains, verbatim.

        The question `search_knowledge` cannot answer: it searches by PREFIX, so
        a two-character fragment of noise matches any document containing a word
        that starts with it. Measured — «хжщзхжщз ккккк» read on the other layout
        becomes «[;op[;op rrrrr», whose token `op` prefix-matched a log file, and
        that was enough to make a repair look justified. Exact membership is the
        test that separates "this reading is words" from "this reading collides".
        """
        if not self._fts_available or not terms:
            return set()
        unique = [term for term in dict.fromkeys(terms) if term][:24]
        if not unique:
            return set()
        placeholders = ",".join("?" for _ in unique)
        try:
            # The only interpolated fragment is a bounded sequence of ``?``.
            rows = self.execute(
                f"SELECT term FROM knowledge_vocab WHERE term IN ({placeholders})",  # nosec B608
                tuple(unique),
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {str(row["term"]) for row in rows}

    def search_knowledge(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        uploaded_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search one tenant, optionally restricted to one exact source author.

        ``uploaded_by`` is applied by joining the source Raw provenance before the
        FTS/LIKE ``LIMIT``.  Filtering a tenant-wide page afterwards is both unsafe
        and incorrect: other contributors can fill the cap, producing a false empty
        answer for the requested person.  Missing, malformed and non-text author
        metadata fails closed and is never attributed by inference.
        """
        text = " ".join((query or "").split()).strip()
        if not text:
            return []
        scope_where = ""
        scope_params: tuple[str, ...] = ()
        if uploaded_by is not None:
            author = str(uploaded_by)
            if not author.strip():
                return []
            scope_where = f" AND {_exact_uploader_knowledge_dependency('k')}"
            scope_params = (author,)
        rows: list[sqlite3.Row] = []
        terms = _fts_terms(text)
        if self._fts_available and terms:
            match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
            try:
                rows = self.execute(
                    f"""SELECT k.*, bm25(knowledge_fts, 1.0, 2.0, 1.5, 0.5) AS _rank
                       FROM knowledge_fts
                       JOIN knowledge_objects k ON k.rowid=knowledge_fts.rowid
                       WHERE k.user_id=? AND k.deleted_at IS NULL
                         AND {_not_private_knowledge_dependency("k")}
                         {scope_where}
                         AND knowledge_fts MATCH ?
                       ORDER BY _rank ASC, k.importance DESC LIMIT ?""",  # nosec B608
                    (user_id, *scope_params, match_query, max(1, min(limit, 200))),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            escaped = text.replace("%", r"\%").replace("_", r"\_")
            like = f"%{escaped}%"
            rows = self.execute(
                f"""SELECT k.* FROM knowledge_objects k
                   WHERE k.user_id=? AND k.deleted_at IS NULL
                     AND {_not_private_knowledge_dependency("k")}
                     {scope_where}
                     AND (k.title LIKE ? ESCAPE '\\' OR k.summary LIKE ? ESCAPE '\\'
                          OR k.content LIKE ? ESCAPE '\\' OR k.tags_json LIKE ? ESCAPE '\\')
                   ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
                (user_id, *scope_params, like, like, like, like, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def eval_case_health(self, user_id: str, *, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """How much of the gold set can still be satisfied at all.

        A case whose expected objects were all deleted depresses recall forever and is
        indistinguishable, in the number alone, from search having got worse — so the
        report says which it is.
        """
        rows = self.list_eval_cases(user_id) if cases is None else cases
        wanted: set[str] = set()
        for case in rows:
            wanted.update(str(item) for item in case.get("expected_ids", []))
        live: set[str] = set()
        ordered = sorted(wanted)
        for start in range(0, len(ordered), 400):
            batch = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            found = self.execute(
                "SELECT id FROM knowledge_objects "  # nosec B608
                f"WHERE user_id=? AND deleted_at IS NULL AND id IN ({placeholders})",
                (user_id, *batch),
            ).fetchall()
            live.update(str(row["id"]) for row in found)
        dead_ids: list[str] = []
        stale_manual = 0
        for case in rows:
            expected = {str(item) for item in case.get("expected_ids", [])}
            if expected and not (expected & live):
                dead_ids.append(str(case["id"]))
                if str(case.get("source") or "") == "manual":
                    stale_manual += 1
        return {
            "cases": len(rows),
            "stale": len(dead_ids),
            "stale_manual": stale_manual,
            "stale_mined": len(dead_ids) - stale_manual,
            "dead_case_ids": dead_ids,
        }

    def link_knowledge_entity(
        self,
        user_id: str,
        knowledge_object_id: str,
        entity_id: str,
        *,
        status: str = "accepted",
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
        reviewed_by: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status must be suggested, accepted, or rejected")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        ko = self.get_knowledge_object(knowledge_object_id, user_id)
        entity = self.get_entity(entity_id, user_id)
        if not ko or not entity or entity.get("deleted_at"):
            raise ValueError("Knowledge object and entity must belong to the same user")
        now = utc_now()
        link_id = new_id("kel")
        with self.transaction() as conn:
            visible_ko = conn.execute(
                f"""SELECT k.entity_id FROM knowledge_objects k
                     WHERE k.id=? AND k.user_id=? AND k.deleted_at IS NULL
                       AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (knowledge_object_id, user_id),
            ).fetchone()
            visible_entity = conn.execute(
                f"""SELECT 1 FROM entities e
                     WHERE e.id=? AND e.user_id=? AND e.deleted_at IS NULL
                       AND e.canonical=1 AND e.merged_into_id IS NULL
                       AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
                (entity_id, user_id),
            ).fetchone()
            if visible_ko is None or visible_entity is None:
                raise ValueError("Knowledge object and entity must belong to the same user")
            conn.execute(
                """INSERT INTO knowledge_entity_links(id, user_id, knowledge_object_id, entity_id,
                   status, confidence, evidence_json, created_at, reviewed_at, reviewed_by)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, knowledge_object_id, entity_id) DO UPDATE SET
                     status=excluded.status, confidence=excluded.confidence,
                     evidence_json=excluded.evidence_json, reviewed_at=excluded.reviewed_at,
                     reviewed_by=excluded.reviewed_by""",
                (
                    link_id,
                    user_id,
                    knowledge_object_id,
                    entity_id,
                    status,
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now if reviewed_by else None,
                    reviewed_by,
                ),
            )
            # Keep legacy primary link synchronized for older clients.
            if status == "accepted" and not visible_ko["entity_id"]:
                conn.execute(
                    "UPDATE knowledge_objects SET entity_id=?, updated_at=? WHERE id=? AND user_id=?",
                    (entity_id, now, knowledge_object_id, user_id),
                )
        row = self.execute(
            """SELECT id FROM knowledge_entity_links
               WHERE user_id=? AND knowledge_object_id=? AND entity_id=?""",
            (user_id, knowledge_object_id, entity_id),
        ).fetchone()
        return _bounded_public_knowledge_entity_link_by_id(self, user_id, str(row["id"])) or {} if row else {}

    def list_knowledge_entity_links_for(self, knowledge_ids: Sequence[str]) -> dict[str, list[str]]:
        """Accepted entity NAMES per Knowledge Object, in one query for the batch.

        The vault renders `[[wikilinks]]` from these. Fetched per page rather than
        per object on purpose — a per-object lookup here would rebuild the N+1 that
        the graph traversal was just cured of.
        """
        ids = [str(item) for item in knowledge_ids if item]
        if not ids:
            return {}
        result: dict[str, list[str]] = {}
        for start in range(0, len(ids), 400):
            batch = ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self.execute(
                "SELECT l.knowledge_object_id AS ko, e.name AS name "  # nosec B608
                "FROM knowledge_entity_links l JOIN entities e "
                "ON e.id=l.entity_id AND e.user_id=l.user_id "
                "JOIN knowledge_objects k ON k.id=l.knowledge_object_id "
                "AND k.user_id=l.user_id "
                f"WHERE l.status='accepted' AND e.deleted_at IS NULL "  # nosec B608
                f"AND {_not_private_entity_material_dependency('e')} "  # nosec B608
                f"AND {_not_private_knowledge_dependency('k')} "  # nosec B608
                f"AND l.knowledge_object_id IN ({placeholders}) "  # nosec B608
                "ORDER BY e.name COLLATE NOCASE",
                tuple(batch),
            ).fetchall()
            for row in rows:
                result.setdefault(str(row["ko"]), []).append(str(row["name"]))
        return result

    def list_knowledge_entity_links(
        self,
        user_id: str,
        *,
        entity_id: str | None = None,
        knowledge_object_id: str | None = None,
        status: str | None = "accepted",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["l.user_id=?"]
        params: list[Any] = [user_id]
        if entity_id:
            clauses.append("l.entity_id=?")
            params.append(entity_id)
        if knowledge_object_id:
            clauses.append("l.knowledge_object_id=?")
            params.append(knowledge_object_id)
        if status:
            clauses.append("l.status=?")
            params.append(status)
        params.append(max(1, min(limit, 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT l.*, e.name AS entity_name, e.entity_type,
                       k.title AS knowledge_title, k.lifecycle_stage AS knowledge_lifecycle
                FROM knowledge_entity_links l
                JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                JOIN knowledge_objects k ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                WHERE {_not_private_entity_material_dependency("e")}
                  AND {_not_private_knowledge_dependency("k")}
                  AND {" AND ".join(clauses)}
                ORDER BY CASE l.status WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                         l.confidence DESC, l.created_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def knowledge_impact(self, user_id: str, knowledge_object_id: str) -> dict[str, int]:
        """Что зависит от этого документа — вторая половина lineage (спека v3 §6).

        Первая половина, «откуда взялось», уже есть: `raw_source`, `versions`,
        `usage`. Здесь — «что затронет изменение»: сколько сущностей документ
        подтверждает и для скольких он ЕДИНСТВЕННЫЙ источник, то есть что именно
        исчезнет из графа вместе с ним.

        Замерено на копии боевой базы, критерий объявлен до запуска (доля ≥10% и
        запрос <100 мс): на одном документе держатся 1168 из 4448 сущностей
        (26.3%), а на самом густом документе корпуса (941 сущность) запрос
        занимает 2.9 мс. То есть ответ нетривиален для каждой четвёртой сущности
        и стоит дёшево.

        Считается ОДНИМ запросом через NOT EXISTS, а не обходом сущностей по
        одной: на 941 сущности обход был бы тысячей запросов ради одной строки.
        """
        row = self.execute(
            f"""SELECT
                 COUNT(*) AS entities,
                 SUM(CASE WHEN NOT EXISTS (
                       SELECT 1 FROM knowledge_entity_links o
                       JOIN knowledge_objects k2
                         ON k2.id=o.knowledge_object_id AND k2.user_id=o.user_id
                       WHERE o.user_id=l.user_id AND o.entity_id=l.entity_id
                         AND o.status='accepted' AND k2.deleted_at IS NULL
                         AND {_not_private_knowledge_dependency("k2")}
                         AND o.knowledge_object_id<>l.knowledge_object_id
                     ) THEN 1 ELSE 0 END) AS only_source
               FROM knowledge_entity_links l
               JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
               JOIN knowledge_objects k
                 ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                AND {_not_private_knowledge_dependency("k")}
               WHERE l.user_id=? AND l.knowledge_object_id=? AND l.status='accepted'
                 AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
            (user_id, knowledge_object_id),
        ).fetchone()
        return {
            "entities_confirmed": int((row["entities"] if row else 0) or 0),
            "entities_without_another_source": int((row["only_source"] if row else 0) or 0),
        }

    def count_knowledge_entity_links(self, user_id: str, knowledge_object_id: str) -> dict[str, int]:
        """Сколько сущностей связано с документом — по статусам и без потолка.

        Список выше ограничен сотней и смешивает статусы, поэтому считать его
        длину значит выдавать «связано сущностей: 100» на штатном расписании и
        засчитывать в это число связи, которые владелец ОТКЛОНИЛ. Статус — это
        решение человека; отклонённая связь не связь.
        """
        rows = self.execute(
            "SELECT l.status AS status, COUNT(*) AS count FROM knowledge_entity_links l"
            " JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id"
            " JOIN knowledge_objects k ON k.id=l.knowledge_object_id AND k.user_id=l.user_id"
            " WHERE l.user_id=? AND l.knowledge_object_id=? AND e.deleted_at IS NULL"
            f" AND {_not_private_entity_material_dependency('e')}"  # nosec B608
            f" AND {_not_private_knowledge_dependency('k')}"  # nosec B608
            " GROUP BY l.status",
            (user_id, knowledge_object_id),
        ).fetchall()
        counts = {"accepted": 0, "suggested": 0, "rejected": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["count"] or 0)
        return counts

    def set_knowledge_entity_link_status(
        self,
        link_id: str,
        user_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        """Review a proposed link without losing its evidence or history."""

        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status must be suggested, accepted, or rejected")
        now = utc_now()
        with self.transaction() as conn:
            link = conn.execute(
                f"""SELECT l.id, l.entity_id, l.knowledge_object_id
                       FROM knowledge_entity_links l
                       JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                            AND {_not_private_entity_material_dependency("e")}
                       JOIN knowledge_objects k
                         ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                        AND k.deleted_at IS NULL
                        AND {_not_private_knowledge_dependency("k")}
                      WHERE l.id=? AND l.user_id=?""",  # nosec B608
                (link_id, user_id),
            ).fetchone()
            if not link:
                return None
            changed = conn.execute(
                f"""UPDATE knowledge_entity_links
                       SET status=?, reviewed_at=?, reviewed_by=?
                     WHERE id=? AND user_id=?
                       AND EXISTS (
                           SELECT 1 FROM entities e
                            WHERE e.id=knowledge_entity_links.entity_id
                              AND e.user_id=knowledge_entity_links.user_id
                              AND {_not_private_entity_material_dependency("e")}
                       )
                       AND EXISTS (
                           SELECT 1 FROM knowledge_objects k
                            WHERE k.id=knowledge_entity_links.knowledge_object_id
                              AND k.user_id=knowledge_entity_links.user_id
                              AND k.deleted_at IS NULL
                              AND {_not_private_knowledge_dependency("k")}
                       )""",  # nosec B608
                (status, now, reviewed_by, link_id, user_id),
            )
            if changed.rowcount != 1:
                return None
            if status == "accepted":
                conn.execute(
                    """UPDATE knowledge_objects SET entity_id=COALESCE(entity_id, ?), updated_at=?
                       WHERE id=? AND user_id=?""",
                    (link["entity_id"], now, link["knowledge_object_id"], user_id),
                )
            else:
                current = conn.execute(
                    """SELECT entity_id FROM knowledge_objects
                       WHERE id=? AND user_id=?""",
                    (link["knowledge_object_id"], user_id),
                ).fetchone()
                if current and current["entity_id"] == link["entity_id"]:
                    fallback = conn.execute(
                        f"""SELECT l.entity_id FROM knowledge_entity_links l
                           JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                           WHERE l.user_id=? AND l.knowledge_object_id=? AND l.status='accepted'
                             AND l.id<>? AND {_not_private_entity_material_dependency("e")}
                           ORDER BY l.confidence DESC, l.created_at ASC LIMIT 1""",  # nosec B608
                        (user_id, link["knowledge_object_id"], link_id),
                    ).fetchone()
                    conn.execute(
                        """UPDATE knowledge_objects SET entity_id=?, updated_at=?
                           WHERE id=? AND user_id=?""",
                        (
                            fallback["entity_id"] if fallback else None,
                            now,
                            link["knowledge_object_id"],
                            user_id,
                        ),
                    )
        return _bounded_public_knowledge_entity_link_by_id(self, user_id, link_id)

    def count_entity_knowledge(self, user_id: str, entity_id: str) -> int:
        """How many accepted, live Knowledge Objects an entity carries.

        Exists because the graph layer answered this by loading up to a thousand
        full rows — bodies, summaries, tags — and calling ``len()`` on the list.
        """
        row = self.execute(
            f"""SELECT COUNT(*) AS count FROM knowledge_entity_links l
               JOIN knowledge_objects k
                 ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
               JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                    AND {_not_private_entity_material_dependency("e")}
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted'
                 AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
            (user_id, entity_id),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_entity_knowledge_refs(
        self, user_id: str, entity_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Ranking-relevant columns only, for callers that never read the body.

        ``context_for_query`` traverses the graph loading every linked object at up
        to 1000 rows per entity, for every entity the BFS dequeues — and the only
        fields it ever touches are the id, the link confidence and the two scores it
        sorts by. Everything else was megabytes of document text read from disk and
        thrown away.
        """
        rows = self.execute(
            f"""SELECT k.id, k.importance, k.quality_score, l.confidence AS _link_confidence
               FROM knowledge_entity_links l
               JOIN knowledge_objects k
                 ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
               JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                    AND {_not_private_entity_material_dependency("e")}
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted'
                 AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                f"""SELECT k.id, k.importance, k.quality_score, 1.0 AS _link_confidence
                   FROM knowledge_objects k WHERE k.user_id=? AND k.entity_id=?
                   AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_entities_knowledge_refs(
        self, user_id: str, entity_ids: Sequence[str], *, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]:
        """Return the per-entity ranking projection without an N+1 query loop.

        This is intentionally equivalent to calling ``list_entity_knowledge_refs``
        for each id: accepted links take precedence, the legacy direct entity link
        is used only when none exist, and the limit applies independently to every
        entity. Batches stay below SQLite's conservative parameter ceiling.
        """
        ordered_ids = list(dict.fromkeys(str(item) for item in entity_ids if item))
        if not ordered_ids:
            return {}
        per_entity_limit = max(1, min(limit, 1000))
        result: dict[str, list[dict[str, Any]]] = {entity_id: [] for entity_id in ordered_ids}

        for start in range(0, len(ordered_ids), 400):
            batch = ordered_ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            linked_rows = self.execute(
                "WITH ranked AS ("  # nosec B608
                " SELECT l.entity_id AS _entity_id, k.id, k.importance, k.quality_score,"
                " l.confidence AS _link_confidence,"
                " ROW_NUMBER() OVER (PARTITION BY l.entity_id"
                " ORDER BY k.importance DESC, k.updated_at DESC) AS _rank"
                " FROM knowledge_entity_links l"
                " JOIN knowledge_objects k ON k.id=l.knowledge_object_id"
                " AND k.user_id=l.user_id"
                " WHERE l.user_id=? AND l.status='accepted' AND k.deleted_at IS NULL"
                f" AND {_not_private_knowledge_dependency('k')}"
                f" AND l.entity_id IN ({placeholders})"
                ") SELECT _entity_id, id, importance, quality_score, _link_confidence"
                " FROM ranked WHERE _rank<=? ORDER BY _entity_id, _rank",
                (user_id, *batch, per_entity_limit),
            ).fetchall()
            for row in linked_rows:
                item = dict(row)
                entity_id = str(item.pop("_entity_id"))
                result[entity_id].append(item)

            fallback_ids = [entity_id for entity_id in batch if not result[entity_id]]
            if not fallback_ids:
                continue
            fallback_placeholders = ",".join("?" for _ in fallback_ids)
            fallback_rows = self.execute(
                "WITH ranked AS ("  # nosec B608
                " SELECT entity_id AS _entity_id, id, importance, quality_score,"
                " 1.0 AS _link_confidence,"
                " ROW_NUMBER() OVER (PARTITION BY entity_id"
                " ORDER BY importance DESC, updated_at DESC) AS _rank"
                " FROM knowledge_objects k WHERE k.user_id=? AND k.deleted_at IS NULL"
                f" AND {_not_private_knowledge_dependency('k')}"
                f" AND k.entity_id IN ({fallback_placeholders})"
                ") SELECT _entity_id, id, importance, quality_score, _link_confidence"
                " FROM ranked WHERE _rank<=? ORDER BY _entity_id, _rank",
                (user_id, *fallback_ids, per_entity_limit),
            ).fetchall()
            for row in fallback_rows:
                item = dict(row)
                entity_id = str(item.pop("_entity_id"))
                result[entity_id].append(item)
        return result

    def get_entity_knowledge(self, user_id: str, entity_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.execute(
            f"""SELECT k.*, l.confidence AS _link_confidence, l.evidence_json AS _link_evidence_json
               FROM knowledge_entity_links l
               JOIN knowledge_objects k
                 ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
               JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                    AND {_not_private_entity_material_dependency("e")}
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted'
                 AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                f"""SELECT k.* FROM knowledge_objects k WHERE k.user_id=? AND k.entity_id=?
                   AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    # Сырого `metadata_json` здесь НЕТ, и это замер, а не вкус: на живом корпусе
    # его медиана — 13 253 знака на десять карточек, то есть один этот столбец
    # перекрывает весь лимит инструмента (12 000), и до модели переставали
    # доходить поля, стоящие в ответе ПОСЛЕ списка: сводка, число документов,
    # пометка о производности. Замерено: так было у 34% сущностей корпуса.
    # Из метаданных карточке нужна ровно одна вещь — собственная дата документа.
    _ENTITY_CARD_COLUMNS = (
        "substr(k.id, 1, 160) AS id, "
        f"substr(k.title, 1, {_ENTITY_CARD_TITLE_MAX_CHARS}) AS title, "
        f"substr(k.summary, 1, {_ENTITY_CARD_SUMMARY_MAX_CHARS}) AS summary, "
        f"CASE WHEN length(CAST(COALESCE(k.tags_json, '') AS BLOB))<={_ENTITY_CARD_TAGS_MAX_BYTES} "
        "THEN CASE WHEN json_valid(k.tags_json) "
        "THEN CASE WHEN json_type(k.tags_json)='array' THEN k.tags_json ELSE '[]' END "
        "ELSE '[]' END ELSE '[]' END AS tags_json, "
        "k.importance, "
        f"CASE WHEN length(CAST(COALESCE(k.metadata_json,'') AS BLOB))"
        f"<={_ENTITY_CARD_METADATA_MAX_BYTES} THEN CASE WHEN json_valid(k.metadata_json) "
        "THEN CASE WHEN json_type(k.metadata_json,'$.document_date')='text' "
        "THEN substr(json_extract(k.metadata_json,'$.document_date'),1,64) ELSE '' END "
        "ELSE '' END ELSE '' END AS document_date, "
        "k.quality_score, substr(k.lifecycle_stage,1,80) AS lifecycle_stage, "
        "substr(k.knowledge_kind,1,80) AS knowledge_kind, "
        "substr(k.created_at,1,64) AS created_at, substr(k.updated_at,1,64) AS updated_at"
    )

    def get_entity_knowledge_cards(
        self, user_id: str, entity_id: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """The same rows as `get_entity_knowledge`, without the document bodies.

        An object view lists documents; it never displays their text. Selecting
        `k.*` for that list put the full `content` of every row into the reply —
        measured at 2.4–4.9 MB for one card on this corpus, of which a reader
        uses the titles. The same oversized payload also reached the model
        through `entity_lookup`, where it was truncated at 11 900 characters
        anyway, so the bytes bought nothing and cost the head of the list.
        """
        rows = self.execute(
            f"""SELECT {self._ENTITY_CARD_COLUMNS}, l.confidence AS _link_confidence
               FROM knowledge_entity_links l
               JOIN knowledge_objects k
                 ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
               JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                    AND {_not_private_entity_material_dependency("e")}
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                f"""SELECT {self._ENTITY_CARD_COLUMNS}, 1.0 AS _link_confidence
                   FROM knowledge_objects k
                   WHERE k.user_id=? AND k.entity_id=? AND k.deleted_at IS NULL
                     AND {_not_private_knowledge_dependency("k")}
                   ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",  # nosec B608
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    # The two halves of `get_entity_knowledge` above, minus its LIMIT: the same
    # link predicate first, the same legacy `knowledge_objects.entity_id` fallback
    # second. Kept as literal SQL rather than derived from a shared string so a
    # future edit to one cannot silently change what the other counts.
    _ENTITY_SUMMARY_LINKED = f"""
        WITH scoped AS (
            SELECT CASE
                     WHEN length(CAST(COALESCE(k.metadata_json,'') AS BLOB))
                            <={_ENTITY_CARD_METADATA_MAX_BYTES}
                     THEN CASE WHEN json_valid(k.metadata_json)
                               THEN CASE
                                      WHEN json_type(k.metadata_json,'$.document_date')='text'
                                      THEN substr(json_extract(k.metadata_json,'$.document_date'),1,64)
                                      ELSE '' END
                               ELSE '' END
                     ELSE '' END AS document_date
             FROM knowledge_entity_links l
              JOIN knowledge_objects k
                ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
             WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted'
               AND k.deleted_at IS NULL
               AND {_not_private_knowledge_dependency("k")}
        )
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN document_date='' THEN 1 ELSE 0 END) AS undated,
               MIN(NULLIF(document_date,'')) AS earliest,
               MAX(NULLIF(document_date,'')) AS latest
          FROM scoped
    """
    _ENTITY_SUMMARY_LINKED_TAGS = f"""
        SELECT DISTINCT substr(CAST(je.value AS TEXT),1,{_ENTITY_SUMMARY_TAG_MAX_CHARS}) AS tag
        FROM knowledge_entity_links l
        JOIN knowledge_objects k
          ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
        JOIN json_each(
            CASE WHEN length(CAST(COALESCE(k.tags_json,'') AS BLOB))<={_ENTITY_CARD_TAGS_MAX_BYTES}
                 THEN CASE WHEN json_valid(k.tags_json)
                           THEN CASE WHEN json_type(k.tags_json)='array'
                                     THEN k.tags_json ELSE '[]' END
                           ELSE '[]' END
                 ELSE '[]' END
        ) je
        WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
          AND {_not_private_knowledge_dependency("k")}
          AND je.type='text' AND trim(CAST(je.value AS TEXT))<>''
        ORDER BY tag COLLATE NOCASE, tag LIMIT ?
    """
    _ENTITY_SUMMARY_DIRECT = f"""
        WITH scoped AS (
            SELECT CASE
                     WHEN length(CAST(COALESCE(metadata_json,'') AS BLOB))
                            <={_ENTITY_CARD_METADATA_MAX_BYTES}
                     THEN CASE WHEN json_valid(metadata_json)
                               THEN CASE
                                      WHEN json_type(metadata_json,'$.document_date')='text'
                                      THEN substr(json_extract(metadata_json,'$.document_date'),1,64)
                                      ELSE '' END
                               ELSE '' END
                     ELSE '' END AS document_date
              FROM knowledge_objects k
             WHERE k.user_id=? AND k.entity_id=? AND k.deleted_at IS NULL
               AND {_not_private_knowledge_dependency("k")}
        )
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN document_date='' THEN 1 ELSE 0 END) AS undated,
               MIN(NULLIF(document_date,'')) AS earliest,
               MAX(NULLIF(document_date,'')) AS latest
          FROM scoped
    """
    _ENTITY_SUMMARY_DIRECT_TAGS = f"""
        SELECT DISTINCT substr(CAST(je.value AS TEXT),1,{_ENTITY_SUMMARY_TAG_MAX_CHARS}) AS tag
        FROM knowledge_objects k
        JOIN json_each(
            CASE WHEN length(CAST(COALESCE(k.tags_json,'') AS BLOB))<={_ENTITY_CARD_TAGS_MAX_BYTES}
                 THEN CASE WHEN json_valid(k.tags_json)
                           THEN CASE WHEN json_type(k.tags_json)='array'
                                     THEN k.tags_json ELSE '[]' END
                           ELSE '[]' END
                 ELSE '[]' END
        ) je
        WHERE k.user_id=? AND k.entity_id=? AND k.deleted_at IS NULL
          AND {_not_private_knowledge_dependency("k")}
          AND je.type='text' AND trim(CAST(je.value AS TEXT))<>''
        ORDER BY tag COLLATE NOCASE, tag LIMIT ?
    """

    def entity_knowledge_summary(self, user_id: str, entity_id: str) -> dict[str, Any]:
        """Tags, date range and counts over EVERY document of an entity.

        Separate from `get_entity_knowledge` on purpose. That one is a *page* —
        the top slice a card shows — and deriving a summary from a page is how a
        card ends up stating "documents: 10" and a date range taken from the ten
        most important documents as if both were facts about the whole entity.
        On this corpus that was measured, not feared: of the 200 entities with the
        most documents, 93 had a wrong date range (worst edge off by 13 years),
        all 200 had an understated count, and tag unions lost a median of 9 tags.

        Cost is a non-issue: `idx_links_entity(user_id, entity_id, status)` covers
        the predicate, measured p50 0.20 ms / max 16 ms on the live-sized copy
        for the widest entity (314 documents).
        """
        row = self.execute(self._ENTITY_SUMMARY_LINKED, (user_id, entity_id)).fetchone()
        tags_sql = self._ENTITY_SUMMARY_LINKED_TAGS
        if not row or not int(row["total"] or 0):
            row = self.execute(self._ENTITY_SUMMARY_DIRECT, (user_id, entity_id)).fetchone()
            tags_sql = self._ENTITY_SUMMARY_DIRECT_TAGS
        total = int(row["total"] or 0) if row else 0
        if not total:
            return {
                "tags": [],
                "tags_matched_at_least": 0,
                "tags_truncated": False,
                "document_date_range": None,
                "documents_without_own_date": 0,
                "total": 0,
            }
        tag_rows = self.execute(
            tags_sql,
            (user_id, entity_id, _ENTITY_SUMMARY_TAG_LIMIT + 1),
        ).fetchall()
        tags = [str(item["tag"]) for item in tag_rows[:_ENTITY_SUMMARY_TAG_LIMIT]]
        earliest, latest = row["earliest"], row["latest"]
        return {
            "tags": tags,
            "tags_matched_at_least": len(tag_rows),
            "tags_truncated": len(tag_rows) > len(tags),
            "document_date_range": (
                {"earliest": str(earliest)[:64], "latest": str(latest)[:64]} if earliest and latest else None
            ),
            "documents_without_own_date": int(row["undated"] or 0),
            "total": total,
        }

    @staticmethod
    def conflict_pair_key(knowledge_a_id: str, knowledge_b_id: str) -> str:
        """Canonical key for an unordered pair — public so a detector can ask about a
        pair it has not stored yet."""
        return "|".join(sorted((knowledge_a_id, knowledge_b_id)))

    def store_knowledge_conflict(
        self,
        user_id: str,
        knowledge_a_id: str,
        knowledge_b_id: str,
        *,
        conflict_type: str = "potential_contradiction",
        confidence: float,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if knowledge_a_id == knowledge_b_id:
            raise ValueError("A knowledge object cannot conflict with itself")
        left = self.get_knowledge_object(knowledge_a_id, user_id)
        right = self.get_knowledge_object(knowledge_b_id, user_id)
        if not left or not right or left.get("deleted_at") or right.get("deleted_at"):
            raise ValueError("Both knowledge objects must belong to the same user")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        pair_key = self.conflict_pair_key(knowledge_a_id, knowledge_b_id)
        # Bound once and reused for both the write and the read-back: the row is unique
        # on (user_id, pair_key, conflict_type), so reading by pair alone can return a
        # DIFFERENT conflict about the same pair.
        normalized_type = str(conflict_type or "potential_contradiction")[:80]
        conflict_id = new_id("conf")
        now = utc_now()
        serialized_evidence = json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True)
        if len(serialized_evidence.encode("utf-8")) > _KNOWLEDGE_CONFLICT_EVIDENCE_MAX_BYTES:
            raise ValueError("Conflict evidence is too large")
        with self.transaction() as conn:
            visible = conn.execute(
                f"""SELECT COUNT(DISTINCT k.id) AS count FROM knowledge_objects k
                     WHERE k.user_id=? AND k.id IN (?, ?) AND k.deleted_at IS NULL
                       AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (user_id, knowledge_a_id, knowledge_b_id),
            ).fetchone()
            if visible is None or int(visible["count"] or 0) != 2:
                raise ValueError("Both knowledge objects must belong to the same user")
            conn.execute(
                """INSERT INTO knowledge_conflicts(
                       id, user_id, knowledge_a_id, knowledge_b_id, pair_key,
                       conflict_type, confidence, evidence_json, status, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'suggested', ?)
                   ON CONFLICT(user_id, pair_key, conflict_type) DO UPDATE SET
                     confidence=MAX(knowledge_conflicts.confidence, excluded.confidence),
                     evidence_json=CASE
                       WHEN excluded.confidence >= knowledge_conflicts.confidence THEN excluded.evidence_json
                       ELSE knowledge_conflicts.evidence_json
                     END""",
                (
                    conflict_id,
                    user_id,
                    knowledge_a_id,
                    knowledge_b_id,
                    pair_key,
                    normalized_type,
                    parsed_confidence,
                    serialized_evidence,
                    now,
                ),
            )
        return self.get_knowledge_conflict_by_pair(user_id, pair_key, normalized_type)

    # Projection shared with ``list_knowledge_conflicts`` so a conflict looks the same
    # whether it was just written or read back from a list.
    # Стадия и «кем погашен» тянутся вместе с заголовком, потому что без них админка
    # физически не может показать, что сторона уже погашена другим решением. Замерено
    # на живой базе: 207 пар дубликатов, и union-find по ним даёт 126 групп — 97 пар,
    # 19 троек, 7 четвёрок и 3 пятёрки. То есть больше половины пар лежат внутри
    # кластеров, где одна сторона могла быть погашена соседним решением, а человек
    # видел бы её как равноправного кандидата.
    # ОДНО определение колонок на три запроса. Их было три копии, и они разошлись:
    # добавленные стадия и «кем погашен» попали в одну, а тест на совпадение форм
    # написан ровно потому, что расхождение здесь незаметно — строка выглядит целой,
    # просто в ней нет пары полей.
    _CONFLICT_COLUMNS = f"""c.id, c.user_id, c.knowledge_a_id, c.knowledge_b_id,
                       substr(c.pair_key,1,360) AS pair_key,
                       substr(c.conflict_type,1,80) AS conflict_type,
                       c.confidence,
                       CASE WHEN length(CAST(COALESCE(c.evidence_json,'') AS BLOB))
                                          <={_KNOWLEDGE_CONFLICT_EVIDENCE_MAX_BYTES}
                                  AND json_valid(c.evidence_json)
                                  AND json_type(c.evidence_json)='object'
                            THEN c.evidence_json ELSE '{{}}' END AS evidence_json,
                       substr(c.status,1,40) AS status,
                       substr(c.created_at,1,64) AS created_at,
                       substr(COALESCE(c.reviewed_at,''),1,64) AS reviewed_at,
                       substr(COALESCE(c.resolution_note,''),1,2000) AS resolution_note,
                       substr(a.title,1,240) AS knowledge_a_title,
                       substr(a.summary,1,500) AS knowledge_a_summary,
                       substr(a.lifecycle_stage,1,80) AS knowledge_a_stage,
                       substr(COALESCE(a.superseded_by_id,''),1,160)
                           AS knowledge_a_superseded_by,
                       substr(b.title,1,240) AS knowledge_b_title,
                       substr(b.summary,1,500) AS knowledge_b_summary,
                       substr(b.lifecycle_stage,1,80) AS knowledge_b_stage,
                       substr(COALESCE(b.superseded_by_id,''),1,160)
                           AS knowledge_b_superseded_by"""
    _CONFLICT_PROJECTION = f"""SELECT {_CONFLICT_COLUMNS}
                FROM knowledge_conflicts c
                JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                     AND {_not_private_knowledge_dependency("a")}
                JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
                     AND {_not_private_knowledge_dependency("b")}"""

    def get_knowledge_conflict_by_pair(
        self, user_id: str, pair_key: str, conflict_type: str
    ) -> dict[str, Any]:
        """Read the one conflict identified by its full unique key.

        ``store_knowledge_conflict`` used to answer this by listing up to 5000 conflicts
        and scanning them in Python — O(n) work, growing, on every write, while conflict
        detection runs per promoted object. It also matched on ``pair_key`` alone, and
        the row is unique on ``(user_id, pair_key, conflict_type)``: with two conflict
        types about the same pair it returned whichever had the higher confidence, not
        the one just written. The lookup uses the leftmost prefix of that UNIQUE index.
        """
        row = self.execute(
            f"{self._CONFLICT_PROJECTION} WHERE c.user_id=? AND c.pair_key=? AND c.conflict_type=?",  # nosec B608
            (user_id, pair_key, conflict_type),
        ).fetchone()
        return dict(row) if row else {}

    # Both joins are FILTERS: INNER, and matching `user_id` on each side drops a
    # conflict whose object is gone or belongs elsewhere. The count uses the same FROM.
    _CONFLICT_FROM = f"""FROM knowledge_conflicts c
                JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                    AND a.deleted_at IS NULL
                    AND {_not_private_knowledge_dependency("a")}
                JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
                    AND b.deleted_at IS NULL
                    AND {_not_private_knowledge_dependency("b")}"""

    @staticmethod
    def _conflict_filter(user_id: str, status: str | None) -> tuple[list[str], list[Any]]:
        allowed = {"suggested", "confirmed", "dismissed", "resolved"}
        clauses = ["c.user_id=?"]
        params: list[Any] = [user_id]
        if status:
            if status not in allowed:
                raise ValueError("Invalid conflict status")
            clauses.append("c.status=?")
            params.append(status)
        return clauses, params

    def count_knowledge_conflicts(self, user_id: str, *, status: str | None = "suggested") -> int:
        clauses, params = self._conflict_filter(user_id, status)
        # ``clauses`` contains only fixed predicates; values remain bound.
        row = self.execute(
            f"SELECT COUNT(*) AS count {self._CONFLICT_FROM} "  # nosec B608
            f"WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_knowledge_conflicts(
        self,
        user_id: str,
        *,
        status: str | None = "suggested",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = self._conflict_filter(user_id, status)
        params.extend([max(1, min(int(limit), 5000)), max(0, offset)])
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT {self._CONFLICT_COLUMNS}
                {self._CONFLICT_FROM}
                WHERE {" AND ".join(clauses)}
                ORDER BY c.confidence DESC, c.created_at DESC, c.id LIMIT ? OFFSET ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_conflict_pair_statuses(self, user_id: str, conflict_type: str) -> dict[str, str]:
        """``pair_key -> status`` for one conflict type, in a single query.

        A detector needs to know which pairs a human has already settled BEFORE it
        proposes them again. Reading the whole map once per run also replaces the
        per-pair full re-listing ``store_knowledge_conflict`` does to return its row.
        """
        rows = self.execute(
            "SELECT pair_key, status FROM knowledge_conflicts WHERE user_id=? AND conflict_type=?",
            (user_id, str(conflict_type)),
        ).fetchall()
        return {str(row["pair_key"]): str(row["status"] or "suggested") for row in rows}

    def get_knowledge_conflict(self, user_id: str, conflict_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"{self._CONFLICT_PROJECTION} WHERE c.id=? AND c.user_id=?",  # nosec B608
            (conflict_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def review_knowledge_conflict(
        self,
        user_id: str,
        conflict_id: str,
        status: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        if status not in {"confirmed", "dismissed", "resolved"}:
            raise ValueError("Invalid conflict review status")
        with self.transaction() as conn:
            row = conn.execute(
                f"""SELECT c.status FROM knowledge_conflicts c
                     JOIN knowledge_objects a
                       ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                      AND a.deleted_at IS NULL
                      AND {_not_private_knowledge_dependency("a")}
                     JOIN knowledge_objects b
                       ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
                      AND b.deleted_at IS NULL
                      AND {_not_private_knowledge_dependency("b")}
                    WHERE c.id=? AND c.user_id=?""",  # nosec B608
                (conflict_id, user_id),
            ).fetchone()
            if row is None:
                return None
            current_status = str(row["status"] or "suggested")
            if current_status == status:
                pass
            elif current_status == "suggested" or (current_status == "confirmed" and status == "resolved"):
                conn.execute(
                    """UPDATE knowledge_conflicts
                       SET status=?, reviewed_at=?, reviewed_by=?, resolution_note=?
                       WHERE id=? AND user_id=?""",
                    (status, utc_now(), reviewed_by, resolution_note[:2000], conflict_id, user_id),
                )
            else:
                raise ValueError(
                    f"Conflict is already {current_status}; only confirmed conflicts may advance to resolved"
                )
        return self.get_knowledge_conflict(user_id, conflict_id)

    def resolve_conflict(
        self,
        user_id: str,
        conflict_id: str,
        winner_id: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        """Resolve one conflict atomically against its current privacy boundary."""

        with self.transaction():
            return self._resolve_conflict_in_transaction(
                user_id,
                conflict_id,
                winner_id,
                reviewed_by=reviewed_by,
                resolution_note=resolution_note,
            )

    def _resolve_conflict_in_transaction(
        self,
        user_id: str,
        conflict_id: str,
        winner_id: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        """Resolve a conflict by choosing a winner; the loser is deprecated.

        Detection and confirmation only flag a contradiction — this is the
        action that actually settles it: the losing Knowledge Object becomes
        ``deprecated`` and points at the winner (``superseded_by_id`` plus a
        ``deprecated_by_conflict`` metadata stamp), and the conflict is marked
        ``resolved``. Provenance is preserved: the loser is versioned, not
        deleted, and can be reactivated by editing it. Ordering (deprecate the
        loser, then flip the conflict) keeps a re-run after a crash idempotent.
        """
        conflict = self.get_knowledge_conflict(user_id, conflict_id)
        if conflict is None:
            return None
        knowledge_a = str(conflict["knowledge_a_id"])
        knowledge_b = str(conflict["knowledge_b_id"])
        if winner_id not in (knowledge_a, knowledge_b):
            raise ValueError("winner_id must be one of the conflicting knowledge objects")
        current_status = str(conflict.get("status") or "suggested")
        if current_status in {"dismissed", "resolved"}:
            raise ValueError(f"Conflict is already {current_status}")
        loser_id = knowledge_b if winner_id == knowledge_a else knowledge_a
        loser = self.get_knowledge_object(loser_id, user_id)
        if loser is None or loser.get("deleted_at"):
            raise ValueError("Losing knowledge object not found")
        # Победитель обязан быть живым. Проверялось только то, что он одна из двух
        # сторон, — а в кластере из трёх-пяти дубликатов сторона могла быть уже
        # погашена соседним решением, и «оставить её» означало бы объявить главной
        # запись, которая сама указывает на другую. Замерено: 110 пар из 207 лежат
        # внутри таких кластеров.
        winner = self.get_knowledge_object(winner_id, user_id)
        if winner is None or winner.get("deleted_at"):
            raise ValueError("Winning knowledge object not found")
        if str(winner.get("lifecycle_stage") or "") == LifecycleStage.DEPRECATED.value:
            raise ValueError(
                "Winner is already deprecated: it was superseded by another decision in this cluster"
            )

        metadata = _json_load(loser.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["deprecated_by_conflict"] = {
            "conflict_id": conflict_id,
            "superseded_by": winner_id,
            "at": utc_now(),
        }
        self.update_knowledge_fields(
            loser_id,
            user_id,
            lifecycle_stage=LifecycleStage.DEPRECATED.value,
            superseded_by_id=winner_id,
            metadata_json=metadata,
        )
        note = (resolution_note or f"kept {winner_id}; deprecated {loser_id}")[:2000]
        self.review_knowledge_conflict(
            user_id, conflict_id, "resolved", reviewed_by=reviewed_by, resolution_note=note
        )
        return {
            "conflict": self.get_knowledge_conflict(user_id, conflict_id),
            "winner_id": winner_id,
            "deprecated_id": loser_id,
        }

    def find_duplicate_candidates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.5,
    ) -> list[EntityResolutionCandidate]:
        """Generate conservative, context-aware duplicate proposals.

        Name similarity is only one signal.  Shared Knowledge Objects and graph neighbours raise
        confidence, while compact identifiers never use fuzzy or prefix matching.  The method only
        creates review candidates; it never performs a merge.

        One pass over the whole blocking-key space, bounded by the pair ceiling —
        the behaviour this method has always had. `sweep_entity_duplicates` walks
        the same space across several calls instead; both go through
        `_duplicate_pass`, so the pair enumeration and the scoring cannot drift
        apart between «everything at once» and «a bit at a time».
        """
        return self._duplicate_pass(user_id, min_confidence=min_confidence)[0]

    def _duplicate_pass(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.5,
        after_key: tuple[int, list[str]] | None = None,
        max_pairs: int | None = None,
    ) -> tuple[list[EntityResolutionCandidate], dict[str, Any]]:
        """The pass itself, optionally resuming and optionally bounded.

        `after_key` is a position in the deterministic strongest-key-first ordering
        of blocking keys, not a row id: what is being walked is the KEY space, and
        a pair examined twice is harmless because `store_resolution_candidate`
        upserts and never reopens a decision a human already made.

        Returns the candidates and a report saying where it stopped and how much of
        the space is left — which is the part that used to exist only as a log line.
        """
        pair_ceiling = _MAX_DUPLICATE_PAIRS if max_pairs is None else max(1, max_pairs)
        # This scan compares blocking keys across the whole tenant graph. A browse
        # page is not a corpus: `list_entities` is intentionally capped at 5000 and
        # used to make every entity after that alphabetical boundary invisible here.
        # One statement also gives the scan a coherent row snapshot; LIMIT/OFFSET
        # pages can shift when an entity is inserted between two reads. Only fields
        # used below are carried across the full walk (not descriptions or bodies).
        entities = [
            dict(row)
            for row in self.execute(
                f"""SELECT substr(e.id,1,160) AS id,
                           substr(e.name,1,240) AS name,
                           substr(e.entity_type,1,80) AS entity_type,
                           CASE WHEN length(CAST(COALESCE(e.aliases_json,'') AS BLOB))
                                          <={_ENTITY_CARD_TAGS_MAX_BYTES}
                                     AND json_valid(e.aliases_json)
                                     AND json_type(e.aliases_json)='array'
                                THEN e.aliases_json ELSE '[]' END AS aliases_json,
                           CASE WHEN length(CAST(COALESCE(e.metadata_json,'') AS BLOB))
                                          <={_ENTITY_CARD_METADATA_MAX_BYTES}
                                     AND json_valid(e.metadata_json)
                                     AND json_type(e.metadata_json)='object'
                                THEN e.metadata_json ELSE '{{}}' END AS metadata_json
                   FROM entities e
                   WHERE e.user_id=? AND e.deleted_at IS NULL AND e.canonical=1
                     AND {_not_private_entity_material_dependency("e")}
                   ORDER BY e.name COLLATE NOCASE, e.id""",  # nosec B608
                (user_id,),
            )
        ]
        knowledge_by_entity: dict[str, set[str]] = {}
        for row in self.execute(
            f"""SELECT substr(l.entity_id,1,160) AS entity_id,
                       substr(l.knowledge_object_id,1,160) AS knowledge_object_id
                  FROM knowledge_entity_links l
                  JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                       AND {_not_private_entity_material_dependency("e")}
                  JOIN knowledge_objects k
                    ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                   AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                 WHERE l.user_id=? AND l.status='accepted'""",  # nosec B608
            (user_id,),
        ).fetchall():
            knowledge_by_entity.setdefault(str(row["entity_id"]), set()).add(str(row["knowledge_object_id"]))
        neighbours_by_entity: dict[str, set[str]] = {}
        for row in self.execute(
            f"""SELECT substr(r.source_entity_id,1,160) AS source_entity_id,
                       substr(r.target_entity_id,1,160) AS target_entity_id
                  FROM relations r
                  JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                       AND {_not_private_entity_material_dependency("s")}
                  JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                       AND {_not_private_entity_material_dependency("t")}
                 WHERE r.user_id=? AND r.deleted_at IS NULL
                   AND {_not_private_relation_dependency("r")}""",  # nosec B608
            (user_id,),
        ).fetchall():
            source = str(row["source_entity_id"])
            target = str(row["target_entity_id"])
            neighbours_by_entity.setdefault(source, set()).add(target)
            neighbours_by_entity.setdefault(target, set()).add(source)

        def variants(entity: dict[str, Any]) -> list[str]:
            values = [str(entity.get("name") or "")]
            values.extend(str(item) for item in _json_load(entity.get("aliases_json"), []))
            return [normalize_entity_name(value) for value in values if normalize_entity_name(value)]

        def acronym(name: str) -> str:
            """Первые буквы слов — но только БУКВЫ и только если их хватает.

            «Калининск 17» и «Кемерово 17» давали одинаковое «к1»: первая буква
            слова плюс цифра. Это не аббревиатура, а совпадение первой буквы у
            разных городов с одним индексом — и оно ставило паре 0.82.
            """
            tokens = [token for token in re.split(r"\s+", name) if token and token[0].isalpha()]
            if len(tokens) < 2:
                return ""
            return "".join(token[0] for token in tokens).casefold()

        def overlap(left: set[str], right: set[str]) -> float:
            union = left | right
            return len(left & right) / len(union) if union else 0.0

        candidates: list[EntityResolutionCandidate] = []
        min_confidence = max(0.0, min(1.0, float(min_confidence)))
        prepared: list[dict[str, Any]] = [
            {
                "entity": entity,
                "variants": variants(entity),
                # Counted once per entity, not once per pair: the multiset bound
                # below is only cheap if this is not rebuilt 200 000 times.
                "counts": [Counter(variant) for variant in variants(entity)],
                "identifier": any(
                    _is_entity_identifier(str(value))
                    for value in [entity.get("name", ""), *_json_load(entity.get("aliases_json"), [])]
                ),
            }
            for entity in entities
        ]
        # Blocking, not all-pairs. The exhaustive scan is quadratic in entity count
        # with three-plus SequenceMatcher calls per surviving pair, and it runs from
        # an agent tool call and two HTTP routes as well as a worker. Measured on a
        # synthetic corpus of 2000 entities: **94 seconds**, on the event loop, with
        # `list_entities(limit=5000)` allowing well over twice that.
        blocks: dict[tuple[str, ...], list[int]] = {}
        for index, data in enumerate(prepared):
            for key in _blocking_keys(str(data["entity"]["entity_type"]), data["variants"]):
                blocks.setdefault(key, []).append(index)

        # No block is dropped for being large. Skipping a crowded bucket was the
        # obvious way to bound the work and it is the wrong one: a pair whose only
        # shared key lives in that bucket disappears from the proposals with nothing
        # to show for it — silent truncation, dressed as an optimisation. The pruning
        # is done instead by `_ratio_ceiling` below, which is exact.
        # Each pair remembers the STRONGEST key that introduced it, so the ceiling
        # below removes the flimsiest evidence first rather than whatever happens to
        # sort last. A shared alias outranks a shared word, which outranks two
        # adjacent characters.
        # Blocks are consumed strongest-key-first and enumeration stops at the
        # ceiling, so the ceiling bounds wall time and not merely the scoring: on a
        # corpus whose entity names share common words, 2000 entities produce 1.7
        # MILLION candidate pairs, and simply building that set costs more than
        # scoring the ones worth scoring.
        ordered: list[tuple[int, int]] = []
        seen_pairs: set[tuple[int, int]] = set()
        truncated = False
        # Пара двух объявленных ФИО отбрасывается ЗДЕСЬ, при наборе, а не ниже при
        # оценке. Разница не косметическая: бюджет `pair_ceiling` тратится на наборе, и
        # замерено на живом архиве — набор упирался в потолок на 214 323 парах и объявлял
        # список неполным, при том что почти все набранные пары ниже отбрасывались как
        # раз этим правилом. То есть настоящие кандидаты вытеснялись теми, которые всё
        # равно не могли стать кандидатами.
        #
        # Ключ `variant` означает общий псевдоним — прямое утверждение человека «это один
        # и тот же», и такие пары проходят. Ключи перебираются сильнейшим первым
        # (`_KEY_RANK`, variant = 0), поэтому пара с общим псевдонимом всегда вносится
        # именно этим ключом, и проверка по имени ключа здесь точна, а не приблизительна.
        declared_person = [_is_declared_person(data["entity"]) for data in prepared]
        ranked = sorted(
            ((_KEY_RANK.get(key[0], len(_KEY_RANK)), list(key), key) for key in blocks),
            key=lambda item: (item[0], item[1]),
        )
        resume_after = (after_key[0], after_key[1]) if after_key else None
        remaining = [item for item in ranked if resume_after is None or (item[0], item[1]) > resume_after]
        keys_total = len(ranked)
        keys_done = 0
        stopped_at: tuple[int, list[str]] | None = None
        for rank, key_list, key in remaining:
            # Бюджет проверяется МЕЖДУ ключами, а не внутри. Обрыв на середине
            # перечисления одного ключа означал бы, что курсор встаёт за ключ,
            # часть пар которого не рассматривалась, — и они не рассматривались бы
            # уже никогда. Оракульный тест поймал ровно это: 362 потерянные пары.
            # Ключ поэтому либо пройден целиком, либо не начат; цена — перебор
            # может превысить бюджет на один блок.
            if len(ordered) > pair_ceiling:
                truncated = True
                break
            keys_done += 1
            stopped_at = (rank, key_list)
            members = blocks[key]
            alias_key = key[0] == "variant"
            for position, left_index in enumerate(members):
                for right_index in members[position + 1 :]:
                    pair = (left_index, right_index)
                    if pair in seen_pairs:
                        continue
                    if not alias_key and declared_person[left_index] and declared_person[right_index]:
                        # Намеренно НЕ помечается как `seen`. В одном прогоне это
                        # безразлично — ключи `variant` идут первыми и своё уже внесли.
                        # Но обход возобновляемый: на продолжении с курсором сильные
                        # ключи остались в прошлом вызове, а `seen_pairs` живёт только
                        # внутри вызова. Пометить сейчас значило бы закрыть паре дорогу
                        # на случай, которого мы не проверяли.
                        continue
                    seen_pairs.add(pair)
                    ordered.append(pair)
        report: dict[str, Any] = {
            "entities": len(entities),
            "pairs_examined": len(ordered),
            "keys_total": keys_total,
            "keys_examined": keys_done,
            # Осталось необойдённым — то самое, что раньше существовало только
            # строкой в логе. Пустой список предложений при `keys_pending > 0`
            # означает «ещё не смотрели», а не «дубликатов нет».
            "keys_pending": max(0, len(remaining) - keys_done),
            "partial": truncated,
            "stopped_at": list(stopped_at) if truncated and stopped_at else None,
        }
        if truncated:
            # Said out loud. The scan is quadratic in entity count with several
            # SequenceMatcher calls per surviving pair; an exhaustive run over those
            # 2000 entities takes **166 seconds**. Returning a short list in silence
            # would let the reviewer believe there is nothing more to merge — so the
            # cheapest evidence (two adjacent characters) is what gets dropped, and
            # the fact that anything was dropped is a warning.
            LOGGER.warning(
                "duplicate detection stopped at %d candidate pairs — "
                "the proposal list is PARTIAL; weakest evidence was dropped first",
                len(ordered),
            )

        for left_index, right_index in ordered:
            left_data = prepared[left_index]
            right_data = prepared[right_index]
            left = left_data["entity"]
            right = right_data["entity"]
            left_variants = left_data["variants"]
            right_variants = right_data["variants"]
            left_name = left_variants[0] if left_variants else ""
            right_name = right_variants[0] if right_variants else ""
            if left["entity_type"] != right["entity_type"] or not left_name or not right_name:
                continue

            exact_alias = bool(set(left_variants) & set(right_variants))
            # ОБЪЯВЛЕННОЕ ФИО — само по себе утверждение личности, и два РАЗНЫХ таких
            # имени означают двух разных людей. Нечёткое сходство здесь не улика:
            # русские ФИО делят между собой почти всю структуру, а `context_boost` за
            # «общие документы» на штатном расписании означает всего лишь «оба в одном
            # списке» — тот же концентратор, что губит графовый канал.
            #
            # Замерено на живой базе сразу после прохода правилом ФИО: очередь слияний
            # выросла с 20 пар до 45 061, и 45 041 из них (100.0%) — пары, где ОБА узла
            # заведены объявляющим правилом. 78% имели уверенность ниже 0.80, а у одной
            # сущности набралось 173 пары. Такую очередь человек не разберёт никогда.
            #
            # Цена ошибки несимметрична: два дубликата — неудобство, а слитые в один
            # узел два РАЗНЫХ человека — порча данных, и откатить её нечем (функции
            # разъединения в системе нет, проверено grep'ом по undo|unmerge|split).
            #
            # Совпадение по псевдониму пропускается: псевдоним заводит человек, и это
            # его прямое утверждение «это один и тот же».
            if not exact_alias and _is_declared_person(left) and _is_declared_person(right):
                continue
            # Codes, tickers, contract identifiers, and versioned names are exact-match only.
            if (left_data["identifier"] or right_data["identifier"]) and not exact_alias:
                continue

            left_tokens = set(left_name.split())
            right_tokens = set(right_name.split())
            # Те же слова в другом порядке — правило про ОДНО имя, записанное иначе
            # («Хасанов Руслан Рашитович» ⟷ «Руслан Рашитович Хасанов»). Считать его
            # по морфологически свёрнутым токенам нельзя: свёртка тянет фамилию к
            # имени того же корня — «Иванов» → «иван», «Сергеев» → «серг», — и два
            # РАЗНЫХ человека получают одинаковый набор. Именно так «Иванов Сергей
            # Александрович ⟷ Сергеев Иван Александрович» попадал в /merges третьей
            # строкой с уверенностью 0.94.
            #
            # Сегодня эта пара отсекается и более сильным правилом ниже (общим должно
            # быть содержательное слово, а не отчество), и на боевом корпусе замер даёт
            # 19 кандидатур при обоих вариантах — проверено. Сырые токены оставлены
            # намеренно: правило говорит «то же имя», и считать его по свёрнутым
            # формам неверно по существу, независимо от того, страхует ли его сосед.
            left_raw = {token.casefold() for token in str(left.get("name") or "").split()}
            right_raw = {token.casefold() for token in str(right.get("name") or "").split()}
            # Номер — это и есть различие. «в/ч 01688» и «в/ч 03079» совпадают всем,
            # кроме единственного, что их различает, и общая похожесть строк ставила
            # им 0.91: на боевом корпусе 149 таких сущностей, и очередь слияний
            # заполнялась парами разных воинских частей. Пропускаем пару, если числа
            # есть у ОБОИХ и не совпадают ни одно; когда номер только у одного
            # («Отдел» и «Отдел 5»), правило молчит — там решает остальное.
            left_numbers = set(_NUMBER_RE.findall(str(left.get("name") or "")))
            right_numbers = set(_NUMBER_RE.findall(str(right.get("name") or "")))
            if left_numbers and right_numbers and not (left_numbers & right_numbers):
                continue
            # У людей отчество — не улика: оно общее у множества неродственных ФИО
            # («Анатольевич» встречается в архиве десятками), и пара, у которой
            # совпало ТОЛЬКО оно, — это два разных человека. Замерено: из 878 пар с
            # уверенностью ≥ 0.85 у 375 не было ни одного общего слова вовсе, а
            # среди остальных заметная часть держалась на одном отчестве.
            token_jaccard = overlap(left_tokens, right_tokens)
            acronym_match = bool(
                acronym(left_name)
                and acronym(left_name) == acronym(right_name)
                and len(left_tokens) >= 2
                and len(right_tokens) >= 2
            )
            # Только для МНОГОСЛОВНЫХ имён: у однословных общих токенов нет по
            # определению, и там решает посимвольное сходство — «Зюзюкинск» и
            # «Зюзюкинец» это опечатка, а не два разных объекта.
            if not exact_alias and not acronym_match and len(left_raw) > 1 and len(right_raw) > 1:
                # Общим должно быть хоть одно СОДЕРЖАТЕЛЬНОЕ слово. Не в счёт:
                #   • числа — «Калининск 17» и «Кемерово 17» это разные города,
                #     совпавшие индексом;
                #   • отчества — «Анатольевич» встречается в архиве десятками, и
                #     пара, державшаяся только на нём, — два разных человека.
                # У людей сравниваются СЫРЫЕ слова: морфология тянет фамилию к
                # имени того же корня («Иванов»→«иван», «Сергеев»→«серг») и
                # склеивает разных людей. У остальных — свёрнутые, потому что там
                # она делает ровно свою работу: «ПОДПИСКА» и «ПОДПИСКУ» — одно.
                both_people = (
                    str(left.get("entity_type") or "") == "person"
                    and str(right.get("entity_type") or "") == "person"
                )
                shared = (left_raw & right_raw) if both_people else (left_tokens & right_tokens)
                meaningful = {
                    token
                    for token in shared
                    if len(token) > 2 and not token.isdigit() and not _PATRONYMIC_RE.search(token)
                }
                if not meaningful:
                    continue
            if not exact_alias and not (left_raw == right_raw and len(left_raw) >= 2):
                # Exact ceiling before spending three-plus SequenceMatcher calls.
                # `ratio()` is 2·M/(len(a)+len(b)) and matched characters cannot
                # exceed the shorter string, so `_ratio_ceiling` bounds it from above
                # for free — and `sorted_similarity` compares strings of the same
                # lengths, so the same bound holds. Adding the context boost's maximum
                # keeps this an over-estimate, so nothing that could have qualified is
                # skipped: the candidate set is unchanged, only the arithmetic is.
                left_counts = left_data["counts"]
                right_counts = right_data["counts"]
                name_ceiling = (
                    _ratio_ceiling(left_name, right_name, left_counts[0], right_counts[0])
                    if left_counts and right_counts
                    else 0.0
                )
                ceiling = max(
                    name_ceiling * 0.78,
                    token_jaccard * 0.90,
                    max(
                        (
                            _ratio_ceiling(left_variant, right_variant, left_count, right_count)
                            for left_variant, left_count in zip(left_variants, left_counts, strict=True)
                            for right_variant, right_count in zip(right_variants, right_counts, strict=True)
                        ),
                        default=0.0,
                    )
                    * 0.76,
                    0.82 if acronym_match else 0.0,
                )
                if min(0.97, ceiling + 0.14) < min_confidence:
                    continue

            name_similarity = SequenceMatcher(None, left_name, right_name).ratio()
            sorted_similarity = SequenceMatcher(
                None,
                " ".join(sorted(left_tokens)),
                " ".join(sorted(right_tokens)),
            ).ratio()
            alias_similarity = max(
                (
                    SequenceMatcher(None, left_variant, right_variant).ratio()
                    for left_variant in left_variants
                    for right_variant in right_variants
                ),
                default=0.0,
            )
            shared_knowledge = overlap(
                knowledge_by_entity.get(str(left["id"]), set()),
                knowledge_by_entity.get(str(right["id"]), set()),
            )
            shared_neighbours = overlap(
                neighbours_by_entity.get(str(left["id"]), set()),
                neighbours_by_entity.get(str(right["id"]), set()),
            )

            if exact_alias:
                confidence = 0.995
                method = "exact_name_or_alias"
            elif left_raw == right_raw and len(left_raw) >= 2:
                confidence = 0.94
                method = "same_tokens_different_order"
            else:
                confidence = max(
                    name_similarity * 0.70,
                    sorted_similarity * 0.78,
                    token_jaccard * 0.90,
                    alias_similarity * 0.76,
                    0.82 if acronym_match else 0.0,
                )
                context_boost = min(0.14, shared_knowledge * 0.09 + shared_neighbours * 0.07)
                confidence = min(0.97, confidence + context_boost)
                method = "name_alias_and_graph_evidence"

            # A single generic token needs very strong evidence; fuzzy short names create noise.
            if len(left_tokens) == len(right_tokens) == 1 and not exact_alias:
                if min(len(left_name), len(right_name)) < 5:
                    confidence *= 0.72
                if shared_knowledge == 0 and shared_neighbours == 0:
                    confidence *= 0.88
            if confidence < min_confidence:
                continue
            candidates.append(
                EntityResolutionCandidate(
                    id=new_id("er"),
                    user_id=user_id,
                    entity_a_id=left["id"],
                    entity_b_id=right["id"],
                    confidence=round(confidence, 6),
                    resolution_method=method,
                    evidence_json={
                        "left_name": left["name"],
                        "right_name": right["name"],
                        "name_similarity": round(name_similarity, 4),
                        "sorted_token_similarity": round(sorted_similarity, 4),
                        "token_jaccard": round(token_jaccard, 4),
                        "alias_similarity": round(alias_similarity, 4),
                        "exact_alias": exact_alias,
                        "acronym_match": acronym_match,
                        "shared_knowledge": round(shared_knowledge, 4),
                        "shared_graph_neighbours": round(shared_neighbours, 4),
                        # Complete scoring inputs, not only the derived scalar.
                        # A later quarantine can therefore invalidate this exact
                        # proposal before it authorizes a merge.
                        "knowledge_object_ids": sorted(
                            knowledge_by_entity.get(str(left["id"]), set())
                            | knowledge_by_entity.get(str(right["id"]), set())
                        ),
                        "graph_neighbour_entity_ids": sorted(
                            neighbours_by_entity.get(str(left["id"]), set())
                            | neighbours_by_entity.get(str(right["id"]), set())
                        ),
                        "identifier_safe": not (left_data["identifier"] or right_data["identifier"]),
                    },
                )
            )
        candidates.sort(key=lambda item: item.confidence, reverse=True)
        report["candidates"] = len(candidates)
        return candidates, report

    _SWEEP_KEY = "entity_dedup:cursor:"

    _MENTION_SWEEP_KEY = "graph:mention_backfill:"

    def backfill_entity_mentions(
        self,
        user_id: str,
        *,
        max_documents: int = 200,
        max_seconds: float = 15.0,
        max_links: int = 50,
    ) -> dict[str, Any]:
        """Cooperatively link old documents to entities created after ingestion.

        Candidate discovery stays inverted (bounded n-grams from the document,
        then bounded tenant-row pages), but candidates are spooled as numeric
        rowid markers instead of being silently capped.  Literal matching has
        character/entity/material cursors.  Inflected matching sees the complete
        candidate set for each small text window before applying longest-first
        occupancy.  Every durable payload contains technical numbers only.

        The awaiting asyncio task cannot cancel a Python thread already inside
        this method.  A wall-clock deadline is therefore observed *between* small
        SQLite/regex units; it is not expected to interrupt an unbounded unit.
        """

        from friday.entity_phrases import mention_phrase_candidate_page
        from friday.mentions import (
            MAX_INFLECTED_NAME_TOKENS,
            exact_mentions_page,
            inflected_mentions_present_tokens,
            inflected_mentions_tokens,
            inflected_token_position_page,
        )

        seconds = float(max_seconds)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("max_seconds must be a finite positive number")
        deadline = monotonic() + min(seconds, 60.0)
        link_limit = int(max_links)
        if link_limit <= 0:
            raise ValueError("max_links must be a positive number")
        link_limit = min(link_limit, 2000)
        document_limit = max(1, min(int(max_documents), 2000))

        discovery_page = 8
        document_scan_page = 8
        cleanup_page_size = 64
        content_page_chars = 8_192
        phrase_read_chars = 16_386
        exact_halo_chars = 8_193
        token_read_chars = content_page_chars + 242
        inflected_owned_tokens = 32
        inflected_context_tokens = MAX_INFLECTED_NAME_TOKENS - 1
        maximum_token_positions = inflected_context_tokens + inflected_owned_tokens + inflected_context_tokens
        maximum_winner_rowids = maximum_token_positions // 2 + 1

        sqlite_integer_max = (1 << 63) - 1

        def parsed_numeric(value: object) -> int | None:
            if isinstance(value, bool):
                parsed = int(value)
            elif isinstance(value, int):
                parsed = value
            elif isinstance(value, str):
                try:
                    parsed = int(value)
                except ValueError:
                    return None
            else:
                return None
            return parsed if 0 <= parsed <= sqlite_integer_max else None

        def numeric(value: object, default: int = 0) -> int:
            parsed = parsed_numeric(value)
            return parsed if parsed is not None else default

        def entity_authority(entity_id: object, version: object) -> int:
            identifier = str(entity_id)
            payload = f"{len(identifier)}:{identifier}:{numeric(version, 1)}".encode()
            authority = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
            return (authority & sqlite_integer_max) or 1

        cursor = 0
        linked = 0
        scanned = 0
        validated_context_key: tuple[int, ...] | None = None
        state: dict[str, Any] = {}
        try:
            stored = self.kv_get(self._MENTION_SWEEP_KEY + user_id)
            loaded = json.loads(stored) if stored else {}
            state = loaded if isinstance(loaded, dict) else {}
            cursor = numeric(state.get("rowid"))
        except (TypeError, ValueError, AttributeError):
            state = {}
            cursor = 0
        pending = state.get("pending")
        if not isinstance(pending, dict):
            pending = {}
        raw_count_cursor = parsed_numeric(state.get("entity_count_cursor", 0))
        raw_count_total = parsed_numeric(state.get("entity_count_total", 0))
        raw_count_complete = parsed_numeric(state.get("entity_count_complete", 0))
        count_state_valid = (
            raw_count_cursor is not None and raw_count_total is not None and raw_count_complete in {0, 1}
        )
        if count_state_valid:
            assert raw_count_cursor is not None
            assert raw_count_total is not None
            assert raw_count_complete is not None
            entity_count_cursor = raw_count_cursor
            entity_count_total = raw_count_total
            entity_count_complete = bool(raw_count_complete)
        else:
            entity_count_cursor = 0
            entity_count_total = 0
            entity_count_complete = False

        phases = {
            "discover",
            "discover_fallback",
            "exact",
            "inflected_tokenize",
            "inflected_validate",
            "inflected_collect",
            "inflected_resolve",
            "inflected_link",
            "present_cleanup",
            "present_restart_cleanup",
            "restart_cleanup",
            "cleanup",
        }

        def numeric_list(value: object, *, limit: int = maximum_winner_rowids) -> list[int]:
            if not isinstance(value, list) or len(value) > limit:
                return []
            result_ids: list[int] = []
            seen: set[int] = set()
            for item in value:
                position = numeric(item)
                if position and position not in seen:
                    seen.add(position)
                    result_ids.append(position)
            return result_ids

        def aligned_winner_lists(
            rowids_value: object,
            versions_value: object,
            authorities_value: object,
        ) -> tuple[list[int], list[int], list[int]]:
            rowids = numeric_list(rowids_value)
            if (
                not isinstance(versions_value, list)
                or not isinstance(authorities_value, list)
                or len(versions_value) > maximum_winner_rowids
                or len(authorities_value) > maximum_winner_rowids
            ):
                return [], [], []
            versions = [numeric(item) for item in versions_value]
            authorities = [numeric(item) for item in authorities_value]
            if (
                len(rowids) != len(versions)
                or len(rowids) != len(authorities)
                or any(version <= 0 for version in versions)
                or any(authority <= 0 for authority in authorities)
            ):
                return [], [], []
            return rowids, versions, authorities

        def phrase_state(value: object) -> dict[str, int]:
            raw = value if isinstance(value, Mapping) else {}
            result_state = {
                "char": numeric(raw.get("char")),
                "byte": numeric(raw.get("byte")),
                "length": max(1, numeric(raw.get("length"), 1)),
                "skip": min(1, numeric(raw.get("skip"))),
            }
            if result_state["char"] and not result_state["byte"]:
                return {"char": 0, "byte": 0, "length": 1, "skip": 0}
            return result_state

        def exact_state(value: object) -> dict[str, int]:
            raw = value if isinstance(value, Mapping) else {}
            result_state = {
                "char": numeric(raw.get("char")),
                "byte": numeric(raw.get("byte")),
                "entity": numeric(raw.get("entity")),
                "material": numeric(raw.get("material")),
            }
            if result_state["char"] and not result_state["byte"]:
                return {"char": 0, "byte": 0, "entity": 0, "material": 0}
            return result_state

        def scan_state(value: object) -> dict[str, int]:
            raw = value if isinstance(value, Mapping) else {}
            result_state = {
                "char": numeric(raw.get("char")),
                "byte": numeric(raw.get("byte")),
                "skip": min(1, numeric(raw.get("skip"))),
            }
            if result_state["char"] and not result_state["byte"]:
                return {"char": 0, "byte": 0, "skip": 0}
            return result_state

        def token_positions(value: object) -> list[int]:
            if not isinstance(value, list) or len(value) > maximum_token_positions * 2:
                return []
            clean: list[int] = []
            previous_end = -1
            for index in range(0, len(value), 2):
                if index + 1 >= len(value):
                    return []
                start = numeric(value[index])
                end = numeric(value[index + 1])
                if start < previous_end or not start <= end or end - start > 960:
                    return []
                clean.extend((start, end))
                previous_end = end
            return clean

        def token_fields(value: object) -> dict[str, Any]:
            raw = value if isinstance(value, Mapping) else {}
            positions = token_positions(raw.get("token_positions"))
            offset = numeric(raw.get("owned_offset"))
            if offset > len(positions) // 2:
                offset = 0
                positions = []
            return {
                "scan_cursor": scan_state(raw.get("scan_cursor")),
                "token_positions": positions,
                "owned_offset": offset,
                "token_eof": min(1, numeric(raw.get("token_eof"))),
            }

        def token_work(phase: str, source: object, **extra: Any) -> dict[str, Any]:
            result_work: dict[str, Any] = {"phase": phase, **token_fields(source)}
            result_work.update(extra)
            return result_work

        def token_context_key(
            position: int,
            document_version: int,
            source: object,
        ) -> tuple[int, ...]:
            fields = token_fields(source)
            return (
                position,
                document_version,
                int(fields["owned_offset"]),
                *fields["token_positions"],
            )

        def clean_work(value: object) -> dict[str, Any]:
            raw = value if isinstance(value, Mapping) else {}
            phase = str(raw.get("phase") or "discover")
            if phase not in phases:
                return {
                    "phase": "discover",
                    "phrase_cursor": phrase_state(None),
                    "entity_scan_rowid": 0,
                }
            if phase == "discover":
                return {
                    "phase": phase,
                    "phrase_cursor": phrase_state(raw.get("phrase_cursor")),
                    "entity_scan_rowid": numeric(raw.get("entity_scan_rowid")),
                }
            if phase == "discover_fallback":
                return {
                    "phase": phase,
                    "entity_scan_rowid": numeric(raw.get("entity_scan_rowid")),
                }
            if phase == "exact":
                return {
                    "phase": phase,
                    "candidate_rowid": numeric(raw.get("candidate_rowid")),
                    "exact_cursor": exact_state(raw.get("exact_cursor")),
                }
            if phase == "inflected_tokenize":
                return token_work(phase, raw)
            if phase == "inflected_validate":
                if "token_positions" not in raw:
                    return token_work("inflected_tokenize", {})
                return token_work(
                    phase,
                    raw,
                    validation_index=numeric(raw.get("validation_index")),
                    validation_byte=numeric(raw.get("validation_byte")),
                    validation_skip=min(1, numeric(raw.get("validation_skip"))),
                )
            if phase == "inflected_collect":
                # Character-window checkpoints predate the numeric token stream
                # and cannot be translated without re-reading private text.
                if "token_positions" not in raw:
                    return token_work("inflected_tokenize", {})
                return token_work(
                    phase,
                    raw,
                    entity_scan_rowid=numeric(raw.get("entity_scan_rowid")),
                )
            if phase == "inflected_resolve":
                if "token_positions" not in raw:
                    return token_work("inflected_tokenize", {})
                winners, versions, authorities = aligned_winner_lists(
                    raw.get("winner_rowids"),
                    raw.get("winner_versions"),
                    raw.get("winner_authorities"),
                )
                return token_work(
                    phase,
                    raw,
                    priority_cursor=numeric(raw.get("priority_cursor")),
                    candidate_rowid=numeric(raw.get("candidate_rowid")),
                    winner_rowids=winners,
                    winner_versions=versions,
                    winner_authorities=authorities,
                )
            if phase == "inflected_link":
                if "token_positions" not in raw:
                    return token_work("inflected_tokenize", {})
                winners, versions, authorities = aligned_winner_lists(
                    raw.get("winner_rowids"),
                    raw.get("winner_versions"),
                    raw.get("winner_authorities"),
                )
                return token_work(
                    phase,
                    raw,
                    winner_rowids=winners,
                    winner_versions=versions,
                    winner_authorities=authorities,
                    winner_cursor=numeric(raw.get("winner_cursor")),
                )
            if phase in {"present_cleanup", "present_restart_cleanup"}:
                if "token_positions" not in raw:
                    return token_work("inflected_tokenize", {})
                return token_work(phase, raw)
            if phase == "restart_cleanup":
                return {
                    "phase": phase,
                    "cleanup_cursor": numeric(raw.get("cleanup_cursor")),
                }
            return {
                "phase": "cleanup",
                "cleanup_cursor": numeric(raw.get("cleanup_cursor")),
            }

        def result(
            *,
            linked: int,
            scanned: int,
            complete: bool,
            entities: int,
            budget_reason: str | None = None,
        ) -> dict[str, Any]:
            return {
                "linked": linked,
                "scanned": scanned,
                "complete": complete,
                "entities": entities,
                "budget_exhausted": budget_reason is not None,
                "budget_reason": budget_reason,
                "cursor": cursor,
                "has_more": not complete,
            }

        def save_cursor() -> None:
            self.kv_set(
                self._MENTION_SWEEP_KEY + user_id,
                json.dumps(
                    {
                        "entity_count_complete": int(entity_count_complete),
                        "entity_count_cursor": entity_count_cursor,
                        "entity_count_total": entity_count_total,
                        "rowid": cursor,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )

        def pending_value(
            position: int,
            document_version: int,
            work: Mapping[str, Any],
        ) -> str:
            return json.dumps(
                {
                    "pending": {
                        "document_rowid": position,
                        "document_version": document_version,
                        "work": clean_work(work),
                    },
                    "entity_count_complete": int(entity_count_complete),
                    "entity_count_cursor": entity_count_cursor,
                    "entity_count_total": entity_count_total,
                    "rowid": cursor,
                },
                separators=(",", ":"),
                sort_keys=True,
            )

        def write_pending(
            conn: Any,
            position: int,
            document_version: int,
            work: Mapping[str, Any],
        ) -> None:
            conn.execute(
                """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                (
                    self._MENTION_SWEEP_KEY + user_id,
                    pending_value(position, document_version, work),
                    utc_now(),
                ),
            )

        def save_pending(
            position: int,
            document_version: int,
            work: Mapping[str, Any],
        ) -> None:
            # Do not serialise a caller-provided mapping.  Rebuilding the schema
            # here guarantees that even a legacy/forged checkpoint cannot make a
            # name, alias, text fragment or match span durable.
            self.kv_set(
                self._MENTION_SWEEP_KEY + user_id,
                pending_value(position, document_version, work),
            )

        def new_work(phrase_cursor: Mapping[str, object] | None = None) -> dict[str, Any]:
            return {
                "phase": "discover",
                "phrase_cursor": dict(phrase_cursor or {"char": 0, "byte": 0, "length": 1, "skip": 0}),
                "entity_scan_rowid": 0,
            }

        def candidate_prefix(position: int, document_version: int) -> str:
            # Length-prefix the already-technical tenant id so embedded colons
            # cannot make two tenants share a candidate namespace.
            return (
                f"{self._MENTION_SWEEP_KEY}candidate:{len(user_id):08d}:{user_id}:"
                f"{position:020d}:{document_version:020d}:"
            )

        def candidate_document_prefix(position: int) -> str:
            return f"{self._MENTION_SWEEP_KEY}candidate:{len(user_id):08d}:{user_id}:{position:020d}:"

        def candidate_key(position: int, document_version: int, entity_rowid: int) -> str:
            return candidate_prefix(position, document_version) + f"{entity_rowid:020d}"

        def store_candidate_rows(
            position: int,
            document_version: int,
            rows: Sequence[Mapping[str, Any]],
            *,
            conn: Any,
        ) -> None:
            values = {
                int(item["position"]): entity_authority(item["id"], item["version"])
                for item in rows
                if int(item["position"]) > 0 and str(item.get("id") or "")
            }
            if not values:
                return
            now = utc_now()
            conn.executemany(
                """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [
                    (
                        candidate_key(position, document_version, entity_rowid),
                        str(authority),
                        now,
                    )
                    for entity_rowid, authority in sorted(values.items())
                ],
            )

        def candidate_entries(
            position: int,
            document_version: int,
            *,
            after: int,
            limit: int,
        ) -> list[tuple[int, int]]:
            prefix = candidate_prefix(position, document_version)
            lower = prefix + (f"{after:020d}" if after else "")
            rows = self.execute(
                """SELECT key,value FROM runtime_kv
                   WHERE key>? AND key<? ORDER BY key LIMIT ?""",
                (lower, prefix + "\uffff", max(1, min(int(limit), 64))),
            ).fetchall()
            found: list[tuple[int, int]] = []
            for item in rows:
                try:
                    authority = parsed_numeric(item["value"])
                    if authority:
                        found.append((int(str(item["key"]).rsplit(":", 1)[-1]), authority))
                except (TypeError, ValueError):
                    continue
            return found

        def candidate_cursor_is_valid(
            position: int,
            document_version: int,
            candidate_rowid: int,
        ) -> bool:
            if candidate_rowid == 0:
                return True
            row = self.execute(
                "SELECT value FROM runtime_kv WHERE key=?",
                (candidate_key(position, document_version, candidate_rowid),),
            ).fetchone()
            candidates = load_candidate_rows([candidate_rowid])
            return bool(
                row is not None
                and len(candidates) == 1
                and numeric(row["value"]) == entity_authority(candidates[0]["id"], candidates[0]["version"])
            )

        def delete_candidate_page(
            position: int,
            document_version: int,
            *,
            after: int,
        ) -> tuple[int, bool]:
            prefix = candidate_prefix(position, document_version)
            lower = prefix + (f"{after:020d}" if after else "")
            rows = self.execute(
                """SELECT key FROM runtime_kv
                   WHERE key>? AND key<? ORDER BY key LIMIT ?""",
                (lower, prefix + "\uffff", cleanup_page_size),
            ).fetchall()
            keys = [str(item["key"]) for item in rows]
            if not keys:
                return after, False
            placeholders = ",".join("?" for _ in keys)
            with self.transaction() as conn:
                conn.execute(
                    f"DELETE FROM runtime_kv WHERE key IN ({placeholders})",  # nosec B608
                    tuple(keys),
                )
            try:
                next_after = int(keys[-1].rsplit(":", 1)[-1])
            except ValueError:
                next_after = after
            return next_after, len(keys) == cleanup_page_size

        def present_prefix(position: int, document_version: int) -> str:
            return (
                f"{self._MENTION_SWEEP_KEY}present:{len(user_id):08d}:{user_id}:"
                f"{position:020d}:{document_version:020d}:"
            )

        def present_document_prefix(position: int) -> str:
            return f"{self._MENTION_SWEEP_KEY}present:{len(user_id):08d}:{user_id}:{position:020d}:"

        def winner_document_prefix(position: int) -> str:
            return f"{self._MENTION_SWEEP_KEY}winner:{len(user_id):08d}:{user_id}:{position:020d}:"

        def validation_document_prefix(position: int) -> str:
            return f"{self._MENTION_SWEEP_KEY}validation:{len(user_id):08d}:{user_id}:{position:020d}:"

        def validation_version_prefix(position: int, document_version: int) -> str:
            return validation_document_prefix(position) + f"{document_version:020d}:"

        def validation_context_authority(source: object) -> int:
            fields = token_fields(source)
            scan = scan_state(fields["scan_cursor"])
            numbers = [
                int(fields["owned_offset"]),
                int(fields["token_eof"]),
                int(scan["char"]),
                int(scan["byte"]),
                int(scan["skip"]),
                len(fields["token_positions"]),
                *[int(item) for item in fields["token_positions"]],
            ]
            payload = b"".join(item.to_bytes(8, "big") for item in numbers)
            authority = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
            return (authority & sqlite_integer_max) or 1

        def validation_key(
            position: int,
            document_version: int,
            source: object,
        ) -> str:
            return validation_version_prefix(position, document_version) + (
                f"{validation_context_authority(source):020d}"
            )

        def validation_progress_authority(
            source: object,
            validation_index: int,
            validation_byte: int,
            validation_skip: int,
        ) -> int:
            numbers = (
                validation_context_authority(source),
                validation_index,
                validation_byte,
                validation_skip,
            )
            payload = b"".join(int(item).to_bytes(8, "big") for item in numbers)
            authority = int.from_bytes(
                hashlib.blake2b(
                    payload,
                    key=_MENTION_VALIDATION_SECRET,
                    digest_size=8,
                ).digest(),
                "big",
            )
            return (authority & sqlite_integer_max) or 1

        def store_validation_progress(
            position: int,
            document_version: int,
            source: object,
            validation_index: int,
            validation_byte: int,
            validation_skip: int,
            *,
            conn: Any,
        ) -> None:
            conn.execute(
                """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                (
                    validation_key(position, document_version, source),
                    str(
                        validation_progress_authority(
                            source,
                            validation_index,
                            validation_byte,
                            validation_skip,
                        )
                    ),
                    utc_now(),
                ),
            )

        def validation_progress_is_authorized(
            position: int,
            document_version: int,
            source: object,
            validation_index: int,
            validation_byte: int,
            validation_skip: int,
        ) -> bool:
            row = self.execute(
                "SELECT value FROM runtime_kv WHERE key=?",
                (validation_key(position, document_version, source),),
            ).fetchone()
            return row is not None and numeric(row["value"]) == validation_progress_authority(
                source,
                validation_index,
                validation_byte,
                validation_skip,
            )

        def delete_validation_page(position: int, document_version: int) -> bool:
            prefix = validation_version_prefix(position, document_version)
            rows = self.execute(
                """SELECT key FROM runtime_kv
                   WHERE key>? AND key<? ORDER BY key LIMIT ?""",
                (prefix, prefix + "\uffff", cleanup_page_size),
            ).fetchall()
            keys = [str(item["key"]) for item in rows]
            if not keys:
                return False
            placeholders = ",".join("?" for _ in keys)
            with self.transaction() as conn:
                conn.execute(
                    f"DELETE FROM runtime_kv WHERE key IN ({placeholders})",  # nosec B608
                    tuple(keys),
                )
            return len(keys) == cleanup_page_size

        def winner_version_prefix(position: int, document_version: int) -> str:
            return winner_document_prefix(position) + f"{document_version:020d}:"

        def winner_key(
            position: int,
            document_version: int,
            window_start: int,
            entity_rowid: int,
        ) -> str:
            return (
                winner_version_prefix(position, document_version) + f"{window_start:020d}:{entity_rowid:020d}"
            )

        def store_winner_rows(
            position: int,
            document_version: int,
            window_start: int,
            rows: Sequence[Mapping[str, Any]],
            *,
            conn: Any,
        ) -> None:
            values = {
                int(item["position"]): entity_authority(item["id"], item["version"])
                for item in rows
                if int(item["position"]) > 0 and str(item.get("id") or "")
            }
            if not values:
                return
            now = utc_now()
            conn.executemany(
                """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [
                    (
                        winner_key(
                            position,
                            document_version,
                            window_start,
                            entity_rowid,
                        ),
                        str(authority),
                        now,
                    )
                    for entity_rowid, authority in sorted(values.items())
                ],
            )

        def winner_is_authorized(
            position: int,
            document_version: int,
            window_start: int,
            entity_rowid: int,
            entity_id: str,
            entity_version: int,
        ) -> bool:
            row = self.execute(
                "SELECT value FROM runtime_kv WHERE key=?",
                (
                    winner_key(
                        position,
                        document_version,
                        window_start,
                        entity_rowid,
                    ),
                ),
            ).fetchone()
            return row is not None and numeric(row["value"]) == entity_authority(entity_id, entity_version)

        def delete_winner_page(position: int, document_version: int) -> bool:
            prefix = winner_version_prefix(position, document_version)
            rows = self.execute(
                """SELECT key FROM runtime_kv
                   WHERE key>? AND key<? ORDER BY key LIMIT ?""",
                (prefix, prefix + "\uffff", cleanup_page_size),
            ).fetchall()
            keys = [str(item["key"]) for item in rows]
            if not keys:
                return False
            placeholders = ",".join("?" for _ in keys)
            with self.transaction() as conn:
                conn.execute(
                    f"DELETE FROM runtime_kv WHERE key IN ({placeholders})",  # nosec B608
                    tuple(keys),
                )
            return len(keys) == cleanup_page_size

        def present_key(
            position: int,
            document_version: int,
            name_length: int,
            entity_rowid: int,
        ) -> str:
            # Ascending lexical order becomes longest-name-first, then rowid.
            priority = 999 - max(0, min(int(name_length), 999))
            return present_prefix(position, document_version) + f"{priority:04d}:{entity_rowid:020d}"

        def store_present_rows(
            position: int,
            document_version: int,
            rows: Sequence[Mapping[str, Any]],
            *,
            conn: Any,
        ) -> None:
            values = [
                (
                    int(item["position"]),
                    len(str(item.get("name") or "")),
                    entity_authority(item["id"], item["version"]),
                )
                for item in rows
                if int(item["position"]) > 0 and str(item.get("id") or "")
            ]
            if not values:
                return
            now = utc_now()
            conn.executemany(
                """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [
                    (
                        present_key(position, document_version, name_length, entity_rowid),
                        str(authority),
                        now,
                    )
                    for entity_rowid, name_length, authority in values
                ],
            )

        def present_entries(
            position: int,
            document_version: int,
            *,
            after_priority: int,
            after_rowid: int,
            limit: int,
        ) -> list[tuple[int, int, int]]:
            prefix = present_prefix(position, document_version)
            lower = (
                prefix + f"{after_priority:04d}:{after_rowid:020d}"
                if after_priority or after_rowid
                else prefix
            )
            rows = self.execute(
                """SELECT key,value FROM runtime_kv
                   WHERE key>? AND key<? ORDER BY key LIMIT ?""",
                (lower, prefix + "\uffff", max(1, min(int(limit), 64))),
            ).fetchall()
            found: list[tuple[int, int, int]] = []
            for item in rows:
                try:
                    priority_text, rowid_text = str(item["key"]).rsplit(":", 2)[-2:]
                    authority = parsed_numeric(item["value"])
                    if authority:
                        found.append((int(priority_text), int(rowid_text), authority))
                except (TypeError, ValueError):
                    continue
            return found

        def present_cursor_is_valid(
            position: int,
            document_version: int,
            priority: int,
            entity_rowid: int,
        ) -> bool:
            if priority == 0 and entity_rowid == 0:
                return True
            key = present_prefix(position, document_version) + f"{priority:04d}:{entity_rowid:020d}"
            return self.execute("SELECT 1 FROM runtime_kv WHERE key=?", (key,)).fetchone() is not None

        def delete_present_page(
            position: int,
            document_version: int,
        ) -> bool:
            prefix = present_prefix(position, document_version)
            rows = self.execute(
                """SELECT key FROM runtime_kv
                   WHERE key>? AND key<? ORDER BY key LIMIT ?""",
                (prefix, prefix + "\uffff", cleanup_page_size),
            ).fetchall()
            keys = [str(item["key"]) for item in rows]
            if not keys:
                return False
            placeholders = ",".join("?" for _ in keys)
            with self.transaction() as conn:
                conn.execute(
                    f"DELETE FROM runtime_kv WHERE key IN ({placeholders})",  # nosec B608
                    tuple(keys),
                )
            return len(keys) == cleanup_page_size

        def delete_old_namespace_page(
            document_prefix: str,
            current_version: int,
        ) -> bool:
            """Boundedly remove markers owned by an older document version."""

            upper = document_prefix + f"{current_version:020d}:"
            rows = self.execute(
                """SELECT key FROM runtime_kv
                   WHERE key>? AND key<? ORDER BY key LIMIT ?""",
                (document_prefix, upper, cleanup_page_size),
            ).fetchall()
            keys = [str(item["key"]) for item in rows]
            if not keys:
                return False
            placeholders = ",".join("?" for _ in keys)
            with self.transaction() as conn:
                conn.execute(
                    f"DELETE FROM runtime_kv WHERE key IN ({placeholders})",  # nosec B608
                    tuple(keys),
                )
            return len(keys) == cleanup_page_size

        entity_visibility = (
            "e.deleted_at IS NULL AND e.canonical=1 AND e.merged_into_id IS NULL "
            f"AND {_not_private_entity_material_dependency('e')}"
        )

        def material_needs_exact_fallback(value: object) -> bool:
            """Identify literal material outside the fast phrase grammar."""

            clean = str(value or "").strip()[:8_192]
            if len(clean) < 3:
                return False
            tokens: list[tuple[int, int, str]] = []
            for match in re.finditer(r"(?u)[\w.+#/-]+", clean):
                token = match.group(0).rstrip(".,;:!?…")
                if token:
                    tokens.append((match.start(), match.start() + len(token), token))
            if (
                not tokens
                or len(tokens) > 12
                or any(len(token) < 2 for _start, _end, token in tokens)
                or tokens[0][0] != 0
                or tokens[-1][1] != len(clean)
            ):
                return True
            return any(
                bool(gap := clean[left[1] : right[0]]) and not gap.isspace()
                for left, right in zip(tokens, tokens[1:], strict=False)
            )

        def row_needs_exact_fallback(item: Mapping[str, Any]) -> bool:
            return any(
                material_needs_exact_fallback(material)
                for material in (
                    item.get("name"),
                    *_aliases_of(dict(item)),
                )
            )

        def discovery_entity_rows(after: int) -> list[dict[str, Any]]:
            # Page the global rowid B-tree first. `WHERE user_id=? ORDER BY rowid`
            # has no supporting compound index and may inspect an arbitrary number
            # of another tenant's rows before returning eight. The first query
            # reads technical rowids only; private material is projected solely
            # from this tenant's fixed-size IN page below.
            positions = [
                int(item["position"])
                for item in self.execute(
                    """SELECT rowid AS position FROM entities
                       WHERE rowid>? ORDER BY rowid LIMIT ?""",
                    (after, discovery_page),
                ).fetchall()
            ]
            if not positions:
                return []
            placeholders = ",".join("?" for _ in positions)
            rows = self.execute(
                f"""SELECT e.rowid AS position,
                           CASE WHEN {entity_visibility} THEN e.id ELSE '' END AS id,
                           CASE WHEN {entity_visibility} THEN e.version ELSE 0 END AS version,
                           CASE WHEN {entity_visibility}
                                THEN substr(e.name,1,240) ELSE '' END AS name,
                           CASE WHEN {entity_visibility}
                                  AND length(CAST(COALESCE(e.normalized_name,'') AS BLOB))<=8192
                                THEN e.normalized_name ELSE '' END AS normalized_name,
                           CASE WHEN {entity_visibility}
                                  AND length(CAST(COALESCE(e.aliases_json,'') AS BLOB))<=8192
                                  AND json_valid(e.aliases_json)
                                  AND json_type(e.aliases_json)='array'
                                THEN e.aliases_json ELSE '[]' END AS aliases_json,
                           CASE WHEN {entity_visibility} THEN 1 ELSE 0 END AS eligible
                      FROM entities e
                     WHERE e.user_id=? AND e.rowid IN ({placeholders})
                     ORDER BY e.rowid""",  # nosec B608
                (user_id, *positions),
            ).fetchall()
            by_position = {int(item["position"]): dict(item) for item in rows}
            return [
                by_position.get(
                    position,
                    {
                        "position": position,
                        "id": "",
                        "version": 0,
                        "name": "",
                        "normalized_name": "",
                        "aliases_json": "[]",
                        "eligible": 0,
                    },
                )
                for position in positions
            ]

        def load_candidate_rows(rowids: Sequence[int]) -> list[dict[str, Any]]:
            positions = sorted({int(item) for item in rowids if int(item) > 0})
            if not positions:
                return []
            found: dict[int, dict[str, Any]] = {}
            for start in range(0, len(positions), 400):
                batch = positions[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = self.execute(
                    f"""SELECT e.rowid AS position, e.id, e.version,
                               substr(e.name,1,240) AS name,
                               CASE WHEN length(CAST(COALESCE(e.aliases_json,'') AS BLOB))<=8192
                                          AND json_valid(e.aliases_json)
                                          AND json_type(e.aliases_json)='array'
                                    THEN e.aliases_json ELSE '[]' END AS aliases_json
                          FROM entities e
                         WHERE e.user_id=? AND e.rowid IN ({placeholders})
                           AND {entity_visibility}
                         ORDER BY e.rowid""",  # nosec B608
                    (user_id, *batch),
                ).fetchall()
                for item in rows:
                    found[int(item["position"])] = dict(item)
            return [found[item] for item in positions if item in found]

        def document_is_current(
            conn: Any,
            position: int,
            document_version: int,
        ) -> bool:
            row = conn.execute(
                f"""SELECT 1 FROM knowledge_objects k
                     WHERE k.rowid=? AND k.user_id=? AND k.version=?
                       AND k.deleted_at IS NULL
                       AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (position, user_id, document_version),
            ).fetchone()
            return row is not None

        def entities_are_current(
            conn: Any,
            expected_rows: Sequence[Mapping[str, Any]],
        ) -> bool:
            expected = {
                int(item["position"]): (str(item["id"]), max(1, int(item["version"])))
                for item in expected_rows
                if int(item.get("position") or 0) > 0 and str(item.get("id") or "")
            }
            if not expected:
                return True
            positions = sorted(expected)
            actual: dict[int, tuple[str, int]] = {}
            for start in range(0, len(positions), 100):
                batch = positions[start : start + 100]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""SELECT e.rowid AS position,e.id,e.version FROM entities e
                         WHERE e.user_id=? AND e.rowid IN ({placeholders})
                           AND {entity_visibility}""",  # nosec B608
                    (user_id, *batch),
                ).fetchall()
                actual.update(
                    {int(item["position"]): (str(item["id"]), int(item["version"])) for item in rows}
                )
            return actual == expected

        def commit_pending(
            position: int,
            document_version: int,
            work: Mapping[str, Any],
            *,
            expected_entities: Sequence[Mapping[str, Any]] = (),
            candidate_rows_to_store: Sequence[Mapping[str, Any]] = (),
            present_rows_to_store: Sequence[Mapping[str, Any]] = (),
            winner_rows_to_store: Sequence[Mapping[str, Any]] = (),
            winner_window_start: int = 0,
            validation_progress: tuple[object, int, int, int] | None = None,
        ) -> bool:
            """Publish material-derived markers and their owner checkpoint atomically."""

            with self.transaction() as conn:
                if not document_is_current(conn, position, document_version):
                    return False
                if not entities_are_current(conn, expected_entities):
                    return False
                store_candidate_rows(
                    position,
                    document_version,
                    candidate_rows_to_store,
                    conn=conn,
                )
                store_present_rows(
                    position,
                    document_version,
                    present_rows_to_store,
                    conn=conn,
                )
                store_winner_rows(
                    position,
                    document_version,
                    winner_window_start,
                    winner_rows_to_store,
                    conn=conn,
                )
                if validation_progress is not None:
                    validation_source, validation_index, validation_byte, validation_skip = (
                        validation_progress
                    )
                    store_validation_progress(
                        position,
                        document_version,
                        validation_source,
                        validation_index,
                        validation_byte,
                        validation_skip,
                        conn=conn,
                    )
                write_pending(conn, position, document_version, work)
            return True

        def link_candidate(
            *,
            document_rowid: int,
            expected_version: int,
            entity_rowid: int,
            expected_entity_id: str,
            expected_entity_version: int,
            method: str,
        ) -> str:
            """Atomically recheck version/status and create at most one link."""

            nonlocal linked
            if linked >= link_limit:
                return "max_links"
            if monotonic() >= deadline:
                return "max_seconds"
            with self.transaction() as conn:
                document = conn.execute(
                    f"""SELECT k.id, k.version FROM knowledge_objects k
                         WHERE k.rowid=? AND k.user_id=? AND k.deleted_at IS NULL
                           AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                    (document_rowid, user_id),
                ).fetchone()
                if document is None or int(document["version"] or 0) != expected_version:
                    return "stale"
                entity = conn.execute(
                    f"""SELECT e.id,e.version FROM entities e
                         WHERE e.rowid=? AND e.user_id=? AND e.id=? AND e.version=?
                           AND {entity_visibility}""",  # nosec B608
                    (
                        entity_rowid,
                        user_id,
                        expected_entity_id,
                        expected_entity_version,
                    ),
                ).fetchone()
                if entity is None:
                    return "entity_stale"
                entity_id = str(entity["id"])
                existing = conn.execute(
                    """SELECT 1 FROM knowledge_entity_links
                       WHERE user_id=? AND knowledge_object_id=? AND entity_id=?""",
                    (user_id, str(document["id"]), entity_id),
                ).fetchone()
                if existing is not None:
                    return "known"
                self.link_knowledge_entity(
                    user_id,
                    str(document["id"]),
                    entity_id,
                    status="accepted",
                    confidence=0.97,
                    evidence={"method": method, "source": "backfill"},
                )
                linked += 1
            return "linked"

        def finish_document(position: int, expected_version: int) -> bool:
            """Advance the outer cursor in the same transaction as version check."""

            nonlocal cursor, scanned
            with self.transaction() as conn:
                current = conn.execute(
                    f"""SELECT 1 FROM knowledge_objects k
                         WHERE k.rowid=? AND k.user_id=? AND k.version=?
                           AND k.deleted_at IS NULL
                           AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                    (position, user_id, expected_version),
                ).fetchone()
                if current is None:
                    return False
                cursor = position
                conn.execute(
                    """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
                       ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value, updated_at=excluded.updated_at""",
                    (
                        self._MENTION_SWEEP_KEY + user_id,
                        json.dumps(
                            {
                                "entity_count_complete": int(entity_count_complete),
                                "entity_count_cursor": entity_count_cursor,
                                "entity_count_total": entity_count_total,
                                "rowid": cursor,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        utc_now(),
                    ),
                )
                scanned += 1
            return True

        def next_document_row(after: int) -> tuple[Any | None, int, bool]:
            """Inspect one fixed global rowid page without reading other tenants' text."""

            positions = [
                int(item["position"])
                for item in self.execute(
                    """SELECT rowid AS position FROM knowledge_objects
                       WHERE rowid>? ORDER BY rowid LIMIT ?""",
                    (after, document_scan_page),
                ).fetchall()
            ]
            if not positions:
                return None, after, True
            placeholders = ",".join("?" for _ in positions)
            row = self.execute(
                f"""SELECT k.rowid AS position, k.version
                      FROM knowledge_objects k
                     WHERE k.user_id=? AND k.rowid IN ({placeholders})
                       AND k.deleted_at IS NULL
                       AND {_not_private_knowledge_dependency("k")}
                     ORDER BY k.rowid LIMIT 1""",  # nosec B608
                (user_id, *positions),
            ).fetchone()
            return row, positions[-1], len(positions) < document_scan_page

        def read_content_page(
            position: int,
            document_version: int,
            byte_start: int,
            char_length: int,
        ) -> tuple[str, list[int], bool, bool] | None:
            row = self.execute(
                f"""SELECT 1 FROM knowledge_objects k
                     WHERE k.rowid=? AND k.user_id=? AND k.version=?
                       AND k.deleted_at IS NULL
                       AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (
                    position,
                    user_id,
                    document_version,
                ),
            ).fetchone()
            if row is None:
                return None
            requested = max(1, min(int(char_length), phrase_read_chars))
            start = max(0, int(byte_start))
            with self.conn.blobopen(
                "knowledge_objects",
                "content",
                position,
                readonly=True,
            ) as blob:
                total_bytes = len(blob)
                if start > total_bytes:
                    return "", [0], False, False
                blob.seek(start)
                raw = blob.read(min(total_bytes - start, requested * 4 + 4))

            decoded = ""
            consumed_raw = len(raw)
            for trim in range(0, min(4, len(raw)) + 1):
                try:
                    decoded = raw[: len(raw) - trim if trim else None].decode("utf-8")
                    consumed_raw = len(raw) - trim
                    break
                except UnicodeDecodeError as exc:
                    if exc.reason != "unexpected end of data":
                        return "", [0], False, False
            else:  # pragma: no cover - UTF-8 code points are at most four bytes
                return "", [0], False, False

            text_page = decoded[:requested]
            offsets = [0]
            byte_offset = 0
            for character in text_page:
                byte_offset += len(character.encode("utf-8"))
                offsets.append(byte_offset)
            # `consumed_raw` can include decoded lookahead beyond `requested`;
            # only the returned character prefix belongs to this page.
            del consumed_raw
            return text_page, offsets, start + offsets[-1] < total_bytes, True

        def read_token_context(
            position: int,
            document_version: int,
            flat_positions: Sequence[int],
        ) -> tuple[str, list[tuple[int, int]], bool] | None:
            pairs = [
                (int(flat_positions[index]), int(flat_positions[index + 1]))
                for index in range(0, len(flat_positions), 2)
            ]
            if not pairs:
                return "", [], True
            row = self.execute(
                f"""SELECT 1 FROM knowledge_objects k
                     WHERE k.rowid=? AND k.user_id=? AND k.version=?
                       AND k.deleted_at IS NULL
                       AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (position, user_id, document_version),
            ).fetchone()
            if row is None:
                return None
            pieces: list[str | None] = []
            with self.conn.blobopen(
                "knowledge_objects",
                "content",
                position,
                readonly=True,
            ) as blob:
                total_bytes = len(blob)
                for start, end in pairs:
                    if not 0 <= start <= end <= total_bytes or end - start > 960:
                        return "", [], False
                    if start == end:
                        pieces.append(None)
                        continue
                    blob.seek(start)
                    try:
                        pieces.append(blob.read(end - start).decode("utf-8"))
                    except UnicodeDecodeError:
                        return "", [], False
            synthetic_parts: list[str] = []
            synthetic_positions: list[tuple[int, int]] = []
            offset = 0
            for raw_piece in pieces:
                piece = raw_piece if raw_piece is not None else "0"
                if synthetic_parts:
                    synthetic_parts.append(" ")
                    offset += 1
                start = offset
                synthetic_parts.append(piece)
                offset += len(piece)
                synthetic_positions.append((start, offset))
            return "".join(synthetic_parts), synthetic_positions, True

        def previous_content_character_byte(
            position: int,
            document_version: int,
            byte_start: int,
        ) -> tuple[int, bool] | None:
            """Return the previous UTF-8 boundary without projecting the body."""

            row = self.execute(
                f"""SELECT 1 FROM knowledge_objects k
                     WHERE k.rowid=? AND k.user_id=? AND k.version=?
                       AND k.deleted_at IS NULL
                       AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (position, user_id, document_version),
            ).fetchone()
            if row is None:
                return None
            current = max(0, int(byte_start))
            if current == 0:
                return 0, True
            with self.conn.blobopen(
                "knowledge_objects",
                "content",
                position,
                readonly=True,
            ) as blob:
                if current > len(blob):
                    return 0, False
                probe = max(0, current - 4)
                blob.seek(probe)
                raw = blob.read(current - probe)
            for skip in range(min(4, len(raw))):
                try:
                    decoded = raw[skip:].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if decoded:
                    return current - len(decoded[-1].encode("utf-8")), True
            return 0, False

        count_page_size = 2_048
        while not entity_count_complete:
            count_positions = [
                int(item["position"])
                for item in self.execute(
                    """SELECT rowid AS position FROM entities
                       WHERE rowid>? ORDER BY rowid LIMIT ?""",
                    (entity_count_cursor, count_page_size),
                ).fetchall()
            ]
            if not count_positions:
                entity_count_complete = True
            else:
                for start in range(0, len(count_positions), 400):
                    batch = count_positions[start : start + 400]
                    placeholders = ",".join("?" for _ in batch)
                    rows = self.execute(
                        f"""SELECT e.rowid FROM entities e
                             WHERE e.user_id=? AND e.rowid IN ({placeholders})
                               AND {entity_visibility}""",  # nosec B608
                        (user_id, *batch),
                    ).fetchall()
                    entity_count_total += len(rows)
                entity_count_cursor = count_positions[-1]
                entity_count_complete = len(count_positions) < count_page_size
            if pending:
                count_position = numeric(pending.get("document_rowid"))
                count_version = numeric(pending.get("document_version"))
                if count_position and count_version:
                    save_pending(count_position, count_version, clean_work(pending.get("work")))
                else:
                    save_cursor()
            else:
                save_cursor()
            if not entity_count_complete and monotonic() >= deadline:
                return result(
                    linked=0,
                    scanned=0,
                    complete=False,
                    entities=entity_count_total,
                    budget_reason="max_seconds",
                )

        entity_total = entity_count_total
        if entity_total == 0:
            stale_position = numeric(pending.get("document_rowid"))
            stale_version = numeric(pending.get("document_version"))
            if stale_position and stale_version:
                _next, candidates_remain = delete_candidate_page(
                    stale_position,
                    stale_version,
                    after=0,
                )
                present_remains = delete_present_page(stale_position, stale_version)
                winner_remains = delete_winner_page(stale_position, stale_version)
                validation_remains = delete_validation_page(stale_position, stale_version)
                if candidates_remain or present_remains or winner_remains or validation_remains:
                    cleanup_work = {"phase": "cleanup", "cleanup_cursor": 0}
                    save_pending(stale_position, stale_version, cleanup_work)
                    return result(linked=0, scanned=0, complete=False, entities=0)
            cursor = 0
            entity_count_cursor = 0
            entity_count_total = 0
            entity_count_complete = False
            save_cursor()
            return result(linked=0, scanned=0, complete=True, entities=0)

        while scanned < document_limit:
            if linked >= link_limit:
                return result(
                    linked=linked,
                    scanned=scanned,
                    complete=False,
                    entities=entity_total,
                    budget_reason="max_links",
                )
            if monotonic() >= deadline:
                return result(
                    linked=linked,
                    scanned=scanned,
                    complete=False,
                    entities=entity_total,
                    budget_reason="max_seconds",
                )

            pending_position = numeric(pending.get("document_rowid"))
            pending_version = numeric(pending.get("document_version"))
            row = None
            if pending_position > cursor:
                row = self.execute(
                    f"""SELECT k.rowid AS position, k.version
                          FROM knowledge_objects k
                         WHERE k.user_id=? AND k.rowid=? AND k.deleted_at IS NULL
                           AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                    (user_id, pending_position),
                ).fetchone()
                if row is None or int(row["version"] or 0) != pending_version:
                    _next_cleanup, candidates_remain = delete_candidate_page(
                        pending_position,
                        pending_version,
                        after=0,
                    )
                    present_remains = delete_present_page(
                        pending_position,
                        pending_version,
                    )
                    winner_remains = delete_winner_page(
                        pending_position,
                        pending_version,
                    )
                    validation_remains = delete_validation_page(
                        pending_position,
                        pending_version,
                    )
                    remains = candidates_remain or present_remains or winner_remains or validation_remains
                    if remains:
                        cleanup_work = {"phase": "cleanup", "cleanup_cursor": 0}
                        save_pending(pending_position, pending_version, cleanup_work)
                        pending = {
                            "document_rowid": pending_position,
                            "document_version": pending_version,
                            "work": cleanup_work,
                        }
                        if monotonic() >= deadline:
                            return result(
                                linked=linked,
                                scanned=scanned,
                                complete=False,
                                entities=entity_total,
                                budget_reason="max_seconds",
                            )
                        continue
                    pending = {}
                    save_cursor()
                    continue
            if row is None:
                row, scanned_to, reached_end = next_document_row(cursor)
                if row is None:
                    cursor = scanned_to
                    save_cursor()
                    if reached_end:
                        cursor = 0
                        entity_count_cursor = 0
                        entity_count_total = 0
                        entity_count_complete = False
                        save_cursor()
                        return result(
                            linked=linked,
                            scanned=scanned,
                            complete=True,
                            entities=entity_total,
                        )
                    continue

            position = int(row["position"])
            document_version = max(1, int(row["version"] or 1))

            old_candidates_remain = delete_old_namespace_page(
                candidate_document_prefix(position),
                document_version,
            )
            old_present_remain = delete_old_namespace_page(
                present_document_prefix(position),
                document_version,
            )
            old_winners_remain = delete_old_namespace_page(
                winner_document_prefix(position),
                document_version,
            )
            old_validation_remain = delete_old_namespace_page(
                validation_document_prefix(position),
                document_version,
            )
            if old_candidates_remain or old_present_remain or old_winners_remain or old_validation_remain:
                work = new_work()
                if same_document := (pending_position == position and pending_version == document_version):
                    work = clean_work(pending.get("work"))
                save_pending(position, document_version, work)
                pending = {
                    "document_rowid": position,
                    "document_version": document_version,
                    "work": work,
                }
                if monotonic() >= deadline:
                    return result(
                        linked=linked,
                        scanned=scanned,
                        complete=False,
                        entities=entity_total,
                        budget_reason="max_seconds",
                    )
                continue

            work = new_work()
            same_document = pending_position == position and pending_version == document_version
            if same_document:
                work = clean_work(pending.get("work"))

            restart_document = False
            while True:
                if linked >= link_limit:
                    save_pending(position, document_version, work)
                    return result(
                        linked=linked,
                        scanned=scanned,
                        complete=False,
                        entities=entity_total,
                        budget_reason="max_links",
                    )
                if monotonic() >= deadline:
                    save_pending(position, document_version, work)
                    return result(
                        linked=linked,
                        scanned=scanned,
                        complete=False,
                        entities=entity_total,
                        budget_reason="max_seconds",
                    )

                phase = str(work["phase"])
                if phase in {
                    "inflected_collect",
                    "inflected_resolve",
                    "inflected_link",
                    "present_cleanup",
                    "present_restart_cleanup",
                }:
                    token_state = token_fields(work)
                    flat = list(token_state["token_positions"])
                    owned_offset = int(token_state["owned_offset"])
                    context_key = token_context_key(position, document_version, token_state)
                    if validated_context_key != context_key:
                        if not flat or owned_offset >= len(flat) // 2:
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        else:
                            previous = previous_content_character_byte(
                                position,
                                document_version,
                                int(flat[0]),
                            )
                            if previous is None:
                                restart_document = True
                                break
                            validation_byte, previous_valid = previous
                            if not previous_valid:
                                work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                            else:
                                work = token_work(
                                    "inflected_validate",
                                    token_state,
                                    validation_index=0,
                                    validation_byte=validation_byte,
                                    validation_skip=0,
                                )
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                if phase == "discover":
                    discovery_work = clean_work(work)
                    source = phrase_state(work.get("phrase_cursor"))
                    source_base = source["char"]
                    source_byte = source["byte"]
                    content_page = read_content_page(
                        position,
                        document_version,
                        source_byte,
                        phrase_read_chars,
                    )
                    if content_page is None:
                        restart_document = True
                        break
                    content_chunk, byte_offsets, content_remains, content_valid = content_page
                    if not content_valid:
                        work = new_work()
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    if not content_chunk:
                        work = {
                            "phase": "discover_fallback",
                            "entity_scan_rowid": 0,
                        }
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    phrases, next_phrase_cursor, phrases_remain, valid = mention_phrase_candidate_page(
                        content_chunk,
                        cursor={
                            "char": 0,
                            "length": source["length"],
                            "skip": source["skip"],
                        },
                        limit=64,
                    )
                    if not valid:
                        work = new_work()
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    next_phrase_cursor = {
                        "char": source_base + int(next_phrase_cursor["char"]),
                        "byte": source_byte + byte_offsets[int(next_phrase_cursor["char"])],
                        "length": int(next_phrase_cursor["length"]),
                        "skip": int(next_phrase_cursor["skip"]),
                    }
                    phrases_remain = bool(phrases_remain or content_remains)
                    if not phrases:
                        if phrases_remain:
                            work = new_work(next_phrase_cursor)
                        else:
                            work = {
                                "phase": "discover_fallback",
                                "entity_scan_rowid": 0,
                            }
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue

                    wanted = list(
                        dict.fromkeys(
                            normalized
                            for phrase in phrases
                            if (normalized := normalize_entity_name(str(phrase or "")))
                        )
                    )
                    entity_scan = numeric(work.get("entity_scan_rowid"))
                    if entity_scan:
                        scan_position = self.execute(
                            "SELECT 1 FROM entities WHERE rowid=?",
                            (entity_scan,),
                        ).fetchone()
                        if scan_position is None:
                            entity_scan = 0
                    entity_rows = discovery_entity_rows(entity_scan)
                    matched_rows: list[dict[str, Any]] = []
                    wanted_set = set(wanted)
                    for item in entity_rows:
                        entity_scan = int(item["position"])
                        if not int(item["eligible"] or 0):
                            continue
                        if str(item.get("normalized_name") or "") in wanted_set:
                            matched_rows.append(item)
                            continue
                        aliases = _aliases_of({"aliases_json": item["aliases_json"]})
                        if any(normalize_entity_name(alias) in wanted_set for alias in aliases):
                            matched_rows.append(item)
                    if len(entity_rows) < discovery_page:
                        if phrases_remain:
                            work = new_work(next_phrase_cursor)
                        else:
                            work = {
                                "phase": "discover_fallback",
                                "entity_scan_rowid": 0,
                            }
                    else:
                        work["entity_scan_rowid"] = entity_scan
                    eligible_rows = [item for item in entity_rows if int(item["eligible"] or 0)]
                    if not commit_pending(
                        position,
                        document_version,
                        work,
                        expected_entities=eligible_rows,
                        candidate_rows_to_store=matched_rows,
                    ):
                        work = discovery_work
                        continue
                    continue

                if phase == "discover_fallback":
                    fallback_work = clean_work(work)
                    entity_after = numeric(work.get("entity_scan_rowid"))
                    entity_rows = discovery_entity_rows(entity_after)
                    eligible_rows = [item for item in entity_rows if int(item["eligible"] or 0)]
                    fallback_rows = [item for item in eligible_rows if row_needs_exact_fallback(item)]
                    if len(entity_rows) < discovery_page:
                        work = {
                            "phase": "exact",
                            "candidate_rowid": 0,
                            "exact_cursor": exact_state(None),
                        }
                    else:
                        work["entity_scan_rowid"] = int(entity_rows[-1]["position"])
                    if not commit_pending(
                        position,
                        document_version,
                        work,
                        expected_entities=eligible_rows,
                        candidate_rows_to_store=fallback_rows,
                    ):
                        work = fallback_work
                    continue

                if phase == "exact":
                    candidate_after = numeric(work.get("candidate_rowid"))
                    literal_cursor = exact_state(work.get("exact_cursor"))
                    if not candidate_cursor_is_valid(
                        position,
                        document_version,
                        candidate_after,
                    ):
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    if literal_cursor["char"] % content_page_chars or literal_cursor["entity"]:
                        work["exact_cursor"] = exact_state(None)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    candidate_page_entries = candidate_entries(
                        position,
                        document_version,
                        after=candidate_after,
                        limit=1,
                    )
                    if not candidate_page_entries:
                        work = token_work("inflected_tokenize", {})
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    candidate_position, marker_authority = candidate_page_entries[0]
                    candidates = load_candidate_rows([candidate_position])
                    if (
                        not candidates
                        or entity_authority(candidates[0]["id"], candidates[0]["version"]) != marker_authority
                    ):
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    candidate = candidates[0]
                    left_characters = 0
                    read_byte = literal_cursor["byte"]
                    if literal_cursor["char"]:
                        previous = previous_content_character_byte(
                            position,
                            document_version,
                            read_byte,
                        )
                        if previous is None:
                            restart_document = True
                            break
                        read_byte, previous_valid = previous
                        if not previous_valid:
                            work["exact_cursor"] = exact_state(None)
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        left_characters = 1
                    content_page = read_content_page(
                        position,
                        document_version,
                        read_byte,
                        content_page_chars + exact_halo_chars + left_characters,
                    )
                    if content_page is None:
                        restart_document = True
                        break
                    content_chunk, byte_offsets, content_remains, content_valid = content_page
                    if not content_valid:
                        work["exact_cursor"] = exact_state(None)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    if not content_chunk:
                        work.update(
                            candidate_rowid=candidate_position,
                            exact_cursor=exact_state(None),
                        )
                        if not commit_pending(
                            position,
                            document_version,
                            work,
                            expected_entities=[candidate],
                        ):
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                        continue
                    exact, next_exact, exact_remains, exact_valid = exact_mentions_page(
                        content_chunk,
                        [
                            (
                                str(candidate.get("name") or ""),
                                str(candidate["id"]),
                                _aliases_of(candidate),
                            )
                        ],
                        cursor={
                            "char": 0,
                            "entity": 0,
                            "material": literal_cursor["material"],
                        },
                        char_limit=content_page_chars + left_characters,
                    )
                    if not exact_valid:
                        work["exact_cursor"] = exact_state(None)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    if str(candidate["id"]) in exact:
                        outcome = link_candidate(
                            document_rowid=position,
                            expected_version=document_version,
                            entity_rowid=candidate_position,
                            expected_entity_id=str(candidate["id"]),
                            expected_entity_version=int(candidate["version"]),
                            method="existing_entity_exact_mention",
                        )
                        if outcome == "stale":
                            save_pending(position, document_version, work)
                            restart_document = True
                            break
                        if outcome in {"max_links", "max_seconds"}:
                            save_pending(position, document_version, work)
                            return result(
                                linked=linked,
                                scanned=scanned,
                                complete=False,
                                entities=entity_total,
                                budget_reason=outcome,
                            )
                        if outcome == "entity_stale":
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        else:
                            work.update(
                                candidate_rowid=candidate_position,
                                exact_cursor=exact_state(None),
                            )
                    else:
                        if int(next_exact["char"]) == 0:
                            work["exact_cursor"] = {
                                "char": literal_cursor["char"],
                                "byte": literal_cursor["byte"],
                                "entity": 0,
                                "material": int(next_exact["material"]),
                            }
                        elif len(content_chunk) > content_page_chars + left_characters:
                            work["exact_cursor"] = {
                                "char": literal_cursor["char"] + content_page_chars,
                                "byte": read_byte + byte_offsets[content_page_chars + left_characters],
                                "entity": 0,
                                "material": 0,
                            }
                        else:
                            work.update(
                                candidate_rowid=candidate_position,
                                exact_cursor=exact_state(None),
                            )
                    if not commit_pending(
                        position,
                        document_version,
                        work,
                        expected_entities=[] if str(candidate["id"]) in exact else [candidate],
                    ):
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                    continue

                if phase == "inflected_tokenize":
                    token_state = token_fields(work)
                    flat = list(token_state["token_positions"])
                    owned_offset = int(token_state["owned_offset"])
                    token_count = len(flat) // 2
                    target = owned_offset + inflected_owned_tokens + inflected_context_tokens
                    if token_count >= target or int(token_state["token_eof"]):
                        if owned_offset >= token_count:
                            work = {"phase": "cleanup", "cleanup_cursor": 0}
                        else:
                            work = token_work(
                                "inflected_collect",
                                token_state,
                                entity_scan_rowid=0,
                            )
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue

                    scan = scan_state(token_state["scan_cursor"])
                    content_page = read_content_page(
                        position,
                        document_version,
                        scan["byte"],
                        token_read_chars,
                    )
                    if content_page is None:
                        restart_document = True
                        break
                    content_chunk, byte_offsets, content_remains, content_valid = content_page
                    if not content_valid:
                        work = token_work("inflected_tokenize", {})
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    if not content_chunk:
                        token_state["token_eof"] = 1
                        work = token_work("inflected_tokenize", token_state)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    needed = max(1, target - token_count)
                    page_positions, next_scan, remains, valid = inflected_token_position_page(
                        content_chunk,
                        cursor={"char": 0, "skip": scan["skip"]},
                        limit=needed,
                        char_limit=content_page_chars,
                    )
                    if not valid or int(next_scan["char"]) <= 0:
                        work = token_work("inflected_tokenize", {})
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    for token_start, token_end in page_positions:
                        flat.extend(
                            (
                                scan["byte"] + byte_offsets[token_start],
                                scan["byte"] + byte_offsets[token_end],
                            )
                        )
                    token_state.update(
                        scan_cursor={
                            "char": scan["char"] + int(next_scan["char"]),
                            "byte": scan["byte"] + byte_offsets[int(next_scan["char"])],
                            "skip": int(next_scan["skip"]),
                        },
                        token_positions=flat,
                        token_eof=int(not remains and not content_remains),
                    )
                    work = token_work("inflected_tokenize", token_state)
                    if not commit_pending(position, document_version, work):
                        restart_document = True
                        break
                    continue

                if phase == "inflected_validate":
                    token_state = token_fields(work)
                    flat = list(token_state["token_positions"])
                    pairs = len(flat) // 2
                    validation_index = numeric(work.get("validation_index"))
                    validation_byte = numeric(work.get("validation_byte"))
                    validation_skip = min(1, numeric(work.get("validation_skip")))
                    scan = scan_state(token_state["scan_cursor"])
                    target_byte = int(scan["byte"])
                    validation_anchor = previous_content_character_byte(
                        position,
                        document_version,
                        int(flat[0]) if flat else 0,
                    )
                    if validation_anchor is None:
                        restart_document = True
                        break
                    anchor_byte, anchor_valid = validation_anchor
                    progress_is_anchor = (
                        validation_index == 0 and validation_byte == anchor_byte and validation_skip == 0
                    )
                    progress_is_authorized = progress_is_anchor or (
                        validation_progress_is_authorized(
                            position,
                            document_version,
                            token_state,
                            validation_index,
                            validation_byte,
                            validation_skip,
                        )
                    )
                    if not anchor_valid or not progress_is_authorized:
                        if not anchor_valid:
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        else:
                            work = token_work(
                                "inflected_validate",
                                token_state,
                                validation_index=0,
                                validation_byte=anchor_byte,
                                validation_skip=0,
                            )
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    invalid_validation = (
                        not flat
                        or int(token_state["owned_offset"]) >= pairs
                        or validation_index > pairs
                        or validation_byte > target_byte
                    )
                    validation_complete = (
                        not invalid_validation
                        and validation_index == pairs
                        and validation_byte == target_byte
                        and validation_skip == int(scan["skip"])
                    )
                    if validation_complete and int(token_state["token_eof"]):
                        eof_probe = read_content_page(
                            position,
                            document_version,
                            target_byte,
                            1,
                        )
                        if eof_probe is None:
                            restart_document = True
                            break
                        eof_text, _eof_offsets, eof_remains, eof_valid = eof_probe
                        validation_complete = bool(eof_valid and not eof_text and not eof_remains)
                        invalid_validation = not validation_complete
                    if validation_complete:
                        validated_context_key = token_context_key(
                            position,
                            document_version,
                            token_state,
                        )
                        work = token_work("present_restart_cleanup", token_state)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    if invalid_validation or validation_byte == target_byte:
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue

                    content_page = read_content_page(
                        position,
                        document_version,
                        validation_byte,
                        token_read_chars,
                    )
                    if content_page is None:
                        restart_document = True
                        break
                    content_chunk, byte_offsets, _content_remains, content_valid = content_page
                    if not content_valid or not content_chunk:
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    page_positions, next_validation, _remains, valid = inflected_token_position_page(
                        content_chunk,
                        cursor={"char": 0, "skip": validation_skip},
                        limit=max(1, min(64, pairs - validation_index)),
                        char_limit=content_page_chars,
                    )
                    next_character = numeric(next_validation.get("char"))
                    if not valid or not 0 < next_character < len(byte_offsets):
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    next_byte = validation_byte + byte_offsets[next_character]
                    for token_start, token_end in page_positions:
                        if validation_index >= pairs:
                            invalid_validation = True
                            break
                        expected_start = int(flat[validation_index * 2])
                        expected_end = int(flat[validation_index * 2 + 1])
                        actual_start = validation_byte + byte_offsets[token_start]
                        actual_end = validation_byte + byte_offsets[token_end]
                        if (actual_start, actual_end) != (expected_start, expected_end):
                            invalid_validation = True
                            break
                        validation_index += 1
                    validation_skip = min(1, numeric(next_validation.get("skip")))
                    if next_byte > target_byte:
                        invalid_validation = True
                    if invalid_validation:
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        next_progress = None
                    else:
                        work = token_work(
                            "inflected_validate",
                            token_state,
                            validation_index=validation_index,
                            validation_byte=next_byte,
                            validation_skip=validation_skip,
                        )
                        next_progress = (
                            token_state,
                            validation_index,
                            next_byte,
                            validation_skip,
                        )
                    if not commit_pending(
                        position,
                        document_version,
                        work,
                        validation_progress=next_progress,
                    ):
                        restart_document = True
                        break
                    continue

                if phase == "inflected_collect":
                    token_state = token_fields(work)
                    context = read_token_context(
                        position,
                        document_version,
                        token_state["token_positions"],
                    )
                    if context is None:
                        restart_document = True
                        break
                    synthetic, synthetic_positions, context_valid = context
                    if not context_valid:
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    entity_after = numeric(work.get("entity_scan_rowid"))
                    entity_rows = discovery_entity_rows(entity_after)
                    if entity_rows:
                        candidates = [item for item in entity_rows if int(item["eligible"] or 0)]
                        present, valid = inflected_mentions_present_tokens(
                            synthetic,
                            [(str(item.get("name") or ""), str(item["id"])) for item in candidates],
                            synthetic_positions,
                        )
                        if not valid:
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        work = token_work(
                            "inflected_collect",
                            token_state,
                            entity_scan_rowid=int(entity_rows[-1]["position"]),
                        )
                        if not commit_pending(
                            position,
                            document_version,
                            work,
                            expected_entities=candidates,
                            present_rows_to_store=[item for item in candidates if str(item["id"]) in present],
                        ):
                            work = token_work("present_restart_cleanup", token_state)
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        continue

                    work = token_work(
                        "inflected_resolve",
                        token_state,
                        priority_cursor=0,
                        candidate_rowid=0,
                        winner_rowids=[],
                        winner_versions=[],
                        winner_authorities=[],
                    )
                    if not commit_pending(position, document_version, work):
                        restart_document = True
                        break
                    continue

                if phase == "inflected_resolve":
                    token_state = token_fields(work)
                    priority_cursor = numeric(work.get("priority_cursor"))
                    candidate_after = numeric(work.get("candidate_rowid"))
                    if not present_cursor_is_valid(
                        position,
                        document_version,
                        priority_cursor,
                        candidate_after,
                    ):
                        work = token_work("present_restart_cleanup", token_state)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    present_page_entries = present_entries(
                        position,
                        document_version,
                        after_priority=priority_cursor,
                        after_rowid=candidate_after,
                        limit=1,
                    )
                    if present_page_entries:
                        next_priority, candidate_position, marker_authority = present_page_entries[0]
                        winners, winner_versions, winner_authorities = aligned_winner_lists(
                            work.get("winner_rowids"),
                            work.get("winner_versions"),
                            work.get("winner_authorities"),
                        )
                        combined = load_candidate_rows([*winners, candidate_position])
                        current = {int(item["position"]): item for item in combined}
                        expected_versions = dict(zip(winners, winner_versions, strict=True))
                        expected_authorities = dict(zip(winners, winner_authorities, strict=True))
                        expected_authorities[candidate_position] = marker_authority
                        if any(
                            rowid not in current
                            or (
                                rowid in expected_versions
                                and int(current[rowid]["version"]) != expected_versions[rowid]
                            )
                            or entity_authority(current[rowid]["id"], current[rowid]["version"])
                            != expected_authority
                            for rowid, expected_authority in expected_authorities.items()
                        ):
                            work = token_work("present_restart_cleanup", token_state)
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        context = read_token_context(
                            position,
                            document_version,
                            token_state["token_positions"],
                        )
                        if context is None:
                            restart_document = True
                            break
                        synthetic, synthetic_positions, context_valid = context
                        if not context_valid:
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        _matches, active, valid = inflected_mentions_tokens(
                            synthetic,
                            [(str(item.get("name") or ""), str(item["id"])) for item in combined],
                            synthetic_positions,
                            owned_start=int(token_state["owned_offset"]),
                            owned_count=min(
                                inflected_owned_tokens,
                                len(synthetic_positions) - int(token_state["owned_offset"]),
                            ),
                        )
                        if not valid:
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        winners = [int(item["position"]) for item in combined if str(item["id"]) in active]
                        if len(winners) > maximum_winner_rowids:
                            raise RuntimeError("inflected winner bound violated")
                        by_position = {int(item["position"]): item for item in combined}
                        work = token_work(
                            "inflected_resolve",
                            token_state,
                            priority_cursor=next_priority,
                            candidate_rowid=candidate_position,
                            winner_rowids=winners,
                            winner_versions=[int(by_position[rowid]["version"]) for rowid in winners],
                            winner_authorities=[
                                entity_authority(
                                    by_position[rowid]["id"],
                                    by_position[rowid]["version"],
                                )
                                for rowid in winners
                            ],
                        )
                        if not commit_pending(
                            position,
                            document_version,
                            work,
                            expected_entities=combined,
                        ):
                            work = token_work("present_restart_cleanup", token_state)
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                        continue

                    winners, winner_versions, winner_authorities = aligned_winner_lists(
                        work.get("winner_rowids"),
                        work.get("winner_versions"),
                        work.get("winner_authorities"),
                    )
                    finalists = load_candidate_rows(winners)
                    current = {int(item["position"]): item for item in finalists}
                    if any(
                        rowid not in current
                        or int(current[rowid]["version"]) != expected_version
                        or entity_authority(current[rowid]["id"], current[rowid]["version"])
                        != expected_authority
                        for rowid, expected_version, expected_authority in zip(
                            winners,
                            winner_versions,
                            winner_authorities,
                            strict=True,
                        )
                    ):
                        work = token_work("present_restart_cleanup", token_state)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    context = read_token_context(
                        position,
                        document_version,
                        token_state["token_positions"],
                    )
                    if context is None:
                        restart_document = True
                        break
                    synthetic, synthetic_positions, context_valid = context
                    if not context_valid:
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    matches, _active, valid = inflected_mentions_tokens(
                        synthetic,
                        [(str(item.get("name") or ""), str(item["id"])) for item in finalists],
                        synthetic_positions,
                        owned_start=int(token_state["owned_offset"]),
                        owned_count=min(
                            inflected_owned_tokens,
                            len(synthetic_positions) - int(token_state["owned_offset"]),
                        ),
                    )
                    if not valid:
                        work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    winner_rowids = [
                        int(item["position"]) for item in finalists if str(item["id"]) in matches
                    ]
                    by_position = {int(item["position"]): item for item in finalists}
                    final_flat = list(token_state["token_positions"])
                    final_owned = int(token_state["owned_offset"])
                    final_window_start = (
                        int(final_flat[final_owned * 2]) if final_owned < len(final_flat) // 2 else 0
                    )
                    work = token_work(
                        "inflected_link",
                        token_state,
                        winner_rowids=winner_rowids,
                        winner_versions=[int(by_position[rowid]["version"]) for rowid in winner_rowids],
                        winner_authorities=[
                            entity_authority(
                                by_position[rowid]["id"],
                                by_position[rowid]["version"],
                            )
                            for rowid in winner_rowids
                        ],
                        winner_cursor=0,
                    )
                    if not commit_pending(
                        position,
                        document_version,
                        work,
                        expected_entities=finalists,
                        winner_rows_to_store=[by_position[rowid] for rowid in winner_rowids],
                        winner_window_start=final_window_start,
                    ):
                        work = token_work("present_restart_cleanup", token_state)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                    continue

                if phase == "inflected_link":
                    token_state = token_fields(work)
                    winners, winner_versions, winner_authorities = aligned_winner_lists(
                        work.get("winner_rowids"),
                        work.get("winner_versions"),
                        work.get("winner_authorities"),
                    )
                    winner_cursor = numeric(work.get("winner_cursor"))
                    if winner_cursor > len(winners):
                        work = token_work("present_restart_cleanup", token_state)
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    if winner_cursor < len(winners):
                        candidate_position = winners[winner_cursor]
                        expected_entity_version = winner_versions[winner_cursor]
                        expected_entity_authority = winner_authorities[winner_cursor]
                        candidates = load_candidate_rows([candidate_position])
                        if (
                            not candidates
                            or int(candidates[0]["version"]) != expected_entity_version
                            or entity_authority(candidates[0]["id"], candidates[0]["version"])
                            != expected_entity_authority
                        ):
                            work = token_work("present_restart_cleanup", token_state)
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        candidate = candidates[0]
                        link_flat = list(token_state["token_positions"])
                        link_owned = int(token_state["owned_offset"])
                        if link_owned >= len(link_flat) // 2:
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        link_window_start = int(link_flat[link_owned * 2])
                        if not winner_is_authorized(
                            position,
                            document_version,
                            link_window_start,
                            candidate_position,
                            str(candidate["id"]),
                            expected_entity_version,
                        ):
                            work = token_work("present_restart_cleanup", token_state)
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        context = read_token_context(
                            position,
                            document_version,
                            token_state["token_positions"],
                        )
                        if context is None:
                            restart_document = True
                            break
                        synthetic, synthetic_positions, context_valid = context
                        if not context_valid:
                            work = {"phase": "restart_cleanup", "cleanup_cursor": 0}
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        verified_matches, _active, verified = inflected_mentions_tokens(
                            synthetic,
                            [(str(candidate.get("name") or ""), str(candidate["id"]))],
                            synthetic_positions,
                            owned_start=link_owned,
                            owned_count=min(
                                inflected_owned_tokens,
                                len(synthetic_positions) - link_owned,
                            ),
                        )
                        if not verified or str(candidate["id"]) not in verified_matches:
                            work = token_work("present_restart_cleanup", token_state)
                            if not commit_pending(position, document_version, work):
                                restart_document = True
                                break
                            continue
                        outcome = link_candidate(
                            document_rowid=position,
                            expected_version=document_version,
                            entity_rowid=candidate_position,
                            expected_entity_id=str(candidate["id"]),
                            expected_entity_version=expected_entity_version,
                            method="existing_entity_inflected_mention",
                        )
                        if outcome == "stale":
                            save_pending(position, document_version, work)
                            restart_document = True
                            break
                        if outcome in {"max_links", "max_seconds"}:
                            save_pending(position, document_version, work)
                            return result(
                                linked=linked,
                                scanned=scanned,
                                complete=False,
                                entities=entity_total,
                                budget_reason=outcome,
                            )
                        if outcome == "entity_stale":
                            work = token_work("present_restart_cleanup", token_state)
                        else:
                            work = token_work(
                                "inflected_link",
                                token_state,
                                winner_rowids=winners,
                                winner_versions=winner_versions,
                                winner_authorities=winner_authorities,
                                winner_cursor=winner_cursor + 1,
                            )
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    work = token_work("present_cleanup", token_state)
                    if not commit_pending(position, document_version, work):
                        restart_document = True
                        break
                    continue

                if phase in {"present_cleanup", "present_restart_cleanup"}:
                    token_state = token_fields(work)
                    present_remains = delete_present_page(position, document_version)
                    winner_remains = delete_winner_page(position, document_version)
                    validation_remains = delete_validation_page(position, document_version)
                    if present_remains or winner_remains or validation_remains:
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    if phase == "present_restart_cleanup":
                        work = token_work(
                            "inflected_collect",
                            token_state,
                            entity_scan_rowid=0,
                        )
                        if not commit_pending(position, document_version, work):
                            restart_document = True
                            break
                        continue
                    flat = list(token_state["token_positions"])
                    pairs = len(flat) // 2
                    owned_offset = int(token_state["owned_offset"])
                    advance = min(inflected_owned_tokens, max(0, pairs - owned_offset))
                    next_owned = owned_offset + advance
                    retain_from = max(0, next_owned - inflected_context_tokens)
                    token_state.update(
                        token_positions=flat[retain_from * 2 :],
                        owned_offset=next_owned - retain_from,
                    )
                    work = token_work("inflected_tokenize", token_state)
                    if not commit_pending(position, document_version, work):
                        restart_document = True
                        break
                    continue

                _next_cleanup, candidates_remain = delete_candidate_page(
                    position,
                    document_version,
                    after=0,
                )
                present_remains = delete_present_page(position, document_version)
                winner_remains = delete_winner_page(position, document_version)
                validation_remains = delete_validation_page(position, document_version)
                if candidates_remain or present_remains or winner_remains or validation_remains:
                    work["cleanup_cursor"] = 0
                    save_pending(position, document_version, work)
                    continue
                if phase == "restart_cleanup":
                    work = new_work()
                    if not commit_pending(position, document_version, work):
                        restart_document = True
                        break
                    continue
                if not finish_document(position, document_version):
                    restart_document = True
                    break
                pending = {}
                break

            if restart_document:
                pending = {
                    "document_rowid": position,
                    "document_version": document_version,
                    "work": clean_work(work),
                }
                continue

        return result(linked=linked, scanned=scanned, complete=False, entities=entity_total)

    def sweep_entity_duplicates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.5,
        max_pairs: int = 50_000,
    ) -> tuple[list[EntityResolutionCandidate], dict[str, Any]]:
        """One tick of the sweep: resume, work within a budget, remember where to continue.

        The ceiling used to mean «the rest was dropped», and the only trace was a
        WARNING in the log — so the reviewer saw a short list of proposals and had
        no way to tell it from «there is nothing more to merge». Measured: at 1000
        entities sharing common words the ceiling already fires, and at 2000 the
        full pass takes 137 s against the worker's 240 s timeout.

        Now the ceiling means «continue next time». The cursor is a position in the
        key ordering, kept in `runtime_kv` (no schema change — the table is core).

        When the entity set changes, the key ordering changes with it, so a key that
        sorts BEFORE the cursor is not seen until the sweep wraps. That is accepted
        deliberately rather than papered over: the sweep always terminates, every
        pair is examined within two full sweeps, and `sweeps` in the report says how
        many have completed. Restarting on every edit would let an actively edited
        graph never finish one.
        """
        state: dict[str, Any] = {}
        try:
            stored = self.kv_get(self._SWEEP_KEY + user_id)
            state = json.loads(stored) if stored else {}
        except (TypeError, ValueError):
            # Битое состояние — это рескан, а не упавший тик. Тот же выбор, что в
            # `dedup.py`: потерять позицию дешевле, чем остановить обход.
            state = {}
        if not isinstance(state, dict):
            state = {}
        after_key = None
        raw_cursor = state.get("after_key")
        if isinstance(raw_cursor, list) and len(raw_cursor) == 2:
            after_key = (int(raw_cursor[0]), [str(part) for part in raw_cursor[1]])

        candidates, report = self._duplicate_pass(
            user_id, min_confidence=min_confidence, after_key=after_key, max_pairs=max_pairs
        )

        sweeps = int(state.get("sweeps", 0) or 0)
        if not report["partial"]:
            # The space is walked out: start over next tick, and say a full sweep
            # finished — that is the only moment «no duplicates» means it.
            sweeps += 1
        self.kv_set(
            self._SWEEP_KEY + user_id,
            json.dumps({"after_key": report["stopped_at"] if report["partial"] else None, "sweeps": sweeps}),
        )
        report["sweeps"] = sweeps
        report["resumed"] = after_key is not None
        report["complete"] = not report["partial"]
        return candidates, report

    def record_knowledge_usage(
        self,
        user_id: str,
        knowledge_object_ids: list[str],
        *,
        retrieved: bool = False,
        used_in_answer: bool = False,
    ) -> int:
        """Record bounded, tenant-checked usage signals for ranking and lifecycle.

        Counts are deliberately coarse.  They improve usefulness over time
        without creating an opaque behavioral profile or allowing another
        tenant to influence a user's ranking.
        """

        unique_ids = list(dict.fromkeys(str(item) for item in knowledge_object_ids if str(item).strip()))
        if not unique_ids or (not retrieved and not used_in_answer):
            return 0
        now = utc_now()
        changed = 0
        with self.transaction() as conn:
            for knowledge_id in unique_ids[:500]:
                exists = conn.execute(
                    f"""SELECT 1 FROM knowledge_objects k
                         WHERE k.id=? AND k.user_id=? AND k.deleted_at IS NULL
                           AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                    (knowledge_id, user_id),
                ).fetchone()
                if not exists:
                    continue
                conn.execute(
                    """INSERT INTO knowledge_usage(
                           user_id, knowledge_object_id, retrieval_count, answer_count,
                           last_retrieved_at, last_used_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, knowledge_object_id) DO UPDATE SET
                         retrieval_count=knowledge_usage.retrieval_count+excluded.retrieval_count,
                         answer_count=knowledge_usage.answer_count+excluded.answer_count,
                         last_retrieved_at=COALESCE(excluded.last_retrieved_at, knowledge_usage.last_retrieved_at),
                         last_used_at=COALESCE(excluded.last_used_at, knowledge_usage.last_used_at),
                         updated_at=excluded.updated_at""",
                    (
                        user_id,
                        knowledge_id,
                        1 if retrieved else 0,
                        1 if used_in_answer else 0,
                        now if retrieved else None,
                        now if used_in_answer else None,
                        now,
                    ),
                )
                changed += 1
        return changed

    def get_knowledge_usage(self, user_id: str, knowledge_object_ids: list[str]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        unique_ids = list(dict.fromkeys(str(item) for item in knowledge_object_ids if str(item).strip()))
        for start in range(0, len(unique_ids), 400):
            chunk = unique_ids[start : start + 400]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            # The only interpolated fragment is a bounded sequence of ``?`` placeholders.
            query = f"""SELECT u.* FROM knowledge_usage u
                    JOIN knowledge_objects k
                      ON k.id=u.knowledge_object_id AND k.user_id=u.user_id
                     AND k.deleted_at IS NULL
                     AND {_not_private_knowledge_dependency("k")}
                    WHERE u.user_id=? AND u.knowledge_object_id IN ({placeholders})"""  # nosec B608
            rows = self.execute(query, (user_id, *chunk)).fetchall()
            output.update({str(row["knowledge_object_id"]): dict(row) for row in rows})
        return output

    def list_knowledge_missing_embedding(
        self,
        model: str,
        *,
        limit: int = 64,
        chunk_scheme: str = "",
        chunk_threshold: int = 0,
    ) -> list[dict[str, Any]]:
        """Knowledge Objects whose stored vector is absent, from another model, or stale.

        Staleness is keyed on the Knowledge Object ``version``, which bumps on every
        content-affecting update, so a re-enriched note is re-embedded on the next
        index cycle while a lifecycle-only change is not.

        A change to the chunking configuration (``chunk_scheme``) re-stales ONLY the
        objects long enough to actually be split, so enabling passage-level recall
        does not rewrite the whole corpus of short notes. The join stays strictly 1:1
        against ``knowledge_embeddings``, so ``limit`` keeps counting objects.
        """
        bounded = max(1, min(int(limit), 1000))
        # Хвост — `rowid`, а НЕ `id`: идентификаторы здесь `uuid4`, и хвост по ним
        # делает порядок СЛУЧАЙНЫМ между прогонами. Само по себе это было бы
        # безобидно, но бюджет тика режет пачку, и тогда случайным становится
        # её состав. `rowid` — порядок вставки: устойчивый и осмысленный.
        where, params = self._missing_embedding_filter(model, chunk_scheme, chunk_threshold)
        rows = self.execute(
            """SELECT k.id AS id, k.user_id AS user_id, k.version AS version,
                      k.title AS title, k.summary AS summary, k.content AS content,
                      k.tags_json AS tags_json, k.knowledge_kind AS knowledge_kind,
                      (e.knowledge_object_id IS NOT NULL
                       AND COALESCE(e.content_hash, '') = '') AS forced
               FROM knowledge_objects k
               LEFT JOIN knowledge_embeddings e
                 ON e.knowledge_object_id = k.id AND e.user_id = k.user_id
               WHERE """
            + where
            + """
               ORDER BY k.updated_at DESC, k.rowid DESC
               LIMIT ?""",  # nosec B608
            (*params, bounded),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _missing_embedding_filter(
        model: str, chunk_scheme: str, chunk_threshold: int
    ) -> tuple[str, tuple[Any, ...]]:
        """Условие «вектор отсутствует, чужой или устарел» — одно на выборку и счёт."""
        return (
            """k.deleted_at IS NULL
                 AND """
            + _not_private_knowledge_dependency("k")
            + """
                 AND (e.knowledge_object_id IS NULL
                      OR e.model != ?
                      OR e.source_version != k.version
                      OR (e.chunk_scheme != ? AND """
            + _SEARCH_TEXT_LEN_SQL
            + """ > ?))""",
            (model, chunk_scheme, max(0, int(chunk_threshold))),
        )

    def count_knowledge_missing_embedding(
        self, model: str, *, chunk_scheme: str = "", chunk_threshold: int = 0
    ) -> int:
        """Сколько объектов ещё ждут вектора.

        На корпусе в тысячи документов индексация идёт часами, и это единственное
        число, отличающее «работает» от «встало». Считается ТЕМ ЖЕ условием, что и
        выборка, иначе прогресс начнёт врать при первой правке порога чанкования.
        """
        where, params = self._missing_embedding_filter(model, chunk_scheme, chunk_threshold)
        row = self.execute(
            """SELECT COUNT(*) AS count FROM knowledge_objects k
               LEFT JOIN knowledge_embeddings e
                 ON e.knowledge_object_id = k.id AND e.user_id = k.user_id
               WHERE """
            + where,  # nosec B608
            params,
        ).fetchone()
        return int(row["count"] if row else 0)

    def get_user_chunk_embeddings(
        self,
        user_id: str,
        model: str,
        dim: int,
        *,
        object_limit: int | None = None,
        row_limit: int | None = None,
        uploaded_by: str | None = None,
    ) -> list[tuple[str, bytes]]:
        """Return (``ko_id#chunk_index``, packed vector) for a user's passage vectors.

        ``object_limit`` is the SAME object window ``get_user_embeddings`` uses, so a
        capped scan never covers a document only halfway; ``row_limit`` is a pure fuse
        on top of it for a corpus where the objects in that window are heavily split.
        Soft-deleted objects are excluded, mirroring the whole-object query — without
        that, deleted knowledge would resurrect through its chunks.
        """
        # There are two opposite good plans, and neither is safe unconditionally.
        #
        # * Sparse/rolling index: start at the exact (user, model, dim) chunk rows,
        #   then sort the small survivor set.  Walking every KO would be 20-30x
        #   slower when only 1% of passage rows belong to the current model.
        # * Dense/current index: walk KOs in final order and look up their chunks by
        #   the composite PK.  This avoids sorting tens of thousands of vector BLOBs.
        #
        # The profile reads covering indexes only.  The two gates are deliberately
        # conservative: more than two active chunks per object in the scanned window,
        # and strictly more than 75% of this tenant's chunks on the active model/dim.
        # Production method, synthetic 5k x 16 with 1024-float BLOBs and privacy
        # predicates: 469.19 ms -> 60.06 ms; at 1% the legacy branch stays around
        # 3 ms instead of taking about 12 ms object-first.
        author = str(uploaded_by) if uploaded_by is not None else None
        if author is not None and not author.strip():
            return []
        profile = self.execute(
            """SELECT
                 (SELECT COUNT(*) FROM knowledge_chunk_embeddings
                   WHERE user_id=?) AS total_chunks,
                 (SELECT COUNT(*) FROM knowledge_chunk_embeddings
                   WHERE user_id=? AND model=? AND dim=?) AS active_chunks,
                 (SELECT COUNT(*) FROM knowledge_objects
                   WHERE user_id=? AND deleted_at IS NULL) AS live_objects""",
            (user_id, user_id, model, int(dim), user_id),
        ).fetchone()
        total_chunks = int(profile["total_chunks"] if profile else 0)
        active_chunks = int(profile["active_chunks"] if profile else 0)
        live_objects = int(profile["live_objects"] if profile else 0)
        # The parent-first branch physically walks the tenant KO order index before
        # testing Raw uploader provenance.  Its cost denominator must therefore be
        # the tenant population, not the much smaller matching-author population;
        # using the latter opened this plan on a sparse tenant and was ~26x slower
        # than the chunk-first branch (28.6 ms vs 1.1 ms on synthetic 6k/600).
        object_window = live_objects
        if object_limit is not None and object_limit > 0:
            object_window = min(object_window, int(object_limit))
        use_index_order = (
            object_window > 0 and active_chunks > 2 * object_window and 4 * active_chunks > 3 * total_chunks
        )

        if use_index_order:
            # CROSS JOIN fixes the outer loop.  INDEXED BY must name the PK index:
            # without it SQLite prefers idx_knowledge_chunks_user_model for every
            # parent and turns an 14-ms scan into a multi-second nested scan.  This
            # is the deterministic autoindex of the table's sole composite PK;
            # migration tests cover every shipped schema that can create the table.
            query = (
                "SELECT c.knowledge_object_id || '#' || c.chunk_index AS id, c.vector AS vector "
                "FROM knowledge_objects k INDEXED BY idx_knowledge_chunk_scan_order "
                "CROSS JOIN knowledge_chunk_embeddings c "
                "INDEXED BY sqlite_autoindex_knowledge_chunk_embeddings_1 "
                "ON c.knowledge_object_id = k.id "
                "WHERE k.user_id = ? AND k.deleted_at IS NULL "
                f"AND {_not_private_knowledge_dependency('k')} "  # nosec B608
            )
            params: list[Any] = [user_id]
            if author is not None:
                query += f"AND {_exact_uploader_knowledge_dependency('k')} "
                params.append(author)
            query += "AND c.user_id = ? AND c.model = ? AND c.dim = ?"
            params.extend([user_id, model, int(dim)])
        else:
            query = (
                "SELECT c.knowledge_object_id || '#' || c.chunk_index AS id, c.vector AS vector "
                "FROM knowledge_chunk_embeddings c "
                "JOIN knowledge_objects k ON k.id = c.knowledge_object_id "
                "WHERE c.user_id = ? AND c.model = ? AND c.dim = ? "
                "AND k.user_id = ? AND k.deleted_at IS NULL "
                f"AND {_not_private_knowledge_dependency('k')}"  # nosec B608
            )
            # The denormalised chunk owner is not an FK to the parent owner.  Both
            # sides are therefore tenant predicates: malformed rows fail closed in
            # the sparse branch exactly as they do in the parent-first branch.
            params = [user_id, model, int(dim), user_id]
            if author is not None:
                query += f" AND {_exact_uploader_knowledge_dependency('k')}"
                params.append(author)
        if object_limit is not None and object_limit > 0:
            if use_index_order:
                # rowid is only the membership key; selection itself has the explicit
                # total (created_at, id) order, so VACUUM/insertion order cannot move
                # an equal-timestamp object across the recall-window boundary.
                query += (
                    " AND k.rowid IN ("
                    "SELECT window_k.rowid FROM knowledge_objects window_k "
                    "INDEXED BY idx_knowledge_chunk_scan_order "
                    "WHERE window_k.user_id = ? AND window_k.deleted_at IS NULL "
                    f"AND {_not_private_knowledge_dependency('window_k')} "  # nosec B608
                )
            else:
                query += (
                    " AND c.knowledge_object_id IN ("
                    "SELECT window_k.id FROM knowledge_objects window_k "
                    "INDEXED BY idx_knowledge_chunk_scan_order "
                    "WHERE window_k.user_id = ? AND window_k.deleted_at IS NULL "
                    f"AND {_not_private_knowledge_dependency('window_k')} "  # nosec B608
                )
            params.append(user_id)
            if author is not None:
                query += f"AND {_exact_uploader_knowledge_dependency('window_k')} "
                params.append(author)
            query += "ORDER BY window_k.created_at DESC, window_k.id ASC LIMIT ?)"
            params.append(int(object_limit))
        query += " ORDER BY k.created_at DESC, k.id, c.chunk_index"
        if row_limit is not None and row_limit > 0:
            query += " LIMIT ?"
            params.append(int(row_limit))
        rows = self.execute(query, tuple(params)).fetchall()
        return [(str(row["id"]), bytes(row["vector"])) for row in rows]

    # `k.*` тянуло `content` КАЖДОГО объекта, а обход честно неограничен — он идёт
    # по всему корпусу страницами до конца. Замерено на 5000 объектов по 3.5 КБ:
    # 45 МБ пикового потребления на страницу из ПЯТИДЕСЯТИ строк, и дашборд делает
    # два таких обхода на один рендер. На корпусе владельца (2107 документов,
    # медиана 19 КБ) это сотни мегабайт на запрос.
    #
    # Вердикту тело не нужно вовсе: он читает оценки, счётчики использования,
    # `content_type` и `metadata_json`. Интерфейс показывает `summary || content`
    # обрезанными до 160 символов — им и отдаём срез, а не весь документ.
    _LIFECYCLE_SQL = f"""SELECT k.id, k.user_id, k.title, k.knowledge_kind, k.content_type,
                      k.metadata_json, k.importance, k.quality_score, k.promotion_score,
                      k.lifecycle_stage, k.created_at, k.updated_at,
                      substr(k.summary, 1, 400) AS summary,
                      substr(k.content, 1, 400) AS content,
                      u.retrieval_count, u.answer_count,
                      u.positive_feedback_count, u.negative_feedback_count,
                      u.last_retrieved_at, u.last_used_at, u.last_feedback_at
               FROM knowledge_objects k
               LEFT JOIN knowledge_usage u
                 ON u.user_id=k.user_id AND u.knowledge_object_id=k.id
               WHERE k.user_id=? AND k.lifecycle_stage='active' AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}
                 AND datetime(k.updated_at) < datetime('now', ?)
               ORDER BY k.importance ASC, k.updated_at ASC, k.id ASC LIMIT ? OFFSET ?"""

    def _lifecycle_candidates(self, user_id: str, days: int) -> list[dict[str, Any]]:
        """EVERY candidate, walked in full — the one list the count and the page share.

        The SQL prefilter is exact but the verdict is not: protection reasons read the
        object's metadata and the risk cutoff is arithmetic over its scores. Taking 500
        rows and filtering afterwards made the reported number saturate BELOW the limit
        and look like a real count — measured, 900 true candidates showed as 200,
        because protected file-derived objects sit at importance 0 and `importance ASC`
        feeds them first, eating the window.

        The predicate could be expressed in SQL — it was verified to match by sets of
        ids — but then there would be two implementations of one rule, and the second
        would drift silently the first time a threshold moves. One walk cannot.
        """
        found: list[dict[str, Any]] = []
        offset = 0
        while True:
            rows = self.execute(self._LIFECYCLE_SQL, (user_id, f"-{days} days", 500, offset)).fetchall()
            if not rows:
                break
            for row in rows:
                verdict = self._lifecycle_verdict(dict(row), days)
                if verdict:
                    found.append(verdict)
            offset += len(rows)
            if len(rows) < 500:
                break
        found.sort(key=lambda item: (-item["risk_score"], str(item["knowledge_object"].get("id", ""))))
        return found

    def count_lifecycle_candidates(self, user_id: str, *, days_threshold: int = 90) -> int:
        """How many there really are — the number the tile shows."""
        return len(self._lifecycle_candidates(user_id, max(1, min(int(days_threshold), 36500))))

    def all_lifecycle_candidates(self, user_id: str, *, days_threshold: int = 90) -> list[dict[str, Any]]:
        """The whole candidate set, for callers that must not miss one.

        `apply` validates `require_candidate` against this. It used to rebuild a
        5000-row listing and look inside, which was safe only while the visible table
        was a prefix of that pool — measured on 50000 objects the pool truncates on its
        own (8747 true, 2174 returned), so a paged table would have had ids that the
        guard rejected as `not_a_current_candidate` while being current.
        """
        return self._lifecycle_candidates(user_id, max(1, min(int(days_threshold), 36500)))

    def _lifecycle_verdict(self, item: dict[str, Any], days: int) -> dict[str, Any] | None:
        if _lifecycle_protection_reasons(item, days):
            return None
        if True:
            # `or 0.5` read a stored 0.0 as 0.5, because zero is falsy — so the one
            # value that should weigh MOST toward review was the one value the scan
            # ignored. Missing and zero are different things here.
            importance = _score_or(item.get("importance"))
            quality = _score_or(item.get("quality_score"))
            promotion = _score_or(item.get("promotion_score"))
            retrievals = int(item.get("retrieval_count") or 0)
            answers = int(item.get("answer_count") or 0)
            negative = int(item.get("negative_feedback_count") or 0)
            risk = (
                (1.0 - importance) * 0.38
                + (1.0 - quality) * 0.22
                + (1.0 - promotion) * 0.16
                + (0.12 if retrievals == 0 else 0.0)
                + (0.08 if answers == 0 else 0.0)
                + min(0.12, negative * 0.04)
            )
            if risk < 0.48:
                return None
            reasons = ["not updated within threshold"]
            if importance < 0.35:
                reasons.append("low importance")
            if quality < 0.4:
                reasons.append("low quality score")
            if promotion < 0.4:
                reasons.append("weak original promotion confidence")
            if retrievals == 0 and answers == 0:
                reasons.append("never used")
            if negative:
                reasons.append("negative feedback")
            return {
                "knowledge_object": item,
                "risk_score": round(min(1.0, risk), 4),
                "recommended_action": "review_for_archive" if risk >= 0.68 else "review_importance",
                "suggested_importance": round(max(0.1, importance - min(0.2, risk * 0.15)), 3),
                "reasons": reasons,
                "protected": False,
            }
        return None

    def list_lifecycle_candidates(
        self,
        user_id: str,
        *,
        days_threshold: int = 90,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """One page of explainable, review-only stale-knowledge candidates.

        Never mutates lifecycle or importance. Manually reviewed, file-derived,
        recently used or positively rated knowledge is protected from suggestions.
        """
        days = max(1, min(int(days_threshold), 36500))
        found = self._lifecycle_candidates(user_id, days)
        start = max(0, offset)
        return found[start : start + max(1, min(int(limit), 5000))]

    def archive_selected_knowledge(
        self, user_id: str, ids: Sequence[str], *, days_threshold: int = 90
    ) -> dict[str, Any]:
        """Archive the objects the reviewer chose, honouring the same protections.

        Replaces ``deprecate_stale_knowledge``, which swept every active object
        under ``importance < 0.3`` older than the threshold — no selection, and
        none of the protections `list_lifecycle_candidates` applies. A file the
        owner uploaded, a note they explicitly saved, something used in an answer
        last week: all archived in one unreviewed call. DATA_LIFECYCLE §5 says the
        opposite in as many words — "изменение importance/lifecycle применяется
        только к явно выбранным объектам".

        A selected object that is protected is reported, not silently skipped: the
        reviewer asked for it and deserves to know why it did not happen.
        """
        days = max(1, min(int(days_threshold), 36500))
        archived: list[str] = []
        skipped: list[dict[str, str]] = []
        for ko_id in list(dict.fromkeys(str(item) for item in ids))[:1000]:
            row = self.execute(
                f"""SELECT k.*, u.positive_feedback_count, u.negative_feedback_count,
                          u.last_retrieved_at, u.last_used_at
                   FROM knowledge_objects k
                   LEFT JOIN knowledge_usage u
                     ON u.user_id=k.user_id AND u.knowledge_object_id=k.id
                   WHERE k.id=? AND k.user_id=?
                     AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (ko_id, user_id),
            ).fetchone()
            if row is None:
                skipped.append({"id": ko_id, "reason": "not found"})
                continue
            item = dict(row)
            if item.get("deleted_at"):
                skipped.append({"id": ko_id, "reason": "soft-deleted"})
                continue
            if item.get("lifecycle_stage") != LifecycleStage.ACTIVE.value:
                skipped.append({"id": ko_id, "reason": f"already {item.get('lifecycle_stage')}"})
                continue
            protection = _lifecycle_protection_reasons(item, days)
            if protection:
                skipped.append({"id": ko_id, "reason": "; ".join(protection)})
                continue
            if self.update_knowledge_fields(ko_id, user_id, lifecycle_stage=LifecycleStage.ARCHIVED.value):
                archived.append(ko_id)
            else:
                skipped.append({"id": ko_id, "reason": "update failed"})
        return {"archived": archived, "skipped": skipped}

    def get_lifecycle_stats(self, user_id: str) -> dict[str, int]:
        rows = self.execute(
            f"""SELECT k.lifecycle_stage, COUNT(*) AS count FROM knowledge_objects k
                 WHERE k.user_id=? AND {_not_private_knowledge_dependency("k")}
                 GROUP BY k.lifecycle_stage""",  # nosec B608
            (user_id,),
        ).fetchall()
        result = {stage.value: 0 for stage in LifecycleStage}
        result.update({row["lifecycle_stage"]: int(row["count"]) for row in rows})
        return result
