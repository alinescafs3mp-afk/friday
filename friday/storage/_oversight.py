"""What one account has written and uploaded, and when.

Every other read in this layer answers «what does this account hold». Oversight asks
a different question — «what did this person DO, in order» — and the answer does not
live in one table: a note typed into Telegram and a file dropped into the web UI are
both `raw_objects` rows, but what makes them legible is the Knowledge Object that
came out, the Inbox item still waiting, and the filename in the metadata.

The spine is `raw_objects` because that is the one row every arrival creates. It
carries `received_at`, which is when the person actually did the thing — not when a
worker got round to enriching it.
"""

from __future__ import annotations

import json
from typing import Any

from friday.storage._base import StorageShared

# How much of a body one timeline row carries. The point of the preview is to make a
# row identifiable at a glance; the full text has its own endpoint, and one 205 KB
# document should not decide the size of a hundred-row response.
_PREVIEW_CHARS = 2000

# Days of the arrival histogram one response carries. The series is capped for
# response size, and the cap is REPORTED (`by_day_days`) rather than left to be
# inferred from the length: `arrivals` beside it is an exact count, so a silently
# clipped series makes the bars and the total disagree with nothing to show for it.
_DAY_BUCKETS = 366

# Which analyses `user_activity_analysis` knows how to run. An explicit vocabulary,
# not a free-form string: it is passed through to the agent tool, and a model given
# a free field invents plausible names and gets silence back.
ANALYSES = ("topics", "rhythm", "volume", "change")


def _preview(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:_PREVIEW_CHARS]


def _previous_window(since: str, until: str | None) -> tuple[str, str]:
    """The window of equal length immediately before this one.

    «Что изменилось за месяц» is a comparison, and the thing being compared has to
    be the same size — otherwise a 30-day window measured against 90 days of history
    reports a collapse in activity that is only arithmetic.
    """
    from datetime import datetime

    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    end = parse(until) if until else datetime.now(parse(since).tzinfo)
    start = parse(since)
    span = end - start
    return (start - span).isoformat(), start.isoformat()


