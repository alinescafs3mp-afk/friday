"""Importer organ — cold-start intake for the knowledge base.

An almost-empty base can never feel alive: proactivity, reflection, and the
profile all need a critical mass of the user's real life. This organ imports it
in bulk — an ICS calendar, a browser bookmarks export (Netscape HTML), an mbox
mail archive, or a single .eml message — while keeping the review gate
absolute: **every imported item lands as a pending Inbox suggestion, никогда не
канонизируется молча** (``force_review``). The admin Inbox already has bulk
confirm/ignore, so a 200-item import is a few clicks to triage, not 200.

Parsers are dependency-free: ICS is plain text; bookmarks use the bs4 already
shipped for web_surfer; mail uses stdlib ``mailbox``/``email`` and is parsed
from the ORIGINAL bytes so declared charsets (cp1251/koi8-r) decode correctly.
Attachments are ignored. Re-importing the same file is idempotent (per-item
``source_ref``: ics UID, bookmark URL, Message-ID), so exports can be re-run
safely as they grow.
"""

from __future__ import annotations

import hashlib
import logging
from html.parser import HTMLParser
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from jericho.organs import Organ
from jericho.permissions import CapabilityDefinition
from jericho.storage.models import AuditEntry, new_id
from jericho.workers._blocking import run_blocking

LOGGER = logging.getLogger(__name__)

IMPORT_CAPABILITY = CapabilityDefinition(
    "import.run",
    "Bulk-import external files (calendar, bookmarks, mail) into the review Inbox",
    "import",
    2,
    ("admin", "moderator", "user"),
    source="organ",
)

# A single import is bounded so one giant export cannot occupy the request
# worker for minutes; re-running the import continues idempotently.
MAX_ITEMS_PER_IMPORT = 500


def parse_ics(text: str) -> list[dict[str, Any]]:
    """Minimal VEVENT parser: SUMMARY + DTSTART(+DTEND/DESCRIPTION/LOCATION/UID).

    Handles RFC 5545 line folding (continuation lines start with a space/tab)
    and both all-day (``20260801``) and timestamped (``...T120000Z`` /
    ``;TZID=...``) DTSTART forms — the date part is kept, matching the
    date-granular ``entity_time`` model.
    """
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw_lines:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    def _date_of(value: str) -> str:
        digits = value.strip().replace("-", "")
        if len(digits) >= 8 and digits[:8].isdigit():
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        return ""

    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is not None and current.get("summary") and current.get("date"):
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        name = key.split(";", 1)[0].strip().upper()
        value = value.strip()
        if name == "SUMMARY":
            current["summary"] = value.replace("\\,", ",")[:200]
        elif name == "DTSTART":
            current["date"] = _date_of(value)
        elif name == "DTEND":
            current["end"] = _date_of(value)
        elif name == "DESCRIPTION":
            current["description"] = value.replace("\\n", "\n").replace("\\,", ",")[:2000]
        elif name == "LOCATION":
            current["location"] = value.replace("\\,", ",")[:200]
        elif name == "UID":
            current["uid"] = value[:120]
    return events


