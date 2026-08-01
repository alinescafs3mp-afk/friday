"""Storage methods for feedback, its rollup state and the eval gold set.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

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
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO eval_cases(id, user_id, query, expected_ids_json, note, source, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, query) DO UPDATE SET
                     expected_ids_json=excluded.expected_ids_json,
                     note=excluded.note, source=excluded.source""",
                (new_id("eval"), user_id, clean_query, json.dumps(ids), note[:500], source[:40], now),
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
            "ORDER BY (source='manual') DESC, created_at DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 5000))),
        ).fetchall()
        cases = []
        for row in rows:
            case = dict(row)
            case["expected_ids"] = _json_load(case.pop("expected_ids_json", "[]"), [])
            cases.append(case)
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
            for start in range(0, len(dead), 400):
                batch = dead[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                cursor = conn.execute(
                    "DELETE FROM eval_cases "  # nosec B608
                    f"WHERE user_id=? AND source<>'manual' AND id IN ({placeholders})",
                    (user_id, *batch),
                )
                deleted_dead += cursor.rowcount or 0
            cursor = conn.execute(
                """DELETE FROM eval_cases
                   WHERE user_id=? AND source<>'manual' AND id NOT IN (
                       SELECT id FROM eval_cases
                        WHERE user_id=? AND source<>'manual'
                        ORDER BY created_at DESC LIMIT ?)""",
                (user_id, user_id, max(1, int(cap))),
            )
            deleted_over_cap = cursor.rowcount or 0
        kept = self.execute(
            "SELECT COUNT(*) AS n FROM eval_cases WHERE user_id=? AND source<>'manual'",
            (user_id,),
        ).fetchone()
        return {
            "deleted_dead": deleted_dead,
            "deleted_over_cap": deleted_over_cap,
            "kept_mined": int(kept["n"]) if kept else 0,
        }

    def delete_eval_case(self, user_id: str, case_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM eval_cases WHERE id=? AND user_id=?", (case_id, user_id))
        return cursor.rowcount > 0

    def store_feedback(self, feedback: FeedbackItem) -> FeedbackItem:
        self.ensure_user(feedback.user_id)
        row = feedback.to_row()
        score = float(row["score"])
        if not math.isfinite(score):
            raise ValueError("feedback score must be finite")
        with self.transaction() as conn:
            previous_state = conn.execute(
                """SELECT score, context_json FROM feedback_state
                   WHERE user_id=? AND target_type=? AND target_id=? AND feedback_type=?""",
                (
                    row["user_id"],
                    row["target_type"],
                    row["target_id"],
                    row["feedback_type"],
                ),
            ).fetchone()
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
                        feedback.user_id,
                        knowledge_id,
                    ),
                )

            for knowledge_id in current_ids:
                owner = conn.execute(
                    "SELECT 1 FROM knowledge_objects WHERE id=? AND user_id=?",
                    (knowledge_id, feedback.user_id),
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
                        feedback.user_id,
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
        return [dict(row) for row in rows]

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
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM feedback_state WHERE {' AND '.join(clauses)}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

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
        params.append(max(1, min(int(limit), 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT * FROM feedback_state WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_feedback_stats(self, user_id: str, target_type: str | None = None) -> dict[str, Any]:
        if target_type:
            rows = self.execute(
                """SELECT feedback_type, AVG(score) AS avg_score, COUNT(*) AS count
                   FROM feedback WHERE user_id=? AND target_type=? GROUP BY feedback_type""",
                (user_id, target_type),
            ).fetchall()
        else:
            rows = self.execute(
                """SELECT feedback_type, AVG(score) AS avg_score, COUNT(*) AS count
                   FROM feedback WHERE user_id=? GROUP BY feedback_type""",
                (user_id,),
            ).fetchall()
        return {row["feedback_type"]: {"avg_score": row["avg_score"], "count": row["count"]} for row in rows}

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
                """SELECT feedback_type, AVG(score) AS avg_score, COUNT(*) AS count,
                          SUM(CASE WHEN score > 0 THEN 1 ELSE 0 END) AS positive,
                          SUM(CASE WHEN score < 0 THEN 1 ELSE 0 END) AS negative
                   FROM feedback_state WHERE user_id=? AND target_type=?
                   GROUP BY feedback_type""",
                (user_id, target_type),
            ).fetchall()
        else:
            rows = self.execute(
                """SELECT feedback_type, AVG(score) AS avg_score, COUNT(*) AS count,
                          SUM(CASE WHEN score > 0 THEN 1 ELSE 0 END) AS positive,
                          SUM(CASE WHEN score < 0 THEN 1 ELSE 0 END) AS negative
                   FROM feedback_state WHERE user_id=? GROUP BY feedback_type""",
                (user_id,),
            ).fetchall()
        return {
            str(row["feedback_type"]): {
                "avg_score": float(row["avg_score"] or 0.0),
                "count": int(row["count"] or 0),
                "positive": int(row["positive"] or 0),
                "negative": int(row["negative"] or 0),
            }
            for row in rows
        }
