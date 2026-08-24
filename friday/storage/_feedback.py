"""Storage methods for feedback, its rollup state and the eval gold set.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import unicodedata

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage._base import (
    EVAL_MINED_CASE_CAP,
    Any,
    FeedbackItem,
    Sequence,
    StorageShared,
    _json_load,
    json,
    math,
    new_id,
    utc_now,
)
from friday.storage._privacy import (
    _not_private_entity_material_dependency,
    _not_private_inbox_dependency,
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
    _not_private_relation_candidate_dependency,
    _not_private_relation_dependency,
    _not_private_resolution_candidate_dependency,
)

_FEEDBACK_TEXT_MAX_BYTES = 65_536
_FEEDBACK_JSON_MAX_BYTES = 1_048_576
_FEEDBACK_VALUE_BUDGET = 20_000
_FEEDBACK_ID_MAX_CHARS = 512
_EVAL_SOURCES = {"manual", "feedback", "bootstrap"}
_FEEDBACK_TARGET_TYPES = {
    "answer",
    "classification",
    "entity",
    "entity_resolution_candidate",
    "inbox",
    "knowledge_object",
    "raw",
    "raw_object",
    "relation",
    "relation_candidate",
}
_FEEDBACK_TYPES = {
    "answer_usefulness",
    "classification",
    "entity_link",
    "general",
    "search_quality",
}


def _privacy_casefold(value: Any) -> str:
    return unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", str(value)).casefold(),
    )


def _bounded_decoded_strings(*values: Any, max_bytes: int) -> list[str] | None:
    """Return every decoded string, failing closed on opaque or hostile material."""

    pending = list(values)
    strings: list[str] = []
    visited = 0
    consumed = 0
    while pending:
        visited += 1
        if visited > _FEEDBACK_VALUE_BUDGET:
            return None
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        if isinstance(value, (list, tuple)):
            pending.extend(value)
            continue
        if value is None:
            continue
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-8")
            except UnicodeError:
                return None
        else:
            text = str(value)
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            return None
        consumed += size
        if size > max_bytes or consumed > max_bytes:
            return None
        strings.append(text)
        candidate = text.lstrip()
        if not candidate.startswith(("{", "[", '"')):
            continue
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            return None
        if isinstance(decoded, (dict, list, str)) and decoded != text:
            pending.append(decoded)
    return strings


def _feedback_dependency_user(storage: Any, user_id: str) -> str:
    return LEGACY_OWNER_USER_ID if getattr(storage.settings, "shared_archive", False) else user_id


def _feedback_token_scope(
    storage: Any,
    conn: Any,
    user_id: str,
) -> tuple[str, tuple[set[str], set[str]]]:
    dependency_user_id = _feedback_dependency_user(storage, user_id)
    if not getattr(storage.settings, "shared_archive", False):
        return dependency_user_id, _private_feedback_tokens(conn, dependency_user_id)
    archive_exact, archive_names = _private_feedback_tokens(
        conn,
        dependency_user_id,
        person_id=user_id,
    )
    # New reminders live directly in the person's namespace.  Legacy reminders
    # that could not be moved remain in the shared tenant and are selected above
    # only for their exact owner.  Union both without ever scanning Bob's
    # personal namespace while validating Alice's feedback.
    person_exact, person_names = _private_feedback_tokens(conn, user_id)
    return dependency_user_id, (archive_exact | person_exact, archive_names | person_names)


def _eval_token_scope(
    storage: Any,
    conn: Any,
    user_id: str,
) -> tuple[str, tuple[set[str], set[str]]]:
    dependency_user_id = _feedback_dependency_user(storage, user_id)
    # Eval is an archive-wide gold set, not personal feedback.  In shared mode
    # it must never turn any participant's private reminder into a shared query
    # or note, including reminders already moved to that person's namespace.
    token_user_id = None if getattr(storage.settings, "shared_archive", False) else dependency_user_id
    return dependency_user_id, _private_feedback_tokens(conn, token_user_id)


def _private_feedback_tokens(
    conn: Any,
    user_id: str | None,
    *,
    person_id: str | None = None,
) -> tuple[set[str], set[str]]:
    """Load only identifiers/names needed for in-memory rejection; never expose them."""

    tenant_filter = ""
    person_filter = ""
    params: list[Any] = []
    if user_id is not None:
        tenant_filter = "user_id=? AND"
        params.append(user_id)
    if person_id is not None:
        # Shared-archive feedback belongs to a person while graph rows belong to
        # the archive tenant.  A clean Alice row must not flicker when Bob adds a
        # private reminder with the same ordinary name, but Alice's own marker
        # and any ambiguous legacy ownership must still fail closed.
        person_filter = """AND (
            EXISTS (
                SELECT 1 FROM private_entity_owners exact_feedback_owner
                 WHERE exact_feedback_owner.entity_id=private_feedback_entity.id
                   AND exact_feedback_owner.person_id=?
                   AND exact_feedback_owner.privacy_kind='reminder'
            )
            OR NOT EXISTS (
                SELECT 1 FROM private_entity_owners any_feedback_owner
                 WHERE any_feedback_owner.entity_id=private_feedback_entity.id
            )
            OR EXISTS (
                SELECT 1 FROM private_entity_owners ambiguous_feedback_owner
                 WHERE ambiguous_feedback_owner.entity_id=private_feedback_entity.id
                   AND ambiguous_feedback_owner.privacy_kind<>'reminder'
            )
            OR EXISTS (
                SELECT 1 FROM private_entity_owners first_feedback_owner
                JOIN private_entity_owners second_feedback_owner
                  ON second_feedback_owner.entity_id=first_feedback_owner.entity_id
                 AND second_feedback_owner.person_id<>first_feedback_owner.person_id
                 WHERE first_feedback_owner.entity_id=private_feedback_entity.id
            )
            OR EXISTS (
                SELECT 1 FROM entity_time unmatched_feedback_time
                 WHERE unmatched_feedback_time.entity_id=private_feedback_entity.id
                   AND unmatched_feedback_time.source LIKE 'reminder:%'
                   AND NOT EXISTS (
                       SELECT 1 FROM private_entity_owners matching_feedback_owner
                        WHERE matching_feedback_owner.entity_id=private_feedback_entity.id
                          AND matching_feedback_owner.privacy_kind='reminder'
                          AND unmatched_feedback_time.source=
                              'reminder:' || matching_feedback_owner.person_id
                   )
            )
        )"""
        params.append(person_id)
    entity_rows = conn.execute(
        f"""SELECT DISTINCT private_feedback_entity.id,
                            private_feedback_identity.name
               FROM entities private_feedback_entity
               JOIN private_entity_material_closure private_feedback_identity
                 ON private_feedback_identity.id=private_feedback_entity.id
              WHERE {tenant_filter} 1
             {person_filter}""",  # nosec B608 - code-owned predicate
        tuple(params),
    ).fetchall()
    knowledge_predicate = _not_private_knowledge_dependency("private_feedback_knowledge")
    knowledge_rows = conn.execute(
        f"""SELECT id FROM knowledge_objects private_feedback_knowledge
             WHERE {tenant_filter} NOT ({knowledge_predicate})""",  # nosec B608 - code-owned predicate
        (() if user_id is None else (user_id,)),
    ).fetchall()
    exact = {
        *(str(row["id"] or "") for row in entity_rows if str(row["id"] or "")),
        *(str(row["id"] or "") for row in knowledge_rows if str(row["id"] or "")),
    }
    names = {_privacy_casefold(row["name"] or "") for row in entity_rows if str(row["name"] or "")}
    return exact, names


def _feedback_material_is_visible(
    values: Sequence[Any],
    private_tokens: tuple[set[str], set[str]],
    *,
    max_bytes: int = _FEEDBACK_JSON_MAX_BYTES,
) -> bool:
    strings = _bounded_decoded_strings(*values, max_bytes=max_bytes)
    if strings is None:
        return False
    exact, folded_names = private_tokens
    for text in strings:
        if any(token in text for token in exact):
            return False
        if folded_names:
            folded = _privacy_casefold(text)
            if any(name in folded for name in folded_names):
                return False
    return True


def _parse_expected_ids(value: Any) -> list[str] | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _FEEDBACK_JSON_MAX_BYTES:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(decoded, list) or len(decoded) > 5_000:
        return None
    ids: list[str] = []
    for item in decoded:
        if not isinstance(item, str) or not item.strip() or len(item) > _FEEDBACK_ID_MAX_CHARS:
            return None
        ids.append(item)
    return ids or None


def _knowledge_ids_are_visible(conn: Any, user_id: str, knowledge_ids: Sequence[str]) -> bool:
    unique = list(dict.fromkeys(str(item) for item in knowledge_ids))
    predicate = _not_private_knowledge_dependency("feedback_knowledge")
    for start in range(0, len(unique), 400):
        batch = unique[start : start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT feedback_knowledge.id, feedback_knowledge.user_id,
                       CASE WHEN {predicate} THEN 1 ELSE 0 END AS is_visible
                  FROM knowledge_objects feedback_knowledge
                 WHERE feedback_knowledge.id IN ({placeholders})""",  # nosec B608
            tuple(batch),
        ).fetchall()
        if any(str(row["user_id"]) != user_id or not int(row["is_visible"]) for row in rows):
            return False
    return True


