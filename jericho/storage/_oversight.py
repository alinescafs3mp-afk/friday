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


def _preview(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:_PREVIEW_CHARS]


class OversightMixin(StorageShared):
    def user_activity(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """One account's arrivals, newest first, with what became of each.

        `since`/`until` are ISO timestamps compared as strings, which is exactly right
        for the ISO-8601 stamps this schema stores: they sort lexicographically.
        """
        clauses = ["r.user_id=?", "r.deleted_at IS NULL"]
        params: list[Any] = [user_id]
        if since:
            clauses.append("COALESCE(r.received_at, r.created_at) >= ?")
            params.append(since)
        if until:
            clauses.append("COALESCE(r.received_at, r.created_at) <= ?")
            params.append(until)
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        # ``clauses`` holds fixed predicates only; every value is bound.
        query = f"""SELECT r.id AS raw_object_id, r.source, r.source_ref, r.content_type,
                           r.metadata_json, r.raw_content,
                           COALESCE(r.received_at, r.created_at) AS at,
                           k.id AS knowledge_object_id, k.title, k.knowledge_kind,
                           k.lifecycle_stage, k.importance,
                           i.id AS inbox_id, i.status AS inbox_status
                    FROM raw_objects r
                    LEFT JOIN knowledge_objects k
                      ON k.raw_object_id=r.id AND k.user_id=r.user_id AND k.deleted_at IS NULL
                    LEFT JOIN inbox i ON i.raw_object_id=r.id AND i.user_id=r.user_id
                    WHERE {" AND ".join(clauses)}
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
            items.append(
                {
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
            )
        return items

    def user_activity_summary(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Counts and spans for one account, without carrying any of the content."""
        clauses = ["user_id=?", "deleted_at IS NULL"]
        params: list[Any] = [user_id]
        if since:
            clauses.append("COALESCE(received_at, created_at) >= ?")
            params.append(since)
        if until:
            clauses.append("COALESCE(received_at, created_at) <= ?")
            params.append(until)
        where = " AND ".join(clauses)

        # ``where`` holds fixed predicates only; every value is bound.
        totals = self.execute(
            f"""SELECT COUNT(*) AS arrivals,
                       MIN(COALESCE(received_at, created_at)) AS first_at,
                       MAX(COALESCE(received_at, created_at)) AS last_at
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
                f"""SELECT substr(COALESCE(received_at, created_at), 1, 10) AS day,
                           COUNT(*) AS count
                    FROM raw_objects WHERE {where}
                    GROUP BY day ORDER BY day DESC LIMIT 90""",  # nosec B608
                tuple(params),
            ).fetchall()
        ]
        by_type = [
            dict(row)
            for row in self.execute(
                f"""SELECT content_type, COUNT(*) AS count FROM raw_objects
                    WHERE {where} GROUP BY content_type ORDER BY count DESC""",  # nosec B608
                tuple(params),
            ).fetchall()
        ]

        promoted = self.execute(
            "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        pending = self.execute(
            "SELECT COUNT(*) AS count FROM inbox WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone()
        messages = self.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE user_id=?",
            (user_id,),
        ).fetchone()

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
            "knowledge_objects": int(promoted["count"] if promoted else 0),
            "pending_inbox": int(pending["count"] if pending else 0),
            "messages": int(messages["count"] if messages else 0),
        }
