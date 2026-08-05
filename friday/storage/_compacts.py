"""Ночная сводка о ПОВЕДЕНИИ системы за сутки — хранение.

Заказ владельца 2026-08-04, проект — `artifacts/compactor_design.md`. Цель:
раз в сутки видеть, как система себя вела, не читая переписку.

Главное свойство слоя: **в сводке нет ни одной строки, выведенной из переписки**,
и это свойство схемы, а не фильтра. Инцидент хранится перечислимым кодом,
человеческая формулировка рендерится при чтении. Тогда утечке некуда попасть.

Обоснование, почему обезличивание не поручено модели, — в проекте. Коротко: за
двое суток пять раз замерено, что промптовые ограничения не работают как
механизм, а корпус содержит фамилии, звания и названия подразделений. Промах был
бы тихим (сводку никто не читает, пока она не понадобится) и создавал бы новую
копию чувствительных данных в новом месте.

Три свойства, ради которых здесь отдельный слой:

1. **Личность, а не арендатор.** `principal` — человек. В общем архиве корпус
   общий, а переписка личная, и сводка о ней тоже. Тот же класс, что закрывался
   в правилах, поправках и заявках на подтверждение.
2. **Идемпотентность по построению.** `UNIQUE(principal, local_date)` плюс
   UPSERT: повторный прогон за те же сутки дубль создать не может — нечем.
3. **Различимые исходы.** Запись «начал» ставится ДО работы, поэтому пара
   «начал / нет конца» сама доказывает незавершённость, и следующий прогон
   знает, какой день переделать. Тот же приём, что у оборванных мутаторов.
"""

from __future__ import annotations

from friday.storage._base import (
    Any,
    StorageShared,
    json,
    new_id,
    utc_now,
)


class CompactsMixin(StorageShared):
    """Сводки за сутки: начать, закрыть, прочитать."""

    def begin_day_compact(self, principal: str, local_date: str) -> str:
        """Отметить начало сборки ДО того, как что-то посчитано.

        Порядок здесь — не стиль. Процесс, умерший посреди сборки, оставляет
        строку `started` без `finished_at`, и это единственное доказательство,
        что день трогали. Запись после работы такого следа не оставляет вовсе, и
        со стороны оборванная ночь неотличима от ночи без происшествий.

        Повторный вызов за те же сутки возвращает существующую запись и сбрасывает
        её в `started`: день пересобирается целиком, а не дополняется.
        """
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM day_compacts WHERE principal=? AND local_date=?",
                (principal, local_date),
            ).fetchone()
            if row:
                compact_id = str(row["id"])
                conn.execute(
                    """
                    UPDATE day_compacts
                       SET status='started', started_at=?, finished_at=NULL,
                           source_turns=0, counters_json='{}', incidents_json='[]',
                           patterns_json='[]', deleted_at=NULL
                     WHERE id=?
                    """,
                    (now, compact_id),
                )
                return compact_id
            compact_id = new_id("cmp")
            conn.execute(
                """
                INSERT INTO day_compacts
                    (id, principal, local_date, status, started_at)
                VALUES (?, ?, ?, 'started', ?)
                """,
                (compact_id, principal, local_date, now),
            )
            return compact_id

    def finish_day_compact(
        self,
        compact_id: str,
        *,
        source_turns: int,
        counters: dict[str, int],
        incidents: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
    ) -> None:
        """Закрыть сутки посчитанным.

        Ни `counters`, ни `incidents`, ни `patterns` не содержат текста из
        переписки: коды и числа. Проверяется тестом на составе полей, а не
        просмотром результата, — фильтровать тут нечего и не нужно.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE day_compacts
                   SET status='done', finished_at=?, source_turns=?,
                       counters_json=?, incidents_json=?, patterns_json=?
                 WHERE id=?
                """,
                (
                    utc_now(),
                    int(source_turns),
                    json.dumps(counters, ensure_ascii=False, sort_keys=True),
                    json.dumps(incidents, ensure_ascii=False),
                    json.dumps(patterns, ensure_ascii=False),
                    compact_id,
                ),
            )

    def abandon_day_compact(self, compact_id: str, *, reason: str = "") -> None:
        """Сборка оборвалась известным образом — так и записать.

        `uncertain`, а не `failed`: часть суток могла быть посчитана. Слово
        «неизвестно» на этом проекте значит ровно это и не должно обесцениваться
        применением к обычным отказам.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE day_compacts SET status='uncertain', finished_at=? WHERE id=?",
                (utc_now(), compact_id),
            )

    def get_day_compact(self, principal: str, local_date: str) -> dict[str, Any] | None:
        row = self.execute(
            """
            SELECT * FROM day_compacts
             WHERE principal=? AND local_date=? AND deleted_at IS NULL
            """,
            (principal, local_date),
        ).fetchone()
        return self._compact_row(row) if row else None

    def list_day_compacts(self, principal: str, *, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.execute(
            """
            SELECT * FROM day_compacts
             WHERE principal=? AND deleted_at IS NULL
             -- Хвост `id` не лишний, хотя `UNIQUE(principal, local_date)` и
             -- делает дату однозначной внутри одного человека: порядок страниц
             -- не должен зависеть от рассуждения о том, какие ограничения где
             -- стоят. Неоднозначная сортировка при листании пропускает и
             -- повторяет строки, и заметить это можно только на второй странице.
             ORDER BY local_date DESC, id DESC
             LIMIT ? OFFSET ?
            """,
            (principal, int(limit), int(offset)),
        ).fetchall()
        return [self._compact_row(row) for row in rows]

    def count_day_compacts(self, principal: str) -> int:
        """Считается отдельным COUNT, а не длиной страницы.

        `len(items)` выдаёт размер СВОЕГО запроса за свойство данных. Ошибка
        найдена трижды за одну ночь в разных подсистемах — здесь предупреждена
        сразу.
        """
        row = self.execute(
            "SELECT COUNT(*) AS n FROM day_compacts WHERE principal=? AND deleted_at IS NULL",
            (principal,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def days_needing_a_compact(self, principal: str, days: list[str]) -> list[str]:
        """Какие из перечисленных суток ещё не сведены или сведены не до конца.

        `started` без конца — это оборванная ночь, и её надо переделать. Молча
        считать такой день готовым значило бы потерять сутки наблюдений и не
        узнать об этом.
        """
        if not days:
            return []
        marks = ",".join("?" for _ in days)
        rows = self.execute(
            f"""
            SELECT local_date FROM day_compacts
             WHERE principal=? AND status='done' AND deleted_at IS NULL
               AND local_date IN ({marks})
            """,
            (principal, *days),
        ).fetchall()
        done = {str(row["local_date"]) for row in rows}
        return [day for day in days if day not in done]

    @staticmethod
    def _compact_row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "principal": str(row["principal"]),
            "local_date": str(row["local_date"]),
            "status": str(row["status"]),
            "source_turns": int(row["source_turns"] or 0),
            "counters": json.loads(str(row["counters_json"] or "{}")),
            "incidents": json.loads(str(row["incidents_json"] or "[]")),
            "patterns": json.loads(str(row["patterns_json"] or "[]")),
            "started_at": str(row["started_at"]),
            "finished_at": str(row["finished_at"] or ""),
        }