def _feedback_target_is_visible(
    conn: Any,
    user_id: str,
    target_type: str,
    target_id: str,
    dependency_user_id: str,
) -> bool:
    """Resolve typed feedback targets through the same public dependency boundary."""

    if not target_id or len(target_id) > _FEEDBACK_ID_MAX_CHARS:
        return False
    if target_type == "knowledge_object":
        predicate = _not_private_knowledge_dependency("target_knowledge")
        row = conn.execute(
            f"""SELECT 1 FROM knowledge_objects target_knowledge
                 WHERE id=? AND user_id=? AND {predicate}""",  # nosec B608
            (target_id, user_id),
        ).fetchone()
        return row is not None
    if target_type == "answer":
        row = conn.execute(
            """SELECT metadata_json FROM messages
                WHERE id=? AND user_id=? AND role='assistant'""",
            (target_id, user_id),
        ).fetchone()
        if row is None:
            return False
        raw_metadata = row["metadata_json"]
        if not isinstance(raw_metadata, str) or len(raw_metadata.encode("utf-8")) > _FEEDBACK_JSON_MAX_BYTES:
            return False
        try:
            metadata = json.loads(raw_metadata)
        except (TypeError, ValueError, RecursionError):
            return False
        if not isinstance(metadata, dict):
            return False
        ids_value = metadata.get("knowledge_object_ids", [])
        citations_value = metadata.get("knowledge_citations", {})
        if not isinstance(ids_value, list) or not isinstance(citations_value, dict):
            return False
        ids = [item for item in ids_value if isinstance(item, str) and item.strip()]
        if len(ids) != len(ids_value) or any(
            not isinstance(item, str) or not item.strip() for item in citations_value.values()
        ):
            return False
        ids.extend(str(item) for item in citations_value.values())
        return not ids or _knowledge_ids_are_visible(conn, dependency_user_id, ids)
    if target_type in {"classification", "raw", "raw_object"}:
        predicate = _not_private_raw_dependency("target_raw")
        row = conn.execute(
            f"""SELECT 1 FROM raw_objects target_raw
                 WHERE id=? AND user_id=? AND {predicate}
                   AND friday_secondary_product_witness_raw(
                       target_raw.source, target_raw.source_ref, target_raw.raw_content,
                       target_raw.content_hash, target_raw.metadata_json
                   )=0""",  # nosec B608
            (target_id, user_id),
        ).fetchone()
        return row is not None
    if target_type == "inbox":
        predicate = _not_private_inbox_dependency("target_inbox")
        raw_predicate = _not_private_raw_dependency("target_raw")
        row = conn.execute(
            f"""SELECT 1 FROM inbox target_inbox
                 JOIN raw_objects target_raw
                   ON target_raw.id=target_inbox.raw_object_id
                  AND target_raw.user_id=target_inbox.user_id
                 WHERE target_inbox.id=? AND target_inbox.user_id=?
                   AND {predicate} AND {raw_predicate}
                   AND friday_secondary_product_witness_raw(
                       target_raw.source, target_raw.source_ref, target_raw.raw_content,
                       target_raw.content_hash, target_raw.metadata_json
                   )=0""",  # nosec B608
            (target_id, user_id),
        ).fetchone()
        return row is not None
    if target_type == "entity":
        predicate = _not_private_entity_material_dependency("target_entity")
        row = conn.execute(
            f"""SELECT 1 FROM entities target_entity
                 WHERE id=? AND user_id=? AND deleted_at IS NULL AND {predicate}""",  # nosec B608
            (target_id, user_id),
        ).fetchone()
        return row is not None
    if target_type == "relation":
        relation = _not_private_relation_dependency("target_relation")
        source = _not_private_entity_material_dependency("target_source")
        target = _not_private_entity_material_dependency("target_destination")
        row = conn.execute(
            f"""SELECT 1 FROM relations target_relation
                 JOIN entities target_source
                   ON target_source.id=target_relation.source_entity_id
                  AND target_source.user_id=target_relation.user_id AND {source}
                 JOIN entities target_destination
                   ON target_destination.id=target_relation.target_entity_id
                  AND target_destination.user_id=target_relation.user_id AND {target}
                 WHERE target_relation.id=? AND target_relation.user_id=?
                   AND target_relation.deleted_at IS NULL AND {relation}""",  # nosec B608
            (target_id, user_id),
        ).fetchone()
        return row is not None
    if target_type == "relation_candidate":
        predicate = _not_private_relation_candidate_dependency("target_candidate")
        source = _not_private_entity_material_dependency("candidate_source")
        target = _not_private_entity_material_dependency("candidate_destination")
        row = conn.execute(
            f"""SELECT 1 FROM relation_candidates target_candidate
                 JOIN entities candidate_source
                   ON candidate_source.id=target_candidate.source_entity_id
                  AND candidate_source.user_id=target_candidate.user_id AND {source}
                 JOIN entities candidate_destination
                   ON candidate_destination.id=target_candidate.target_entity_id
                  AND candidate_destination.user_id=target_candidate.user_id AND {target}
                 WHERE target_candidate.id=? AND target_candidate.user_id=? AND {predicate}""",  # nosec B608
            (target_id, user_id),
        ).fetchone()
        return row is not None
    if target_type == "entity_resolution_candidate":
        predicate = _not_private_resolution_candidate_dependency("target_resolution")
        left = _not_private_entity_material_dependency("resolution_left")
        right = _not_private_entity_material_dependency("resolution_right")
        row = conn.execute(
            f"""SELECT 1 FROM entity_resolution_candidates target_resolution
                 JOIN entities resolution_left
                   ON resolution_left.id=target_resolution.entity_a_id
                  AND resolution_left.user_id=target_resolution.user_id AND {left}
                 JOIN entities resolution_right
                   ON resolution_right.id=target_resolution.entity_b_id
                  AND resolution_right.user_id=target_resolution.user_id AND {right}
                 WHERE target_resolution.id=? AND target_resolution.user_id=? AND {predicate}""",  # nosec B608
            (target_id, user_id),
        ).fetchone()
        return row is not None
    return True


