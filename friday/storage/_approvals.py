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

from friday.storage._base import (
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


#: Заявки, у которых нет автора-человека: их заводит сама система.
#:
#: Откат оборвавшегося шага миссии просит не человек, а исполнительный орган.
#: Спрятать такую заявку от всех было бы хуже той дыры, ради которой вводилась
#: личная граница: решение по ней просто некому было бы принять. Поэтому именная
#: заявка личная, а безымянная служебная остаётся общей для арендатора — ровно
#: как было до правки.
#:
#: Пустая строка тоже здесь: так выглядят записи, заведённые до появления
#: личной границы, и внутренние пути, которые человека не знают.
SYSTEM_REQUESTERS = ("", "executive")


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
        ttl_sec: int = DEFAULT_APPROVAL_TTL_SEC,
    ) -> dict[str, Any]:
        """Завести заявку на подтверждение. Действие ещё НЕ выполнено.

        `policy_epoch` больше не принимается и не пишется: единственным его
        источником была константа `ActorContext.policy_epoch = 1`, никем не
        увеличиваемая. Столбец в таблице оставлен со своим умолчанием — сносить
        его отдельной миграцией дороже, чем он мешает, — но никакой защитой он не
        притворяется и в решениях не участвует. См. `claim_action_approval`.
        """
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
            "expires_at": _plus_seconds(ttl_sec),
            "created_at": now,
            "updated_at": now,
        }
        with self.transaction() as conn:
            # Та же просьба, уже висящая без ответа, не спрашивается второй раз.
            #
            # Человек отвечает на первый вопрос и видит второй; естественное
            # прочтение — «система не услышала», и он отвечает снова. Дальше
            # хуже: одна заявка исполняется, вторая доживает до истечения срока
            # в списке ожидающих, и список перестаёт значить «ждёт решения».
            #
            # Поиск стоит ВНУТРИ той же транзакции, что и вставка: «прочитать,
            # проверить, записать» двумя запросами — это гонка, и две
            # одновременные просьбы разошлись бы ровно между шагами. Тот же
            # довод, по которому решение человека сделано атомарным UPDATE.
            #
            # Три границы, каждая со своей причиной:
            #   * только `pending` — решённая заявка это история, и повторить
            #     просьбу после отказа человек вправе;
            #   * только тот же `payload_hash` — слияние A+B и A+C суть разные
            #     действия, показать одно и сделать другое недопустимо;
            #   * только тот же человек — в общем архиве двое могут просить одно
            #     и то же, и ответ одного не является ответом другого.
            #
            # `requested_by` в ключе — это и есть ЧЕЛОВЕК: в общем архиве
            # `user_id` один на всех, и заявку конкретного человека `_approval_row`
            # отбирает именно по этому столбцу (`requested_by IN (person_id, …)`).
            # Без него просьба одного участника глушила бы просьбу другого.
            waiting = conn.execute(
                """SELECT id FROM action_approvals
                    WHERE user_id=? AND tool=? AND payload_hash=? AND requested_by=?
                      AND status='pending' AND expires_at > ?
                    ORDER BY rowid DESC LIMIT 1""",
                (user_id, clean_tool, record["payload_hash"], record["requested_by"], now),
            ).fetchone()
            if waiting:
                # Идентификатор запоминается, а строка читается ПОСЛЕ выхода из
                # транзакции — как и на обычном пути ниже. Чтение через
                # `self.execute` внутри открытой транзакции берёт соединение
                # заново, и мешать эти два уровня незачем.
                found = str(waiting["id"])
            else:
                found = ""
                conn.execute(
                    """INSERT INTO action_approvals(id, user_id, tool, risk, payload_json, payload_hash,
                   summary, status, requested_by, conversation_id, mission_id,
                   expires_at, created_at, updated_at)
                   VALUES(:id, :user_id, :tool, :risk, :payload_json, :payload_hash, :summary,
                   :status, :requested_by, :conversation_id, :mission_id,
                   :expires_at, :created_at, :updated_at)""",
                    record,
                )
        return self._approval_row(found or str(record["id"]), user_id) or record

    def get_action_approval(
        self, approval_id: str, user_id: str | None = None, *, person_id: str = ""
    ) -> dict[str, Any] | None:
        return self._approval_row(approval_id, user_id, person_id=person_id)

    def _approval_row(
        self, approval_id: str, user_id: str | None, *, person_id: str = ""
    ) -> dict[str, Any] | None:
        """Заявка арендатора, а при `person_id` — только заявка ЭТОГО человека.

        В общем архиве `user_id` один на всех, и одного его мало: заявка это
        просьба конкретного человека разрешить действие ОТ ЕГО ИМЕНИ. Проверено
        на живом коде до правки — участник видел чужую заявку с описанием
        действия и мог её подтвердить; при наличии у него нужного права действие
        исполнилось бы под ним.

        Человек называется `requested_by`, куда теперь кладётся `own_id`. Раньше
        там лежал `identity_id` — СПОСОБ входа, идентификатор токена или связанной
        телеграм-личности, — и заявка разъезжалась бы по токенам, которыми
        человек входил.

        Пустой `person_id` оставлен намеренно: внутренние пути (исполнение,
        истечение срока, служебные проверки) работают по арендатору и человека не
        знают. Личную границу держат вызывающие снаружи — маршруты и мост.
        """
        if user_id and person_id:
            row = self.execute(
                "SELECT * FROM action_approvals WHERE id=? AND user_id=? "
                f"AND requested_by IN (?, {', '.join('?' * len(SYSTEM_REQUESTERS))})",  # nosec B608
                (approval_id, user_id, person_id, *SYSTEM_REQUESTERS),
            ).fetchone()
        elif user_id:
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
        person_id: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Заявки арендатора, а при `person_id` — только заявки этого человека."""
        bounded = max(1, min(int(limit), 200))
        where = ["user_id=?"]
        params: list[Any] = [user_id]
        if person_id:
            where.append(f"requested_by IN (?, {', '.join('?' * len(SYSTEM_REQUESTERS))})")
            params.extend([person_id, *SYSTEM_REQUESTERS])
        if status:
            where.append("status=?")
            params.append(status)
        params.extend([bounded, max(0, int(offset))])
        rows = self.execute(
            # Условия собираются только из констант выше; значения — параметрами.
            f"SELECT * FROM action_approvals WHERE {' AND '.join(where)} "  # nosec B608
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        return [
            self._approval_row(dict(row)["id"], user_id, person_id=person_id) or dict(row)
            for row in rows
        ]

    def count_action_approvals(
        self, user_id: str, *, status: str | None = None, person_id: str = ""
    ) -> int:
        """Число рядом со списком обязано считать ровно то, что список показывает."""
        where = ["user_id=?"]
        params: list[Any] = [user_id]
        if person_id:
            where.append(f"requested_by IN (?, {', '.join('?' * len(SYSTEM_REQUESTERS))})")
            params.extend([person_id, *SYSTEM_REQUESTERS])
        if status:
            where.append("status=?")
            params.append(status)
        row = self.execute(
            # Условия собираются только из констант выше; значения — параметрами.
            f"SELECT COUNT(*) AS count FROM action_approvals WHERE {' AND '.join(where)}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def decide_action_approval(
        self,
        approval_id: str,
        user_id: str,
        *,
        decision: str,
        decided_by: str,
        person_id: str = "",
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
        # Решает ТОЛЬКО тот, кто просил. Условие стоит в самом UPDATE, рядом с
        # `status='pending'`, и по той же причине: «прочитать, потом записать»
        # пропустило бы гонку двух нажатий. В общем архиве `user_id` один на всех,
        # и без этого условия участник подтверждал чужое действие — проверено на
        # живом коде до правки.
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE action_approvals
                   SET status=?, decided_by=?, decided_at=?, updated_at=?
                   WHERE id=? AND user_id=? AND (?='' OR requested_by IN (?, '', 'executive'))
                     AND status='pending' AND expires_at > ?""",
                (
                    "approved" if choice == "approve" else "rejected",
                    str(decided_by or ""),
                    now,
                    now,
                    approval_id,
                    user_id,
                    str(person_id or ""),
                    str(person_id or ""),
                    now,
                ),
            )
        if cursor.rowcount != 1:
            return None
        return self._approval_row(approval_id, user_id, person_id=person_id)

    def claim_action_approval(
        self,
        approval_id: str,
        user_id: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Забрать подтверждение под исполнение — ровно один раз.

        Возвращает запись, если заявление удалось, и None во всех остальных
        случаях: не подтверждено, уже заявлено, просрочено или изменились
        аргументы. Вызывающий обязан считать None отказом и НЕ выполнять действие.

        Проверка хэша здесь, а не только при создании: между решением человека и
        исполнением аргументы могли подменить.

        Сверки `policy_epoch` здесь БОЛЬШЕ НЕТ, и это удаление, а не упущение.
        Разбор Codex (§15) и прямая проверка: единственным источником значения был
        `ActorContext.policy_epoch = 1`, никем не увеличиваемый — ни выдачей
        права, ни отзывом, ни сменой пресета или состояния. Сравнивалась единица с
        единицей, а комментарий называл это защитой от смены политики. Механизм,
        которого нет, опаснее отсутствующего: на него ссылаются, и следующий
        человек считает, что защита есть.

        Настоящая повторная авторизация никуда не делась и живёт выше по стеку:
        `execute_approved` вызывает `authorization.require()` НЕПОСРЕДСТВЕННО
        перед побочным эффектом, поэтому снятое между решением и исполнением право
        действие останавливает.
        """
        record = self._approval_row(approval_id, user_id)
        if not record:
            return None
        if payload is not None and payload_digest(payload) != record.get("payload_hash"):
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

    def mark_action_approval_uncertain(
        self, approval_id: str, user_id: str, *, error: str = ""
    ) -> dict[str, Any] | None:
        """Исход неизвестен: побочный эффект мог случиться, а мог и нет.

        Отдельно от `failed` по одной причине: провал можно повторить, неизвестность
        повторять НЕЛЬЗЯ. Сюда попадает исполнение, оборванное по времени, — тот
        случай, когда обработчик мог довести дело до конца ровно тогда, когда его
        перестали ждать.
        """
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE action_approvals SET status='uncertain', error=?, updated_at=?
                   WHERE id=? AND user_id=? AND status='claimed'""",
                (str(error or "исход неизвестен")[:2000], now, approval_id, user_id),
            )
        if cursor.rowcount != 1:
            return None
        return self._approval_row(approval_id, user_id)

    def settle_uncertain_approval(
        self, approval_id: str, user_id: str, *, happened: bool, detail: str
    ) -> dict[str, Any] | None:
        """Закрыть неизвестный исход тем, что УВИДЕЛИ в состоянии.

        Спека v3 §5: «Uncertain side effects require reconciliation, not automatic
        replay». Повтора здесь нет и быть не может — есть наблюдение: постусловие
        инструмента читает факт из хранилища и отвечает, случилось действие или
        нет. Заявка после этого перестаёт висеть неизвестностью.

        `failed`, а не `pending`: заявка одноразовая и её решение уже потрачено.
        Узнать, что эффекта не было, — не то же самое, что получить право
        повторить: новое действие требует нового решения человека.

        Переход разрешён ТОЛЬКО из `uncertain`. Иначе сверка, запоздавшая на такт,
        переписала бы исход, который уже установлен точнее её.
        """
        now = utc_now()
        note = f"сверено наблюдением: {detail}"[:2000]
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE action_approvals
                   SET status=?, error=?, updated_at=?
                   WHERE id=? AND user_id=? AND status='uncertain'""",
                ("done" if happened else "failed", note, now, approval_id, user_id),
            )
        if cursor.rowcount != 1:
            return None
        return self._approval_row(approval_id, user_id)

    def list_uncertain_approvals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Неизвестные исходы ВСЕХ арендаторов — материал для сверки.

        Без арендатора в запросе: сверку ведёт фоновый работник, у которого своего
        человека нет, а ходить по арендаторам по одному он не может — их список и
        есть то, что он бы выяснял. Тот же приём, что у `reconcile_stale_claims`.
        """
        rows = self.execute(
            """SELECT * FROM action_approvals WHERE status='uncertain'
               ORDER BY updated_at ASC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

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