class OversightMixin(StorageShared):
    def _arrival_window(
        self, user_id: str, since: str | None, until: str | None, *, alias: str = ""
    ) -> tuple[str, list[Any]]:
        """The window every oversight read shares, built once.

        Compared as strings because these are ISO-8601 stamps, which sort
        lexicographically. Bare `received_at`, not `COALESCE(received_at, created_at)`:
        the column is NOT NULL and has been since the table existed, so the COALESCE
        guarded nothing — while making the predicate an expression, which
        `idx_raw_objects_user_received` cannot serve. It cost a scan to defend against
        a row that cannot exist.
        """
        prefix = f"{alias}." if alias else ""
        clauses = [f"{prefix}user_id=?", f"{prefix}deleted_at IS NULL"]
        params: list[Any] = [user_id]
        if since:
            clauses.append(f"{prefix}received_at >= ?")
            params.append(since)
        if until:
            clauses.append(f"{prefix}received_at <= ?")
            params.append(until)
        return " AND ".join(clauses), params

    def _windowed_count(
        self, table: str, user_id: str, since: str | None, until: str | None, extra: str = ""
    ) -> int:
        """Count rows of `table` that happened inside the same window.

        These sit in one panel beside `arrivals`, which IS windowed. Left unwindowed
        they read as «this is what the person did in the last 7 days» while actually
        answering «ever» — the reader has no way to tell the two numbers apart.
        """
        clauses = ["user_id=?"]
        params: list[Any] = [user_id]
        if extra:
            clauses.append(extra)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        where = " AND ".join(clauses)
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {where}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def user_activity(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
        offset: int = 0,
        include_content: bool = True,
    ) -> list[dict[str, Any]]:
        """One account's arrivals, newest first, with what became of each.

        `include_content=False` is the metadata-only view: when, how, how much, what
        type and what became of it — with nothing a person wrote in it. What that
        drops is deliberate and slightly wider than «the body»: `title` and `filename`
        are written by the account holder and routinely say more than the text does
        («заявление на увольнение.docx»), and `source_ref` carries the import path for
        a file. `content_chars` and `size_bytes` stay: a size is not a content.
        """
        where, params = self._arrival_window(user_id, since, until, alias="r")
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        # ``where`` holds fixed predicates only; every value is bound.
        query = f"""SELECT r.id AS raw_object_id, r.source, r.source_ref, r.content_type,
                           r.metadata_json, r.raw_content,
                           r.received_at AS at,
                           k.id AS knowledge_object_id, k.title, k.knowledge_kind,
                           k.lifecycle_stage, k.importance,
                           i.id AS inbox_id, i.status AS inbox_status
                    FROM raw_objects r
                    LEFT JOIN knowledge_objects k
                      ON k.raw_object_id=r.id AND k.user_id=r.user_id AND k.deleted_at IS NULL
                    LEFT JOIN inbox i ON i.raw_object_id=r.id AND i.user_id=r.user_id
                    WHERE {where}
                    ORDER BY at DESC, r.id DESC
                    LIMIT ? OFFSET ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            try:
                metadata = json.loads(record.pop("metadata_json") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            body = str(record.pop("raw_content") or "")
            filename = str(metadata.get("filename") or "")
            item = {
                **record,
                "filename": filename,
                "mime_type": str(metadata.get("mime_type") or ""),
                "size_bytes": metadata.get("size_bytes"),
                # `source` says HOW it arrived; this says WHAT it was, which is the
                # distinction a person actually asks about — «писал» versus «загружал».
                "activity": "upload" if filename or record.get("content_type") == "file" else "wrote",
                "title": record.get("title") or filename or _preview(body)[:80],
                "content_chars": len(body),
                "preview": _preview(body),
            }
            if not include_content:
                # Overwritten rather than skipped: the caller gets the same keys either
                # way, so a surface that forgets to check the flag renders an empty
                # cell instead of the body. Building two different shapes is how the
                # redacted branch quietly grows a field the full one added later.
                for field in ("preview", "title", "filename", "source_ref"):
                    item[field] = ""
                item["redacted"] = True
            items.append(item)
        return items

    def user_activity_summary(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Counts and spans for one account, without carrying any of the content.

        Every number here answers the SAME question — «inside this window» — because
        they are read side by side as one panel. Three of them used to be all-time
        totals sitting next to a windowed `arrivals`, so picking «7 дней» moved one
        card and left three showing the account's whole history in the same type.
        """
        where, params = self._arrival_window(user_id, since, until)

        # ``where`` holds fixed predicates only; every value is bound.
        totals = self.execute(
            f"""SELECT COUNT(*) AS arrivals,
                       MIN(received_at) AS first_at,
                       MAX(received_at) AS last_at
                FROM raw_objects WHERE {where}""",  # nosec B608
            tuple(params),
        ).fetchone()

        by_source = [
            dict(row)
            for row in self.execute(
                f"""SELECT source, COUNT(*) AS count FROM raw_objects
                    WHERE {where} GROUP BY source ORDER BY count DESC""",  # nosec B608
                tuple(params),
            ).fetchall()
        ]
        by_day = [
            dict(row)
            for row in self.execute(
                f"""SELECT substr(received_at, 1, 10) AS day, COUNT(*) AS count
                    FROM raw_objects WHERE {where}
                    GROUP BY day ORDER BY day DESC LIMIT ?""",  # nosec B608
                (*params, _DAY_BUCKETS),
            ).fetchall()
        ]
        # How many days there ACTUALLY are, so a clipped series announces itself
        # instead of being read as the whole span.
        spanned = self.execute(
            f"SELECT COUNT(DISTINCT substr(received_at, 1, 10)) AS days FROM raw_objects WHERE {where}",  # nosec B608
            tuple(params),
        ).fetchone()
        by_type = [
            dict(row)
            for row in self.execute(
                f"""SELECT content_type, COUNT(*) AS count FROM raw_objects
                    WHERE {where} GROUP BY content_type ORDER BY count DESC""",  # nosec B608
                tuple(params),
            ).fetchall()
        ]

        day_count = int(spanned["days"] if spanned else 0)
        return {
            "user_id": user_id,
            "since": since,
            "until": until,
            "arrivals": int(totals["arrivals"] if totals else 0),
            "first_at": totals["first_at"] if totals else None,
            "last_at": totals["last_at"] if totals else None,
            "by_source": by_source,
            "by_content_type": by_type,
            "by_day": by_day,
            "by_day_days": day_count,
            "by_day_truncated": day_count > len(by_day),
            "knowledge_objects": self._windowed_count(
                "knowledge_objects", user_id, since, until, "deleted_at IS NULL"
            ),
            "pending_inbox": self._windowed_count("inbox", user_id, since, until, "status='pending'"),
            "messages": self._windowed_count("messages", user_id, since, until),
        }

    # ------------------------------------------------------------------
    # «Запрошенный анализ»: не лента и не сводка, а ответ на вопрос о человеке
    # ------------------------------------------------------------------

    def _topics(self, where: str, params: list[Any], *, top: int) -> list[dict[str, Any]]:
        """О чём этот человек пишет, по разметке, которая на его материале ЕСТЬ.

        Источник — `inbox.suggested_tags_json`, а не `knowledge_objects.tags_json`.
        Замерено на живой базе: теги Inbox стоят на 66 поступлениях из 66 (146
        различных значений), а теги знаний — на одном объекте из шести. Анализ,
        построенный на втором, был бы почти всегда пуст и читался бы как «человек
        ни о чём не пишет», что неотличимо от «нам нечем это измерить».

        Считается через `json_each` и складывается регистронезависимо той же
        функцией, что и остальная группировка по тегам в проекте.
        """
        rows = self.execute(
            f"""SELECT jericho_casefold(tag.value) AS topic, COUNT(*) AS count
                FROM raw_objects r
                JOIN inbox i ON i.raw_object_id=r.id AND i.user_id=r.user_id
                JOIN json_each(i.suggested_tags_json) AS tag
                WHERE {where} AND tag.value <> ''
                GROUP BY topic ORDER BY count DESC, topic ASC LIMIT ?""",  # nosec B608
            (*params, max(1, top)),
        ).fetchall()
        return [dict(row) for row in rows]

    def _kinds(self, where: str, params: list[Any]) -> list[dict[str, Any]]:
        """Что это за материал по типу, из метаданных поступления."""
        rows = self.execute(
            f"""SELECT COALESCE(json_extract(r.metadata_json, '$.knowledge_kind'), '') AS kind,
                       COUNT(*) AS count
                FROM raw_objects r WHERE {where}
                GROUP BY kind ORDER BY count DESC, kind ASC""",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def user_activity_analysis(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        analyses: tuple[str, ...] | list[str] | None = None,
        top: int = 10,
        include_content: bool = True,
    ) -> dict[str, Any]:
        """Не «что он сделал», а «что из этого следует» — по запрошенному разрезу.

        Лента отвечает на вопрос о событиях, сводка — о количествах. Оба
        предполагают, что смотрящий сам сложит из них картину. Это не работает на
        аккаунте с историей: 900 поступлений нельзя прочитать глазами.

        Каждый разрез — ОДИН SQL с агрегатом, поэтому число здесь не может
        оказаться длиной обрезанной выборки. Единственное место, где стоит
        `LIMIT` — список тем, и он ограничивает то, что ПОКАЗАНО, а не то, что
        посчитано: `topics_total` рядом говорит, сколько их всего.

        `include_content=False` убирает всё, что НАЗЫВАЕТ предмет: темы и виды
        материала. «Ритм» и «объём» остаются — когда человек работает и сколько
        выдаёт, это про деятельность, а не про её содержание. Граница проведена
        здесь, а не у вызывающих: список тем — сжатый пересказ написанного, и
        два вызывающих, решающих это порознь, разойдутся при первом новом разрезе.
        """
        wanted = tuple(analyses) if analyses else ANALYSES
        unknown = [name for name in wanted if name not in ANALYSES]
        if unknown:
            raise ValueError(f"Unknown analysis: {sorted(unknown)}. Valid: {list(ANALYSES)}")

        where, params = self._arrival_window(user_id, since, until, alias="r")
        result: dict[str, Any] = {
            "user_id": user_id,
            "since": since,
            "until": until,
            "analyses": list(wanted),
        }

        if "topics" in wanted:
            distinct = self.execute(
                f"""SELECT COUNT(DISTINCT jericho_casefold(tag.value)) AS total
                    FROM raw_objects r
                    JOIN inbox i ON i.raw_object_id=r.id AND i.user_id=r.user_id
                    JOIN json_each(i.suggested_tags_json) AS tag
                    WHERE {where} AND tag.value <> ''""",  # nosec B608
                tuple(params),
            ).fetchone()
            result["topics"] = self._topics(where, params, top=top) if include_content else []
            result["topics_total"] = int(distinct["total"] if distinct else 0) if include_content else 0
            result["kinds"] = self._kinds(where, params) if include_content else []
            result["topics_redacted"] = not include_content

        if "rhythm" in wanted:
            # Час берётся срезом строки, а не `strftime('%H')`: отметки пишутся в
            # UTC со смещением +00:00, оба дают одно и то же, но срез не может
            # молча сдвинуться, если однажды в базу попадёт местное время.
            result["by_hour"] = [
                dict(row)
                for row in self.execute(
                    f"""SELECT substr(r.received_at, 12, 2) AS hour, COUNT(*) AS count
                        FROM raw_objects r WHERE {where}
                        GROUP BY hour ORDER BY hour ASC""",  # nosec B608
                    tuple(params),
                ).fetchall()
            ]
            result["by_weekday"] = [
                dict(row)
                for row in self.execute(
                    f"""SELECT CAST(strftime('%w', r.received_at) AS INTEGER) AS weekday,
                               COUNT(*) AS count
                        FROM raw_objects r WHERE {where}
                        GROUP BY weekday ORDER BY weekday ASC""",  # nosec B608
                    tuple(params),
                ).fetchall()
            ]

        if "volume" in wanted:
            row = self.execute(
                f"""SELECT COUNT(*) AS arrivals,
                           COALESCE(SUM(LENGTH(r.raw_content)), 0) AS chars,
                           COALESCE(SUM(json_extract(r.metadata_json, '$.size_bytes')), 0) AS bytes,
                           COUNT(DISTINCT substr(r.received_at, 1, 10)) AS active_days
                    FROM raw_objects r WHERE {where}""",  # nosec B608
                tuple(params),
            ).fetchone()
            volume = {key: int(row[key] or 0) for key in ("arrivals", "chars", "bytes", "active_days")}
            volume["chars_per_active_day"] = (
                round(volume["chars"] / volume["active_days"]) if volume["active_days"] else 0
            )
            result["volume"] = volume

        if "change" in wanted:
            result["change"] = self._change(user_id, since, until, top=top, include_content=include_content)

        return result

    def _change(
        self,
        user_id: str,
        since: str | None,
        until: str | None,
        *,
        top: int,
        include_content: bool = True,
    ) -> dict[str, Any]:
        """Это окно против предыдущего такой же длины — одним запросом, не вычитанием.

        Две отдельные выборки и вычитание в питоне дали бы тот же ответ и
        разъехались бы при первой правке одной из них. `SUM(CASE …)` считает обе
        половины по одному и тому же предикату по построению.
        """
        if not since:
            # Без начала окна «изменилось» не с чем сравнивать. Отказ должен быть
            # явным: пустой разрез читался бы как «ничего не изменилось».
            return {"available": False, "reason": "since_required"}

        previous_since, previous_until = _previous_window(since, until)
        # ISO stamps compare as strings, so an open end is the string that sorts
        # after every timestamp this schema can hold.
        end = until or "9999"

        totals = self.execute(
            """SELECT SUM(CASE WHEN received_at >= ? THEN 1 ELSE 0 END) AS now,
                      SUM(CASE WHEN received_at < ? THEN 1 ELSE 0 END) AS before
               FROM raw_objects
               WHERE user_id=? AND deleted_at IS NULL AND received_at >= ? AND received_at <= ?""",
            (since, since, user_id, previous_since, end),
        ).fetchone()

        topics = (
            [
                dict(row)
                for row in self.execute(
                    """SELECT jericho_casefold(tag.value) AS topic,
                          SUM(CASE WHEN r.received_at >= ? THEN 1 ELSE 0 END) AS now,
                          SUM(CASE WHEN r.received_at < ? THEN 1 ELSE 0 END) AS before
                   FROM raw_objects r
                   JOIN inbox i ON i.raw_object_id=r.id AND i.user_id=r.user_id
                   JOIN json_each(i.suggested_tags_json) AS tag
                   WHERE r.user_id=? AND r.deleted_at IS NULL
                     AND r.received_at >= ? AND r.received_at <= ? AND tag.value <> ''
                   GROUP BY topic ORDER BY (now - before) DESC, topic ASC""",
                    (since, since, user_id, previous_since, end),
                ).fetchall()
            ]
            if include_content
            else []
        )

        return {
            "available": True,
            "previous_since": previous_since,
            "previous_until": previous_until,
            # Сколько поступлений — про деятельность, не про предмет: остаётся
            # обоим уровням.
            "arrivals_now": int((totals["now"] if totals else 0) or 0),
            "arrivals_before": int((totals["before"] if totals else 0) or 0),
            "topics": topics[:top],
            "topics_compared": len(topics),
            "topics_redacted": not include_content,
            # Появилось только сейчас — самое читаемое, что даёт эта пара окон.
            "new_topics": [row["topic"] for row in topics if row["before"] == 0 and row["now"] > 0][:top],
            "dropped_topics": [row["topic"] for row in topics if row["now"] == 0 and row["before"] > 0][:top],
        }