def _eval_case_is_visible(
    conn: Any,
    row: dict[str, Any],
    private_tokens: tuple[set[str], set[str]],
    dependency_user_id: str,
) -> tuple[bool, list[str]]:
    if str(row.get("source") or "") not in _EVAL_SOURCES:
        return False, []
    expected_ids = _parse_expected_ids(row.get("expected_ids_json"))
    if expected_ids is None or not _knowledge_ids_are_visible(conn, dependency_user_id, expected_ids):
        return False, []
    visible = _feedback_material_is_visible(
        tuple(row.values()),
        private_tokens,
    )
    return visible, expected_ids if visible else []


def _feedback_row_is_visible(
    conn: Any,
    row: dict[str, Any],
    private_tokens: tuple[set[str], set[str]],
    dependency_user_id: str,
) -> bool:
    context_raw = row.get("context_json", "{}")
    if not isinstance(context_raw, str) or len(context_raw.encode("utf-8")) > _FEEDBACK_JSON_MAX_BYTES:
        return False
    try:
        context = json.loads(context_raw)
    except (TypeError, ValueError, RecursionError):
        return False
    if not isinstance(context, dict):
        return False
    knowledge_ids_value = context.get("knowledge_object_ids", [])
    if not isinstance(knowledge_ids_value, list) or any(
        not isinstance(item, str) or not item.strip() or len(item) > _FEEDBACK_ID_MAX_CHARS
        for item in knowledge_ids_value
    ):
        return False
    knowledge_ids = list(dict.fromkeys(knowledge_ids_value))
    target_type = str(row.get("target_type") or "")
    target_id = str(row.get("target_id") or "")
    user_id = str(row.get("user_id") or "")
    if (
        target_type not in _FEEDBACK_TARGET_TYPES
        or str(row.get("feedback_type") or "") not in _FEEDBACK_TYPES
    ):
        return False
    target_user_id = user_id if target_type == "answer" else dependency_user_id
    if not _feedback_target_is_visible(
        conn,
        target_user_id,
        target_type,
        target_id,
        dependency_user_id,
    ):
        return False
    if target_type == "knowledge_object":
        knowledge_ids.append(target_id)
    if knowledge_ids and not _knowledge_ids_are_visible(
        conn,
        dependency_user_id,
        knowledge_ids,
    ):
        return False
    return _feedback_material_is_visible((*row.values(), context), private_tokens)


