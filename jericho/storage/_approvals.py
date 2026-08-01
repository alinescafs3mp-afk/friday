"""Подтверждение опасного действия: durable, привязано к payload, одноразово.

Спека v3 §5. Право (`kg.merge`, `code.run`) отвечает на вопрос «этому актору
вообще можно». Подтверждение отвечает на другой: «человек видел ИМЕННО ЭТО
действие с ИМЕННО ЭТИМИ аргументами и согласился». Модель — недоверенный источник
предложений, и сегодня она может сама слить две сущности или объявить знание
устаревшим; между «модель предложила» и «система сделала» должен стоять человек.

Три свойства, ради которых здесь отдельный слой, а не флажок в таблице миссий:

1. **Привязка к payload.** Решение годится ровно для того набора аргументов,
   который показали человеку. Аргументы нормализуются в канонический JSON, от
   него берётся SHA-256, и заявление проверяет хэш заново. Подмена после решения
   («подтверди слияние A+B» → выполняется слияние A+C) не проходит.
2. **Одноразовость.** Заявление — атомарный UPDATE с проверкой затронутых строк,
   а не «прочитать, проверить, записать»: два одновременных исполнителя не могут
   оба уйти в исполнение по одному решению.
3. **Различимые исходы.** `claimed` (исполняется), `done`/`failed` (исход
   известен), `uncertain` (процесс умер между заявлением и результатом). Спека
   требует именно этого: неизвестный исход НЕЛЬЗЯ повторять автоматически —
   побочный эффект мог уже случиться, — он ждёт сверки человеком.
"""

from __future__ import annotations

from jericho.storage._base import (
    Any,
    StorageShared,
    datetime,
    hashlib,
    json,
    new_id,
    timedelta,
    utc_now,
)

# Сколько живёт неотвеченная заявка. Подтверждение — не бессрочная доверенность:
# согласие, данное на прошлой неделе, относилось к прошлой картине мира.
DEFAULT_APPROVAL_TTL_SEC = 24 * 3600
# Сколько «исполняется» может длиться, прежде чем это считается неизвестным
# исходом. Заявка живёт ровно один вызов инструмента; всё, что висит дольше,
# пережило смерть процесса.
CLAIM_STALE_SEC = 900

_TERMINAL = {"rejected", "expired", "done", "failed", "uncertain"}


def normalize_payload(payload: dict[str, Any] | None) -> str:
    """Канонический вид аргументов: один и тот же смысл — одна и та же строка.

    Порядок ключей и пробелы не должны менять хэш, иначе подтверждение
    развалилось бы на ровном месте; а вот значения — должны, иначе оно перестало
    бы что-либо гарантировать.
    """
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(payload: dict[str, Any] | None) -> str:
    return hashlib.sha256(normalize_payload(payload).encode("utf-8")).hexdigest()


def _plus_seconds(seconds: int) -> str:
    return (datetime.fromisoformat(utc_now()) + timedelta(seconds=max(1, int(seconds)))).isoformat()


