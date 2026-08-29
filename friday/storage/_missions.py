"""Storage methods for executive missions and their tasks.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from friday.storage._base import (
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
                   budget_seconds, budget_tool_calls, budget_retries, deadline_at,
                   created_at, updated_at, started_at, completed_at)
                   VALUES(:id, :user_id, :goal, :title, :status, :origin, :plan_summary,
                   :created_by, :error, :task_count, :done_count, :metadata_json, :version,
                   :budget_seconds, :budget_tool_calls, :budget_retries, :deadline_at,
                   :created_at, :updated_at, :started_at, :completed_at)""",
                mission.to_row(),
            )
        return self.get_mission(mission.id, mission.user_id) or {}

    def create_mission_unless_twin(
        self,
        mission: Mission,
        *,
        statuses: Sequence[str],
        since: str,
    ) -> tuple[dict[str, Any], bool]:
        """Завести миссию, если такой же живой ещё нет. Второе значение — «завели ли».

        Замерено 2026-08-04: два одинаковых вызова `mission_propose` подряд давали
        две миссии и два набора шагов. При включённой полной автономии исполняются
        ОБЕ, а бегунок активных миссий общий на всех людей (потолок восемь), так
        что близнецы из одного разговора вытесняют работу остальных участников.

        Признак «завели» возвращается ЯВНО, и это главное здесь. Молча не вставить
        и вернуть чужую строку — как сделано у заявок — тут нельзя: вызывающая
        сторона возврат не читает, и пошла бы дальше со своим идентификатором.
        Получилась бы призрачная миссия: лишний поход планировщика в модель,
        запись в аудит о создании несуществующего, второе уведомление человеку
        (ключ очереди содержит идентификатор, у призрака он другой) и `None`
        вместо ответа модели.

        Поиск стоит ВНУТРИ той же транзакции, что и вставка: «прочитать, проверить,
        записать» двумя запросами — гонка. Строка читается ПОСЛЕ выхода из неё.

        Границы задаёт вызывающая сторона, и обе обязательны. `statuses` — только
        незавершённые: повторить законченную работу человек вправе, это прямая
        просьба «сделай ещё раз». `since` — срок: при выключенной автономии
        агентская миссия садится в `proposed` и висит там до решения человека, так
        что бессрочный ключ означал бы, что июльская просьба глушит августовскую.
        """
        self.ensure_user(mission.user_id)
        marks = ",".join("?" for _ in statuses) or "''"
        found = ""
        with self.transaction() as conn:
            row = conn.execute(
                f"""SELECT id FROM missions
                     WHERE user_id=? AND goal=? AND created_by=?
                       AND status IN ({marks}) AND created_at > ?
                     ORDER BY created_at ASC, id ASC LIMIT 1""",
                (mission.user_id, mission.goal, mission.created_by, *statuses, since),
            ).fetchone()
            if row:
                found = str(row["id"])
            else:
                conn.execute(
                    """INSERT INTO missions(id, user_id, goal, title, status, origin, plan_summary,
                       created_by, error, task_count, done_count, metadata_json, version,
                       budget_seconds, budget_tool_calls, budget_retries, deadline_at,
                       created_at, updated_at, started_at, completed_at)
                       VALUES(:id, :user_id, :goal, :title, :status, :origin, :plan_summary,
                       :created_by, :error, :task_count, :done_count, :metadata_json, :version,
                       :budget_seconds, :budget_tool_calls, :budget_retries, :deadline_at,
                       :created_at, :updated_at, :started_at, :completed_at)""",
                    mission.to_row(),
                )
        if found:
            return self.get_mission(found, mission.user_id) or {}, False
        return self.get_mission(mission.id, mission.user_id) or {}, True

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
        task_ids = [str(task.id) for task in tasks]
        task_seqs = [int(task.seq) for task in tasks]
        if any(task.mission_id != mission_id or task.user_id != user_id for task in tasks):
            raise ValueError("mission plan task ownership does not match its mission")
        if len(task_ids) != len(set(task_ids)) or len(task_seqs) != len(set(task_seqs)):
            raise ValueError("mission plan task identities must be unique")
        now = utc_now()
        with self.transaction() as conn:
            owned = conn.execute(
                """SELECT id FROM missions
                    WHERE id=? AND user_id=?
                      AND status NOT IN ('completed','failed','cancelled')""",
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

    def get_mission(
        self, mission_id: str, user_id: str | None = None, *, created_by: str | None = None
    ) -> dict[str, Any] | None:
        """Миссия арендатора, а при `created_by` — только своя.

        Чужая при этом отвечает тем же, чем несуществующая: разница ответов сама
        сообщила бы, что миссия есть и чья она.
        """
        query = "SELECT * FROM missions WHERE id=?"
        params: list[Any] = [mission_id]
        if user_id is not None:
            query += " AND user_id=?"
            params.append(user_id)
        if created_by is not None:
            query += " AND created_by=?"
            params.append(str(created_by or ""))
        row = self.execute(query, tuple(params)).fetchone()
        return dict(row) if row else None

    def list_missions(
        self,
        user_id: str | None = None,
        *,
        status: MissionStatus | str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        created_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Миссии арендатора, а при `created_by` — только ЭТОГО человека.

        В общем архиве `user_id` один на всех, и без второй границы список
        показывал цели ВСЕХ участников: цель миссии — свободный текст просьбы
        («собрать всё по личному делу такого-то»). Найдено ревью 2026-08-04.

        `None` означает «без разбора автора» и оставлен для владельца, надзора и
        фонового бегунка: первым это положено, третьему нужны все.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if created_by is not None:
            clauses.append("created_by=?")
            params.append(str(created_by or ""))
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
            f"SELECT * FROM missions {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",  # nosec B608
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
        if "status" in updates:
            updates["status"] = enum_value(updates["status"])
        assignments = ", ".join(f"{column}=?" for column in updates)
        params = [*updates.values(), utc_now(), mission_id, user_id]
        # Terminal mission states are immutable.  Every service-side check is a
        # snapshot and can race cancellation or another finalizer; the durable
        # writer is the only place where the first terminal decision can win.
        terminal_fence = (
            " AND status NOT IN ('completed','failed','cancelled')" if "status" in updates else ""
        )
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE missions SET {assignments}, version=version+1, updated_at=? "  # nosec B608
                f"WHERE id=? AND user_id=?{terminal_fence}",  # nosec B608
                tuple(params),
            )
        return cursor.rowcount > 0

    def update_mission_task_fields(
        self,
        task_id: str,
        user_id: str,
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_attempt: int | None = None,
        require_live_parent: bool = False,
        **fields: Any,
    ) -> bool:
        updates = {key: value for key, value in fields.items() if key in self._MISSION_TASK_UPDATABLE}
        if not updates:
            return False
        if expected_attempt is not None and (
            isinstance(expected_attempt, bool)
            or not isinstance(expected_attempt, int)
            or expected_attempt < 0
        ):
            return False
        if "status" in updates:
            updates["status"] = enum_value(updates["status"])
        assignments = ", ".join(f"{column}=?" for column in updates)
        params: list[Any] = [*updates.values(), utc_now(), task_id, user_id]
        requested_status = str(updates.get("status") or "")
        # A task which cancellation/finalization already closed must not be
        # reopened by a stale runner, reaper or reconciliation snapshot.
        fences = ["status NOT IN ('done','failed','skipped','compensated')"]
        expected_values = () if expected_statuses is None else expected_statuses
        if isinstance(expected_values, (str, bytes)):
            return False
        expected = tuple(dict.fromkeys(enum_value(value) for value in expected_values if enum_value(value)))
        if expected:
            placeholders = ",".join("?" for _ in expected)
            fences.append(f"status IN ({placeholders})")
            params.extend(expected)
        if expected_attempt is not None:
            fences.append("attempts=?")
            params.append(expected_attempt)
        if require_live_parent:
            fences.append(
                "EXISTS (SELECT 1 FROM missions m "
                "WHERE m.id=mission_tasks.mission_id "
                "AND m.user_id=mission_tasks.user_id "
                "AND m.status IN ('ready','running') "
                "AND (m.deadline_at IS NULL OR trim(m.deadline_at)='' "
                "OR (julianday(m.deadline_at) IS NOT NULL "
                "AND julianday(m.deadline_at)>julianday('now'))))"
            )
        if requested_status == "running":
            fences.append("status='pending'")
            fences.append(
                "EXISTS (SELECT 1 FROM missions m "
                "WHERE m.id=mission_tasks.mission_id "
                "AND m.user_id=mission_tasks.user_id "
                "AND m.status IN ('ready','running') "
                "AND (m.deadline_at IS NULL OR trim(m.deadline_at)='' "
                "OR (julianday(m.deadline_at) IS NOT NULL "
                "AND julianday(m.deadline_at)>julianday('now'))) "
                "AND (m.budget_seconds<=0 OR m.spent_seconds<m.budget_seconds) "
                "AND (m.budget_tool_calls<=0 OR m.spent_tool_calls<m.budget_tool_calls) "
                "AND (m.budget_retries<=0 OR m.spent_retries<m.budget_retries))"
            )
        elif requested_status == "pending":
            verified_effect_clear = (
                updates.get("side_effect") == 0
                and str(updates.get("checkpoint_json") or "").strip() == "{}"
                and str(updates.get("compensation") or "") == ""
            )
            if verified_effect_clear:
                # Only reconciliation may clear an unknown effect and reopen
                # it.  Bind the proof to the exact UNCERTAIN generation and a
                # still-runnable parent; malformed shorthand fails closed.
                if expected != ("uncertain",) or expected_attempt is None or not require_live_parent:
                    return False
                fences.append("status='uncertain'")
            else:
                # A stale reaper snapshot must not return a task to PENDING
                # after its live owner durably checkpointed an effect under the
                # same attempt.  The predicate is evaluated by the writer CAS.
                fences.extend(
                    (
                        "status='running'",
                        "side_effect=0",
                        "trim(COALESCE(checkpoint_json,'')) IN ('','{}')",
                    )
                )
        elif requested_status == "compensated":
            # A previously offered human-resolution action can arrive after a
            # verifier already proved no effect and reopened the task.  That
            # stale approval must not close a new execution attempt.
            fences.append("status='uncertain'")
        checkpoint_value = str(updates.get("checkpoint_json") or "").strip()
        guarded_effect = bool(updates.get("side_effect")) or checkpoint_value not in {"", "{}"}
        if guarded_effect:
            # The checkpoint is the linearization point before a possible
            # external effect.  Cancellation/expiry which won first rejects it.
            fences.append("status='running'")
            fences.append(
                "EXISTS (SELECT 1 FROM missions m "
                "WHERE m.id=mission_tasks.mission_id "
                "AND m.user_id=mission_tasks.user_id "
                "AND m.status IN ('ready','running') "
                "AND (m.deadline_at IS NULL OR trim(m.deadline_at)='' "
                "OR (julianday(m.deadline_at) IS NOT NULL "
                "AND julianday(m.deadline_at)>julianday('now'))))"
            )
        where_fence = "" if not fences else " AND " + " AND ".join(f"({item})" for item in fences)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE mission_tasks SET {assignments}, updated_at=? "  # nosec B608
                f"WHERE id=? AND user_id=?{where_fence}",  # nosec B608
                tuple(params),
            )
        return cursor.rowcount > 0

    def claim_mission_task(
        self,
        task_id: str,
        user_id: str,
        *,
        mission_id: str,
        expected_attempt: int,
    ) -> bool:
        """Atomically acquire one runnable task and account for its attempt.

        The claim and attempt counter share one SQLite writer transaction.  A
        duplicate worker, stale mission snapshot, cancellation, expired parent
        or exhausted durable budget therefore loses before model/tool work.
        """

        if (
            not isinstance(task_id, str)
            or not task_id
            or task_id != task_id.strip()
            or not isinstance(user_id, str)
            or not user_id
            or user_id != user_id.strip()
            or not isinstance(mission_id, str)
            or not mission_id
            or mission_id != mission_id.strip()
            or isinstance(expected_attempt, bool)
            or not isinstance(expected_attempt, int)
            or expected_attempt < 0
        ):
            return False
        now = utc_now()
        with self.transaction() as conn:
            candidate = conn.execute(
                """SELECT t.attempts, t.mission_id
                     FROM mission_tasks t
                     JOIN missions m ON m.id=t.mission_id AND m.user_id=t.user_id
                    WHERE t.id=? AND t.user_id=? AND t.mission_id=?
                      AND t.status='pending' AND t.attempts=?
                      AND m.status IN ('ready','running')
                      AND (m.deadline_at IS NULL OR trim(m.deadline_at)=''
                           OR (julianday(m.deadline_at) IS NOT NULL
                               AND julianday(m.deadline_at)>julianday('now')))
                      AND (m.budget_seconds<=0 OR m.spent_seconds<m.budget_seconds)
                      AND (m.budget_tool_calls<=0 OR m.spent_tool_calls<m.budget_tool_calls)
                      AND (m.budget_retries<=0 OR m.spent_retries<m.budget_retries)""",
                (task_id, user_id, mission_id, expected_attempt),
            ).fetchone()
            if candidate is None:
                return False
            prior_attempts = max(0, int(candidate["attempts"] or 0))
            claimed = conn.execute(
                """UPDATE mission_tasks
                      SET status='running', attempts=attempts+1,
                          started_at=?, updated_at=?
                    WHERE id=? AND user_id=? AND mission_id=?
                      AND status='pending' AND attempts=?""",
                (now, now, task_id, user_id, mission_id, expected_attempt),
            )
            if claimed.rowcount != 1:
                return False
            if prior_attempts > 0:
                conn.execute(
                    """UPDATE missions
                          SET spent_retries=spent_retries+1,
                              version=version+1, updated_at=?
                        WHERE id=? AND user_id=?
                          AND status IN ('ready','running')""",
                    (now, str(candidate["mission_id"]), user_id),
                )
        return True

    def cancel_mission_and_tasks(self, mission_id: str, user_id: str) -> bool:
        """Atomically stop a mission without erasing an already-unknown effect."""

        now = utc_now()
        with self.transaction() as conn:
            cancelled = conn.execute(
                """UPDATE missions
                      SET status='cancelled', completed_at=?,
                          version=version+1, updated_at=?
                    WHERE id=? AND user_id=?
                      AND status NOT IN ('completed','failed','cancelled')""",
                (now, now, mission_id, user_id),
            )
            if cancelled.rowcount != 1:
                return False
            conn.execute(
                """UPDATE mission_tasks
                      SET status=CASE
                              WHEN status='uncertain'
                                OR (status='running' AND side_effect<>0)
                              THEN 'uncertain' ELSE 'skipped' END,
                          error=CASE
                              WHEN status='running' AND side_effect<>0
                              THEN 'миссия остановлена: исход начатого побочного эффекта неизвестен'
                              ELSE error END,
                          completed_at=CASE
                              WHEN status='uncertain'
                                OR (status='running' AND side_effect<>0)
                              THEN completed_at ELSE ? END,
                          updated_at=?
                    WHERE mission_id=? AND user_id=?
                      AND status IN ('pending','running','uncertain')""",
                (now, now, mission_id, user_id),
            )
        return True

    def normalize_future_mission_task_start(self, task_id: str, user_id: str) -> bool:
        """Bound a skewed RUNNING timestamp without revoking its current owner."""

        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE mission_tasks
                      SET started_at=?, updated_at=?
                    WHERE id=? AND user_id=? AND status='running'
                      AND julianday(started_at)>julianday(?)""",
                (now, now, task_id, user_id, now),
            )
        return cursor.rowcount == 1

    def add_mission_spend(
        self,
        mission_id: str,
        user_id: str,
        *,
        seconds: float = 0.0,
        tool_calls: int = 0,
        retries: int = 0,
    ) -> None:
        """Записать израсходованное миссией — в базу, а не в память процесса.

        Спека v3 §5 требует, чтобы бюджеты держались ниже модели и переживали
        перезапуск. Счётчик, живущий в исполнителе, после падения начинается с
        нуля, и вторая попытка тратит весь бюджет заново.

        Прибавление идёт одним SQL-выражением (`spent = spent + ?`), а не
        чтением с последующей записью: два тика, читающие одно значение,
        потеряли бы один из расходов.
        """
        added_seconds = max(0, int(round(float(seconds))))
        added_calls = max(0, int(tool_calls))
        added_retries = max(0, int(retries))
        if not (added_seconds or added_calls or added_retries):
            return
        with self.transaction() as conn:
            conn.execute(
                """UPDATE missions
                   SET spent_seconds = spent_seconds + ?,
                       spent_tool_calls = spent_tool_calls + ?,
                       spent_retries = spent_retries + ?,
                       updated_at = ?
                   WHERE id = ? AND user_id = ?""",
                (added_seconds, added_calls, added_retries, utc_now(), mission_id, user_id),
            )

    def bump_mission_task_attempt(self, task_id: str, user_id: str) -> int:
        """Отметить ещё одну попытку шага и вернуть их общее число.

        Попытка считается ДО работы: если процесс умрёт в середине шага, счётчик
        уже увеличен. Иначе бесконечно падающий шаг выглядел бы после каждого
        перезапуска как первая попытка.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE mission_tasks SET attempts = attempts + 1, updated_at = ? WHERE id = ? AND user_id = ?",
                (utc_now(), task_id, user_id),
            )
        row = self.execute(
            "SELECT attempts FROM mission_tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        ).fetchone()
        return int(row["attempts"]) if row else 0
