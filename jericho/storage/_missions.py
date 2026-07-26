"""Storage methods for executive missions and their tasks.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.storage._base import (
    Any,
    Mission,
    MissionStatus,
    MissionTask,
    Sequence,
    StorageShared,
    enum_value,
    utc_now,
)


class MissionsMixin(StorageShared):
    def create_mission(self, mission: Mission) -> dict[str, Any]:
        """Persist a mission header; tasks are added separately via set_mission_plan."""
        self.ensure_user(mission.user_id)
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO missions(id, user_id, goal, title, status, origin, plan_summary,
                   created_by, error, task_count, done_count, metadata_json, version,
                   created_at, updated_at, started_at, completed_at)
                   VALUES(:id, :user_id, :goal, :title, :status, :origin, :plan_summary,
                   :created_by, :error, :task_count, :done_count, :metadata_json, :version,
                   :created_at, :updated_at, :started_at, :completed_at)""",
                mission.to_row(),
            )
        return self.get_mission(mission.id, mission.user_id) or {}

    def set_mission_plan(
        self,
        mission_id: str,
        user_id: str,
        tasks: list[MissionTask],
        *,
        plan_summary: str,
        status: MissionStatus | str,
    ) -> dict[str, Any] | None:
        """Attach a task plan to a mission atomically and record its size."""
        now = utc_now()
        with self.transaction() as conn:
            owned = conn.execute(
                "SELECT id FROM missions WHERE id=? AND user_id=?",
                (mission_id, user_id),
            ).fetchone()
            if owned is None:
                return None
            conn.execute(
                "DELETE FROM mission_tasks WHERE mission_id=? AND user_id=?",
                (mission_id, user_id),
            )
            for task in tasks:
                conn.execute(
                    """INSERT INTO mission_tasks(id, mission_id, user_id, seq, kind, title,
                       instruction, depends_on_json, status, result, inbox_id, tools_used_json,
                       error, created_at, updated_at, started_at, completed_at)
                       VALUES(:id, :mission_id, :user_id, :seq, :kind, :title, :instruction,
                       :depends_on_json, :status, :result, :inbox_id, :tools_used_json, :error,
                       :created_at, :updated_at, :started_at, :completed_at)""",
                    task.to_row(),
                )
            conn.execute(
                """UPDATE missions SET plan_summary=?, status=?, task_count=?, done_count=0,
                   updated_at=? WHERE id=? AND user_id=?""",
                (plan_summary, enum_value(status), len(tasks), now, mission_id, user_id),
            )
        return self.get_mission(mission_id, user_id)

    def get_mission(self, mission_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM missions WHERE id=? AND user_id=?",
                (mission_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_missions(
        self,
        user_id: str | None = None,
        *,
        status: MissionStatus | str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if status is not None:
            clauses.append("status=?")
            params.append(enum_value(status))
        elif statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(limit, 5000)), max(0, offset)])
        rows = self.execute(
            f"SELECT * FROM missions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_missions(self, user_id: str, *, statuses: Sequence[str] | None = None) -> int:
        params: list[Any] = [user_id]
        clause = "user_id=?"
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clause += f" AND status IN ({placeholders})"
            params.extend(statuses)
        row = self.execute(
            f"SELECT COUNT(*) AS n FROM missions WHERE {clause}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["n"]) if row else 0

    def get_mission_tasks(self, mission_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            rows = self.execute(
                "SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq",
                (mission_id,),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM mission_tasks WHERE mission_id=? AND user_id=? ORDER BY seq",
                (mission_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_mission_fields(self, mission_id: str, user_id: str, **fields: Any) -> bool:
        updates = {key: value for key, value in fields.items() if key in self._MISSION_UPDATABLE}
        if not updates:
            return False
        assignments = ", ".join(f"{column}=?" for column in updates)
        params = [*updates.values(), utc_now(), mission_id, user_id]
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE missions SET {assignments}, version=version+1, updated_at=? "  # nosec B608
                "WHERE id=? AND user_id=?",
                tuple(params),
            )
        return cursor.rowcount > 0

    def update_mission_task_fields(self, task_id: str, user_id: str, **fields: Any) -> bool:
        updates = {key: value for key, value in fields.items() if key in self._MISSION_TASK_UPDATABLE}
        if not updates:
            return False
        assignments = ", ".join(f"{column}=?" for column in updates)
        params = [*updates.values(), utc_now(), task_id, user_id]
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE mission_tasks SET {assignments}, updated_at=? "  # nosec B608
                "WHERE id=? AND user_id=?",
                tuple(params),
            )
        return cursor.rowcount > 0
