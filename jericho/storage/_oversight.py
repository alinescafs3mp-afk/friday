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

from jericho.storage._base import StorageShared

# How much of a body one timeline row carries. The point of the preview is to make a
# row identifiable at a glance; the full text has its own endpoint, and one 205 KB
# document should not decide the size of a hundred-row response.
_PREVIEW_CHARS = 2000

# Days of the arrival histogram one response carries. The series is capped for
# response size, and the cap is REPORTED (`by_day_days`) rather than left to be
# inferred from the length: `arrivals` beside it is an exact count, so a silently
# clipped series makes the bars and the total disagree with nothing to show for it.
_DAY_BUCKETS = 366


def _preview(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:_PREVIEW_CHARS]


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