class FeedbackMixin(StorageShared):
    def add_eval_case(
        self,
        user_id: str,
        query: str,
        expected_ids: Sequence[str],
        *,
        note: str = "",
        source: str = "manual",
    ) -> dict[str, Any]:
        clean_query = " ".join(str(query or "").split()).strip()
        if not clean_query:
            raise ValueError("Eval case query is required")
        ids = sorted({str(item) for item in expected_ids if str(item).strip()})
        if not ids:
            raise ValueError("At least one expected knowledge object is required")
        clean_source = str(source or "")[:40]
        if clean_source not in _EVAL_SOURCES:
            raise ValueError("Invalid eval case source")
        now = utc_now()
        with self.transaction() as conn:
            dependency_user_id, private_tokens = _eval_token_scope(self, conn, user_id)
            existing = conn.execute(
                "SELECT * FROM eval_cases WHERE user_id=? AND query=?",
                (user_id, clean_query),
            ).fetchone()
            if (
                existing is not None
                and not _eval_case_is_visible(
                    conn,
                    dict(existing),
                    private_tokens,
                    dependency_user_id,
                )[0]
            ):
                raise ValueError("Eval case depends on private knowledge")
            candidate = {
                "user_id": user_id,
                "query": clean_query,
                "expected_ids_json": json.dumps(ids),
                "note": note[:500],
                "source": clean_source,
            }
            if not _eval_case_is_visible(conn, candidate, private_tokens, dependency_user_id)[0]:
                raise ValueError("Eval case depends on private knowledge")
            conn.execute(
                """INSERT INTO eval_cases(id, user_id, query, expected_ids_json, note, source, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, query) DO UPDATE SET
                     expected_ids_json=excluded.expected_ids_json,
                     note=excluded.note, source=excluded.source""",
                (new_id("eval"), user_id, clean_query, json.dumps(ids), note[:500], clean_source, now),
            )
        return next((case for case in self.list_eval_cases(user_id) if case["query"] == clean_query), {})

    def upsert_feedback_eval_case(self, user_id: str, query: str, expected_ids: Sequence[str]) -> bool:
        """Insert or refresh a feedback-mined eval case, never overwriting a manual one.

        The conditional ``WHERE source<>'manual'`` on the conflict path leaves a
        hand-curated case for the same query untouched. Returns True if a case was
        written or refreshed.
        """
        clean_query = " ".join(str(query or "").split()).strip()[:500]
        ids = sorted({str(item) for item in expected_ids if str(item).strip()})
        if not clean_query or not ids:
            return False
        with self.transaction() as conn:
            dependency_user_id, private_tokens = _eval_token_scope(self, conn, user_id)
            existing = conn.execute(
                "SELECT * FROM eval_cases WHERE user_id=? AND query=?",
                (user_id, clean_query),
            ).fetchone()
            if (
                existing is not None
                and not _eval_case_is_visible(
                    conn,
                    dict(existing),
                    private_tokens,
                    dependency_user_id,
                )[0]
            ):
                return False
            candidate = {
                "user_id": user_id,
                "query": clean_query,
                "expected_ids_json": json.dumps(ids),
                "note": "auto: подтверждённый feedback",
                "source": "feedback",
            }
            if not _eval_case_is_visible(conn, candidate, private_tokens, dependency_user_id)[0]:
                return False
            cursor = conn.execute(
                """INSERT INTO eval_cases(id, user_id, query, expected_ids_json, note, source, created_at)
                   VALUES(?, ?, ?, ?, 'auto: подтверждённый feedback', 'feedback', ?)
                   ON CONFLICT(user_id, query) DO UPDATE SET
                     expected_ids_json=excluded.expected_ids_json,
                     created_at=excluded.created_at
                   WHERE eval_cases.source<>'manual'""",
                (new_id("eval"), user_id, clean_query, json.dumps(ids), utc_now()),
            )
        return cursor.rowcount > 0

    def list_eval_cases(self, user_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.execute(
            # Hand-curated cases first: mined ones grow without bound and would
            # otherwise push the gold set a human actually chose out of the window.
            "SELECT * FROM eval_cases WHERE user_id=? "
            "ORDER BY (source='manual') DESC, created_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        bounded = max(1, min(int(limit), 5000))
        conn = self.conn
        dependency_user_id, private_tokens = _eval_token_scope(self, conn, user_id)
        cases = []
        for row in rows:
            case = dict(row)
            visible, expected_ids = _eval_case_is_visible(
                conn,
                case,
                private_tokens,
                dependency_user_id,
            )
            if not visible:
                continue
            case.pop("expected_ids_json", None)
            case["expected_ids"] = expected_ids
            cases.append(case)
            if len(cases) >= bounded:
                break
        return cases

    def prune_eval_cases(self, user_id: str, *, cap: int = EVAL_MINED_CASE_CAP) -> dict[str, int]:
        """Drop mined cases that can never be satisfied, and cap how many are kept.

        ``source<>'manual'`` sits on the DELETE itself in BOTH branches rather than on
        the Python-side candidate list: a mistake in the health check, the cap or the
        subquery then costs an unpruned row, never a hand-curated case.
        """
        dead = [
            case_id for case_id in self.eval_case_health(user_id)["dead_case_ids"] if isinstance(case_id, str)
        ]
        deleted_dead = 0
        with self.transaction() as conn:
            dependency_user_id, private_tokens = _eval_token_scope(self, conn, user_id)
            for start in range(0, len(dead), 400):
                batch = dead[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                cursor = conn.execute(
                    "DELETE FROM eval_cases "  # nosec B608
                    f"WHERE user_id=? AND source<>'manual' AND id IN ({placeholders})",
                    (user_id, *batch),
                )
                deleted_dead += cursor.rowcount or 0
            mined_rows = conn.execute(
                """SELECT * FROM eval_cases
                    WHERE user_id=? AND source<>'manual'
                    ORDER BY created_at DESC, id DESC""",
                (user_id,),
            ).fetchall()
            visible_mined = [
                dict(row)
                for row in mined_rows
                if _eval_case_is_visible(
                    conn,
                    dict(row),
                    private_tokens,
                    dependency_user_id,
                )[0]
            ]
            keep = max(1, int(cap))
            over_cap_ids = [str(row["id"]) for row in visible_mined[keep:]]
            deleted_over_cap = 0
            for start in range(0, len(over_cap_ids), 400):
                batch = over_cap_ids[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                cursor = conn.execute(
                    f"""DELETE FROM eval_cases WHERE user_id=? AND source<>'manual'
                         AND id IN ({placeholders})""",  # nosec B608
                    (user_id, *batch),
                )
                deleted_over_cap += cursor.rowcount or 0
            kept_mined = max(0, len(visible_mined) - deleted_over_cap)
        return {
            "deleted_dead": deleted_dead,
            "deleted_over_cap": deleted_over_cap,
            "kept_mined": kept_mined,
        }

    def delete_eval_case(self, user_id: str, case_id: str) -> bool:
        with self.transaction() as conn:
            dependency_user_id, private_tokens = _eval_token_scope(self, conn, user_id)
            row = conn.execute(
                "SELECT * FROM eval_cases WHERE id=? AND user_id=?",
                (case_id, user_id),
            ).fetchone()
            if (
                row is None
                or not _eval_case_is_visible(
                    conn,
                    dict(row),
                    private_tokens,
                    dependency_user_id,
                )[0]
            ):
                return False
            cursor = conn.execute("DELETE FROM eval_cases WHERE id=? AND user_id=?", (case_id, user_id))
        return cursor.rowcount > 0

    def store_feedback(self, feedback: FeedbackItem) -> FeedbackItem:
        self.ensure_user(feedback.user_id)
        row = feedback.to_row()
        score = float(row["score"])
        if not math.isfinite(score):
            raise ValueError("feedback score must be finite")
        with self.transaction() as conn:
            dependency_user_id, private_tokens = _feedback_token_scope(
                self,
                conn,
                feedback.user_id,
            )
            if not _feedback_row_is_visible(conn, row, private_tokens, dependency_user_id):
                raise ValueError("Feedback depends on private knowledge")
            previous_state = conn.execute(
                """SELECT * FROM feedback_state
                   WHERE user_id=? AND target_type=? AND target_id=? AND feedback_type=?""",
                (
                    row["user_id"],
                    row["target_type"],
                    row["target_id"],
                    row["feedback_type"],
                ),
            ).fetchone()
            if previous_state is not None:
                previous_row = dict(previous_state)
                if not _feedback_row_is_visible(
                    conn,
                    previous_row,
                    private_tokens,
                    dependency_user_id,
                ):
                    raise ValueError("Feedback state depends on private knowledge")
            conn.execute(
                """INSERT INTO feedback(id, user_id, target_type, target_id, feedback_type,
                   score, comment, context_json, created_at)
                   VALUES(:id, :user_id, :target_type, :target_id, :feedback_type,
                   :score, :comment, :context_json, :created_at)""",
                row,
            )
            conn.execute(
                """INSERT INTO feedback_state(
                       user_id, target_type, target_id, feedback_type, score,
                       comment, context_json, feedback_id, updated_at
                   ) VALUES(:user_id, :target_type, :target_id, :feedback_type, :score,
                            :comment, :context_json, :id, :created_at)
                   ON CONFLICT(user_id, target_type, target_id, feedback_type) DO UPDATE SET
                     score=excluded.score,
                     comment=excluded.comment,
                     context_json=excluded.context_json,
                     feedback_id=excluded.feedback_id,
                     updated_at=excluded.updated_at""",
                row,
            )

            context = _json_load(row["context_json"], {})
            knowledge_ids = context.get("knowledge_object_ids") if isinstance(context, dict) else []
            current_ids = (
                list(dict.fromkeys(str(item) for item in knowledge_ids if str(item).strip()))
                if isinstance(knowledge_ids, list)
                else []
            )
            previous_context = _json_load(previous_state["context_json"], {}) if previous_state else {}
            previous_ids_value = (
                previous_context.get("knowledge_object_ids") if isinstance(previous_context, dict) else []
            )
            previous_ids = (
                list(dict.fromkeys(str(item) for item in previous_ids_value if str(item).strip()))
                if isinstance(previous_ids_value, list)
                else []
            )
            previous_score = float(previous_state["score"] or 0.0) if previous_state else 0.0

            # Undo the prior current-state attribution before applying the new
            # one. The append-only feedback table remains untouched.
            for knowledge_id in previous_ids:
                conn.execute(
                    """UPDATE knowledge_usage SET
                         positive_feedback_count=MAX(0, positive_feedback_count-?),
                         negative_feedback_count=MAX(0, negative_feedback_count-?),
                         updated_at=?
                       WHERE user_id=? AND knowledge_object_id=?""",
                    (
                        1 if previous_score > 0 else 0,
                        1 if previous_score < 0 else 0,
                        row["created_at"],
                        dependency_user_id,
                        knowledge_id,
                    ),
                )

            for knowledge_id in current_ids:
                owner = conn.execute(
                    "SELECT 1 FROM knowledge_objects feedback_usage_knowledge "
                    "WHERE id=? AND user_id=? AND "
                    f"{_not_private_knowledge_dependency('feedback_usage_knowledge')}",  # nosec B608
                    (knowledge_id, dependency_user_id),
                ).fetchone()
                if not owner:
                    continue
                positive = 1 if score > 0 else 0
                negative = 1 if score < 0 else 0
                conn.execute(
                    """INSERT INTO knowledge_usage(
                           user_id, knowledge_object_id, positive_feedback_count,
                           negative_feedback_count, last_feedback_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, knowledge_object_id) DO UPDATE SET
                         positive_feedback_count=knowledge_usage.positive_feedback_count+excluded.positive_feedback_count,
                         negative_feedback_count=knowledge_usage.negative_feedback_count+excluded.negative_feedback_count,
                         last_feedback_at=excluded.last_feedback_at,
                         updated_at=excluded.updated_at""",
                    (
                        dependency_user_id,
                        knowledge_id,
                        positive,
                        negative,
                        row["created_at"],
                        row["created_at"],
                    ),
                )
        return feedback

    def get_feedback_for_target(self, user_id: str, target_type: str, target_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            """SELECT * FROM feedback WHERE user_id=? AND target_type=? AND target_id=?
               ORDER BY created_at DESC""",
            (user_id, target_type, target_id),
        ).fetchall()
        conn = self.conn
        dependency_user_id, private_tokens = _feedback_token_scope(self, conn, user_id)
        visible: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if _feedback_row_is_visible(conn, item, private_tokens, dependency_user_id):
                visible.append(item)
        return visible

    @staticmethod
    def _feedback_state_filter(
        user_id: str,
        target_type: str | None,
        target_id: str | None,
        feedback_type: str | None,
    ) -> tuple[list[str], list[Any]]:
        """Built once, so the count and the listing cannot answer different questions."""
        clauses = ["user_id=?"]
        params: list[Any] = [user_id]
        if target_type:
            clauses.append("target_type=?")
            params.append(target_type)
        if target_id:
            clauses.append("target_id=?")
            params.append(target_id)
        if feedback_type:
            clauses.append("feedback_type=?")
            params.append(feedback_type)
        return clauses, params

    def count_feedback_state(
        self,
        user_id: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        feedback_type: str | None = None,
        negative_only: bool = False,
    ) -> int:
        """How many rows the same filters select — the number a tile should show.

        The dashboard used to draw `len(get_feedback_state(limit=5000))`, which on a
        busy account is the cap rather than a count. `negative_only` mirrors the
        python it replaces exactly: `score` is `REAL NOT NULL` with a CHECK between
        -1 and 1, so `score < 0` and `float(score or 0) < 0` select the same rows.
        """
        clauses, params = self._feedback_state_filter(user_id, target_type, target_id, feedback_type)
        if negative_only:
            clauses.append("score < 0")
        # ``clauses`` contains only fixed predicates; values remain bound.
        rows = self.execute(
            f"SELECT * FROM feedback_state WHERE {' AND '.join(clauses)}",  # nosec B608
            tuple(params),
        ).fetchall()
        conn = self.conn
        dependency_user_id, private_tokens = _feedback_token_scope(self, conn, user_id)
        return sum(
            1
            for row in rows
            if _feedback_row_is_visible(
                conn,
                dict(row),
                private_tokens,
                dependency_user_id,
            )
        )

    def get_feedback_state(
        self,
        user_id: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        feedback_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses, params = self._feedback_state_filter(user_id, target_type, target_id, feedback_type)
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT * FROM feedback_state WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, feedback_id DESC"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        conn = self.conn
        dependency_user_id, private_tokens = _feedback_token_scope(self, conn, user_id)
        bounded = max(1, min(int(limit), 5000))
        visible: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if not _feedback_row_is_visible(conn, item, private_tokens, dependency_user_id):
                continue
            visible.append(item)
            if len(visible) >= bounded:
                break
        return visible

    def get_feedback_stats(self, user_id: str, target_type: str | None = None) -> dict[str, Any]:
        if target_type:
            rows = self.execute(
                "SELECT * FROM feedback WHERE user_id=? AND target_type=?",
                (user_id, target_type),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM feedback WHERE user_id=?",
                (user_id,),
            ).fetchall()
        conn = self.conn
        dependency_user_id, private_tokens = _feedback_token_scope(self, conn, user_id)
        buckets: dict[str, list[float]] = {}
        for row in rows:
            item = dict(row)
            if _feedback_row_is_visible(conn, item, private_tokens, dependency_user_id):
                buckets.setdefault(str(item["feedback_type"]), []).append(float(item["score"]))
        return {
            feedback_type: {
                "avg_score": sum(scores) / len(scores),
                "count": len(scores),
            }
            for feedback_type, scores in buckets.items()
        }

    def get_current_feedback_stats(
        self,
        user_id: str,
        target_type: str | None = None,
    ) -> dict[str, Any]:
        """Summarize only each target's current feedback state.

        ``feedback`` is intentionally append-only for audit/history.  Product
        behavior, prompts, and ranking must use ``feedback_state`` so replacing
        a thumbs-up with a thumbs-down does not leave a misleading neutral
        average in the active feedback loop.
        """

        if target_type:
            rows = self.execute(
                "SELECT * FROM feedback_state WHERE user_id=? AND target_type=?",
                (user_id, target_type),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM feedback_state WHERE user_id=?",
                (user_id,),
            ).fetchall()
        conn = self.conn
        dependency_user_id, private_tokens = _feedback_token_scope(self, conn, user_id)
        buckets: dict[str, list[float]] = {}
        for row in rows:
            item = dict(row)
            if _feedback_row_is_visible(conn, item, private_tokens, dependency_user_id):
                buckets.setdefault(str(item["feedback_type"]), []).append(float(item["score"]))
        return {
            feedback_type: {
                "avg_score": sum(scores) / len(scores),
                "count": len(scores),
                "positive": sum(1 for score in scores if score > 0),
                "negative": sum(1 for score in scores if score < 0),
            }
            for feedback_type, scores in buckets.items()
        }