class _BookmarkParser(HTMLParser):
    """Streaming anchor reader that stops as soon as ``limit`` links are kept.

    Replaces a BeautifulSoup tree walk, which was quadratic in the number of
    anchors and ran on the event loop: measured on real-shaped Netscape exports,
    0.5 MB parsed in 0.5 s, 2 MB in 6.4 s, 4 MB in 31 s, 8.4 MB in 156 s — and
    the 500-item cap was applied only AFTER the whole tree existed, so it bounded
    nothing. A large browser export is an ordinary thing to import, not an
    attack. This reader is linear, keeps no tree, and stops at the cap, so the
    cost of an oversized file is now the cost of reading the prefix that fills it.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self._limit = max(1, int(limit))
        self.items: list[dict[str, Any]] = []
        self._seen: set[str] = set()
        self._href: str | None = None
        self._add_date = ""
        self._text: list[str] = []
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self.done:
            return
        # A nested <a> cannot happen in valid HTML; if one appears, the outer
        # anchor is abandoned rather than merged, exactly as a tree parse would.
        attributes = dict(attrs)
        href = str(attributes.get("href") or "").strip()
        self._href = href
        self._add_date = str(attributes.get("add_date") or "")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None or self.done:
            return
        href, self._href = self._href, None
        title = "".join(self._text).strip()
        self._text = []
        if not href.startswith(("http://", "https://")) or href in self._seen:
            return
        self._seen.add(href)
        self.items.append({"title": (title or href)[:200], "url": href, "add_date": self._add_date})
        if len(self.items) >= self._limit:
            self.done = True


def parse_bookmarks(html: str, *, limit: int = MAX_ITEMS_PER_IMPORT) -> list[dict[str, Any]]:
    """Netscape bookmarks export: every http(s) anchor with its title, up to ``limit``."""
    parser = _BookmarkParser(limit)
    # Fed in slices so the early stop is honoured mid-document instead of after
    # the whole string has been scanned.
    for start in range(0, len(html), 65_536):
        parser.feed(html[start : start + 65_536])
        if parser.done:
            break
    if not parser.done:
        parser.close()
    return parser.items


def _extract_email(msg: Any) -> dict[str, Any] | None:
    """Common fields from one parsed message; None when there is nothing to keep."""
    subject = str(msg.get("subject") or "").strip()
    sender = str(msg.get("from") or "").strip()
    date = str(msg.get("date") or "").strip()
    message_id = str(msg.get("message-id") or "").strip().strip("<>")
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            if part.get_content_type() == "text/html":
                from bs4 import BeautifulSoup

                content = BeautifulSoup(str(content), "html.parser").get_text(" ", strip=True)
            body = str(content).strip()
    except Exception:  # malformed MIME must not sink the whole archive
        body = ""
    if not subject and not body:
        return None
    return {
        "subject": subject or "(без темы)",
        "from": sender[:200],
        "date": date[:80],
        "message_id": message_id[:200],
        "body": body[:8000],
    }


def parse_eml(payload: bytes) -> list[dict[str, Any]]:
    """A single RFC 822 message; charset handling per its own headers."""
    import email
    import email.policy

    try:
        msg = email.message_from_bytes(payload, policy=email.policy.default)
    except Exception:
        return []
    item = _extract_email(msg)
    return [item] if item else []


def parse_mbox(payload: bytes, *, limit: int = MAX_ITEMS_PER_IMPORT) -> list[dict[str, Any]]:
    """An mbox archive via stdlib mailbox; skips individual malformed messages.

    ``mailbox.mbox`` is path-based, so the upload is staged to a temp file; the
    modern-policy factory gives properly decoded headers and bodies (RFC 2047
    subjects, declared charsets such as cp1251/koi8-r).

    Stops at ``limit`` messages. It used to extract every message of the archive
    — including running each HTML body through a full parse — and only then keep
    the first 500, so a large mail archive paid the whole cost for a bounded result.
    """
    import email
    import email.policy
    import mailbox
    import os
    import tempfile
    from typing import cast

    with tempfile.NamedTemporaryFile(suffix=".mbox", delete=False) as handle:
        handle.write(payload)
        path = handle.name
    items: list[dict[str, Any]] = []
    try:
        # The stdlib stubs insist on an mboxMessage factory, but mailbox.mbox
        # documents accepting any message factory; the modern EmailMessage
        # policy is exactly what we want here.
        box = mailbox.mbox(
            path,
            factory=cast(Any, lambda f: email.message_from_binary_file(f, policy=email.policy.default)),
        )
        try:
            for msg in box:
                try:
                    item = _extract_email(msg)
                except Exception:
                    continue
                if item:
                    items.append(item)
                    if len(items) >= max(1, int(limit)):
                        break
        finally:
            box.close()
    finally:
        os.unlink(path)
    return items


_EMAIL_HEADERS = ("from:", "subject:", "date:", "to:", "message-id:", "received:", "return-path:")


def detect_format(text: str, filename: str = "") -> str:
    head = text.lstrip("﻿ \t\r\n")[:400].upper()
    if head.startswith("BEGIN:VCALENDAR"):
        return "ics"
    # mbox: the very first line is an mbox "From " separator.
    if text.lstrip("﻿\r\n").startswith("From "):
        return "mbox"
    # eml: RFC 822 header lines open the file (checked before bookmarks so an
    # HTML email with anchors is not mistaken for a bookmarks export).
    header_block = text.lstrip("﻿ \t\r\n")[:4000]
    header_hits = sum(
        1
        for line in header_block.splitlines()[:40]
        if any(line.lower().startswith(known) for known in _EMAIL_HEADERS)
    )
    if header_hits >= 2:
        return "eml"
    if "NETSCAPE-BOOKMARK" in head or "<A HREF" in text[:20000].upper():
        return "bookmarks"
    name = filename.lower()
    if name.endswith(".mbox"):
        return "mbox"
    if name.endswith(".eml"):
        return "eml"
    return ""


def _event_payload(event: dict[str, Any]) -> tuple[str, str]:
    """(content, source_ref) for one calendar event."""
    summary = str(event["summary"])
    date = str(event["date"])
    lines = [f"Событие: {summary} — {date}."]
    if event.get("end") and event["end"] != date:
        lines[0] = f"Событие: {summary} — {date} по {event['end']}."
    if event.get("location"):
        lines.append(f"Место: {event['location']}")
    if event.get("description"):
        lines.append(str(event["description"]))
    uid = str(event.get("uid") or "")
    ref = f"ics:{uid}" if uid else f"ics:{date}:{hashlib.sha256(summary.encode()).hexdigest()[:12]}"
    return "\n".join(lines), ref


def _bookmark_payload(item: dict[str, Any]) -> tuple[str, str]:
    content = f"Закладка: {item['title']}\n{item['url']}"
    return content, f"bookmark:{item['url']}"[:500]


def _email_payload(item: dict[str, Any]) -> tuple[str, str]:
    lines = [f"Письмо: {item['subject']}"]
    meta = " — ".join(part for part in (item["from"], item["date"]) if part)
    if meta:
        lines.append(f"От: {meta}")
    if item["body"]:
        lines.append(item["body"])
    ref_key = (
        item["message_id"]
        or hashlib.sha256(f"{item['subject']}|{item['from']}|{item['date']}".encode()).hexdigest()[:16]
    )
    return "\n".join(lines), f"email:{ref_key}"[:500]


def _router() -> APIRouter:
    router = APIRouter(prefix="/api/import", tags=["import"])

    def _require(request: Request, capability: str):
        actor = request.state.actor
        request.app.state.auth_service.require(actor, capability)
        return actor

    @router.post("")
    async def run_import(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        actor = _require(request, "import.run")
        state = request.app.state
        payload = await file.read()
        if len(payload) > state.settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Import file is too large")
        text = payload.decode("utf-8", errors="replace")

        detected = detect_format(text, str(file.filename or ""))
        # Parsing is CPU-bound and runs OFF the event loop. It used to run on it,
        # and a bookmarks export of a few megabytes — an ordinary thing to import —
        # froze the whole process for tens of seconds: one uvicorn worker serves
        # the API, the Telegram bridge and every organ from the same loop, so
        # "slow import" meant "no chat, no worker, no /health".
        #
        # Each parser is also asked for ONE item more than the cap, so "there was
        # more" is still detectable without extracting the rest of the archive.
        probe_limit = MAX_ITEMS_PER_IMPORT + 1
        if detected == "ics":
            events = await run_blocking(parse_ics, text)
            parsed = [_event_payload(event) for event in events[:probe_limit]]
            kind_label = "calendar"
        elif detected == "bookmarks":
            bookmarks = await run_blocking(parse_bookmarks, text, limit=probe_limit)
            parsed = [_bookmark_payload(bookmark) for bookmark in bookmarks]
            kind_label = "bookmarks"
        elif detected == "mbox":
            # Mail is parsed from the ORIGINAL bytes: charsets are declared in
            # the message headers, not necessarily utf-8.
            messages = await run_blocking(parse_mbox, payload, limit=probe_limit)
            parsed = [_email_payload(message) for message in messages]
            kind_label = "email"
        elif detected == "eml":
            parsed = [_email_payload(m) for m in parse_eml(payload)]
            kind_label = "email"
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Unsupported file: expected an ICS calendar, a bookmarks HTML export, "
                    "an mbox mail archive, or a single .eml message"
                ),
            )
        # ``truncated`` is now "at least this many were left", not an exact count:
        # nothing past the cap is extracted, so nothing past it can be counted.
        truncated = max(0, len(parsed) - MAX_ITEMS_PER_IMPORT)
        parsed = parsed[:MAX_ITEMS_PER_IMPORT]

        from jericho.ingestion import IdempotencyConflictError

        queued = existing = conflicts = failed = 0
        for content, source_ref in parsed:
            try:
                result = await state.ingestion.ingest_text(
                    actor.user_id,
                    content,
                    source="import",
                    source_ref=source_ref,
                    force_knowledge=True,
                    # The review gate is absolute for bulk material: an import is
                    # an explicit action, but each ITEM still awaits the user.
                    force_review=True,
                    metadata={"import_kind": kind_label, "filename": str(file.filename or "")},
                )
            except IdempotencyConflictError:
                conflicts += 1
                continue
            except Exception:
                failed += 1
                LOGGER.warning("Import item failed (%s)", source_ref, exc_info=True)
                continue
            if result.get("idempotent_replay"):
                existing += 1
            else:
                queued += 1

        summary = {
            "kind": kind_label,
            "parsed": len(parsed),
            "queued_for_review": queued,
            "already_imported": existing,
            "conflicts": conflicts,
            "failed": failed,
            "truncated": truncated,
        }
        state.storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=actor.user_id,
                action="knowledge.import",
                target_type="import",
                target_id=str(file.filename or kind_label),
                after_json=summary,
                ip_address=getattr(request.state, "client_ip", ""),
                request_id=getattr(request.state, "request_id", ""),
            )
        )
        return summary

    return router


class ImporterOrgan(Organ):
    name = "importer"
    version = "1.0"

    def capabilities(self):
        return (IMPORT_CAPABILITY,)

    def router(self) -> APIRouter | None:
        return _router()