class ApprovalsMixin(StorageShared):
    def create_action_approval(
        self,
        user_id: str,
        *,
        tool: str,
        payload: dict[str, Any] | None = None,
        summary: str = "",
        risk: str = "high",
        requested_by: str = "",
        conversation_id: str | None = None,
        mission_id: str | None = None,
        policy_epoch: str = "",
        ttl_sec: int = DEFAULT_APPROVAL_TTL_SEC,
    ) -> dict[str, Any]:
        """Завести заявку на подтверждение. Действие ещё НЕ выполнено."""
        if risk not in {"mutate", "high"}:
            raise ValueError("risk must be mutate or high")
        clean_tool = str(tool or "").strip()
        if not clean_tool:
            raise ValueError("tool is required")
        self.ensure_user(user_id)
        now = utc_now()
        record = {
            "id": new_id("apr"),
            "user_id": user_id,
            "tool": clean_tool,
            "risk": risk,
            "payload_json": normalize_payload(payload),
            "payload_hash": payload_digest(payload),
            "summary": str(summary or "").strip(),
            "status": "pending",
            "requested_by": str(requested_by or "").strip(),
            "conversation_id": conversation_id or None,
            "mission_id": mission_id or None,
            "policy_epoch": str(policy_epoch or ""),
            "expires_at": _plus_seconds(ttl_sec),
            "created_at": now,
            "updated_at": now,
        }
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO action_approvals(id, user_id, tool, risk, payload_json, payload_hash,
                   summary, status, requested_by, conversation_id, mission_id, policy_epoch,
                   expires_at, created_at, updated_at)
                   VALUES(:id, :user_id, :tool, :risk, :payload_json, :payload_hash, :summary,
                   :status, :requested_by, :conversation_id, :mission_id, :policy_epoch,
                   :expires_at, :created_at, :updated_at)""",
                record,
            )
        return self._approval_row(str(record["id"]), user_id) or record

    def get_action_approval(self, approval_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self._approval_row(approval_id, user_id)

    def _approval_row(self, approval_id: str, user_id: str | None) -> dict[str, Any] | None:
        if user_id:
            row = self.execute(
                "SELECT * FROM action_approvals WHERE id=? AND user_id=?", (approval_id, user_id)
            ).fetchone()
        else:
            row = self.execute("SELECT * FROM action_approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        with_payload = record.get("payload_json") or "{}"
        try:
            record["payload"] = json.loads(with_payload)
        except (TypeError, ValueError):
            record["payload"] = {}
        try:
            record["result"] = json.loads(record.get("result_json") or "{}")
        except (TypeError, ValueError):
            record["result"] = {}
        return record

    def list_action_approvals(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        if status:
            rows = self.execute(
                """SELECT * FROM action_approvals WHERE user_id=? AND status=?
                   ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
                (user_id, status, bounded, max(0, int(offset))),
            ).fetchall()
        else:
            rows = self.execute(
                """SELECT * FROM action_approvals WHERE user_id=?
                   ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
                (user_id, bounded, max(0, int(offset))),
            ).fetchall()
        return [self._approval_row(dict(row)["id"], user_id) or dict(row) for row in rows]

    def count_action_approvals(self, user_id: str, *, status: str | None = None) -> int:
        if status:
            row = self.execute(
                "SELECT COUNT(*) AS count FROM action_approvals WHERE user_id=? AND status=?",
                (user_id, status),
            ).fetchone()
        else:
            row = self.execute(
                "SELECT COUNT(*) AS count FROM action_approvals WHERE user_id=?", (user_id,)
            ).fetchone()
        return int(row["count"] if row else 0)

    def decide_action_approval(
        self,
        approval_id: str,
        user_id: str,
        *,
        decision: str,
        decided_by: str,
    ) -> dict[str, Any] | None:
        """Решение человека. Только из `pending` и только один раз.

        Условие `status='pending'` стоит в самом UPDATE, а не в предваряющем
        SELECT: два нажатия кнопки приходят как два запроса, и «прочитать, потом
        записать» пропустило бы оба.
        """
        choice = str(decision or "").strip().casefold()
        if choice not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE action_approvals
                   SET status=?, decided_by=?, decided_at=?, updated_at=?
                   WHERE id=? AND user_id=? AND status='pending' AND expires_at > ?""",
                (
                    "approved" if choice == "approve" else "rejected",
                    str(decided_by or ""),
                    now,
                    now,
                    approval_id,
                    user_id,
                    now,
                ),
            )
        if cursor.rowcount != 1:
            return None
        return self._approval_row(approval_id, user_id)

    def claim_action_approval(
        self,
        approval_id: str,
        user_id: str,
        *,
        payload: dict[str, Any] | None = None,
        policy_epoch: str | None = None,
    ) -> dict[str, Any] | None:
        """Забрать подтверждение под исполнение — ровно один раз.

        Возвращает запись, если заявление удалось, и None во всех остальных
        случаях: не подтверждено, уже заявлено, просрочено, изменились аргументы
        или сменилась политика прав. Вызывающий обязан считать None отказом и НЕ
        выполнять действие.

        Проверка хэша здесь, а не только при создании, — это и есть повторная
        авторизация непосредственно перед побочным эффектом: между решением
        человека и исполнением аргументы могли подменить.
        """
        record = self._approval_row(approval_id, user_id)
        if not record:
            return None
        if payload is not None and payload_digest(payload) != record.get("payload_hash"):
            return None
        if policy_epoch is not None and str(policy_epoch) != str(record.get("policy_epoch") or ""):
            return None
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE action_approvals SET status='claimed', claimed_at=?, updated_at=?
                   WHERE id=? AND user_id=? AND status='approved' AND expires_at > ?""",
                (now, now, approval_id, user_id, now),
            )
        if cursor.rowcount != 1:
            return None
        return self._approval_row(approval_id, user_id)

    def finish_action_approval(
        self,
        approval_id: str,
        user_id: str,
        *,
        success: bool,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        """Записать исход заявленного действия."""
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE action_approvals SET status=?, result_json=?, error=?, updated_at=?
                   WHERE id=? AND user_id=? AND status='claimed'""",
                (
                    "done" if success else "failed",
                    json.dumps(result or {}, ensure_ascii=False),
                    str(error or "")[:2000],
                    now,
                    approval_id,
                    user_id,
                ),
            )
        if cursor.rowcount != 1:
            return None
        return self._approval_row(approval_id, user_id)

    def expire_action_approvals(self) -> int:
        """Просроченные заявки и решения — в `expired`. Идёт по расписанию."""
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE action_approvals SET status='expired', updated_at=?
                   WHERE status IN ('pending', 'approved') AND expires_at <= ?""",
                (now, now),
            )
        return int(cursor.rowcount or 0)

    def reconcile_stale_claims(self, *, stale_after_sec: int = CLAIM_STALE_SEC) -> int:
        """Заявленное, но не завершённое — это НЕИЗВЕСТНЫЙ исход, а не повтор.

        Процесс умер между заявлением и записью результата: побочный эффект мог
        случиться, а мог и нет. Спека v3 §5 требует различать этот случай и не
        повторять его автоматически — такие записи уходят в `uncertain` и ждут
        сверки человеком.
        """
        cutoff = (datetime.fromisoformat(utc_now()) - timedelta(seconds=max(1, stale_after_sec))).isoformat()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE action_approvals
                   SET status='uncertain',
                       error=CASE WHEN error='' THEN 'исполнение прервано: исход неизвестен' ELSE error END,
                       updated_at=?
                   WHERE status='claimed' AND claimed_at <= ?""",
                (utc_now(), cutoff),
            )
        return int(cursor.rowcount or 0)

    @staticmethod
    def approval_is_terminal(record: dict[str, Any] | None) -> bool:
        return bool(record) and str((record or {}).get("status") or "") in _TERMINAL
