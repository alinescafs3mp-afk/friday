"""Map already-durable mixed-journey identities through landed adapters.

This observer may read a duck-typed storage protocol or already-observed turn
facts.  It does not query sqlite inside the pure projection, hash file bytes,
mint web consumption from URLs, or register a mixed-journey organ.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from friday.orchestration.mixed_journey_archive_facts import (
    MixedJourneyArchiveFactsState,
    build_mixed_journey_archive_facts,
)
from friday.orchestration.mixed_journey_conversation_facts import (
    MixedJourneyConversationFactsState,
    build_mixed_journey_conversation_facts,
)
from friday.orchestration.mixed_journey_coverage import build_mixed_journey_coverage
from friday.orchestration.mixed_journey_file_facts import (
    MixedJourneyFileFactsState,
    build_mixed_journey_file_facts,
)
from friday.orchestration.mixed_journey_organs import (
    ORGAN_NAMES,
    MixedJourneyOrgansFactsV1,
    build_mixed_journey_organs,
)
from friday.orchestration.mixed_journey_progress import MixedStatusStage, build_mixed_operation_progress
from friday.orchestration.mixed_journey_store_projection import (
    MixedJourneyStoreProjectionV1,
    build_mixed_journey_store_projection,
)
from friday.orchestration.mixed_journey_table_facts import (
    MixedJourneyTableFactsState,
    build_mixed_journey_table_facts,
)
from friday.orchestration.mixed_journey_web_facts import (
    MixedJourneyWebFactsState,
    build_mixed_journey_web_facts,
)
from friday.orchestration.shared_operation_view import build_shared_operation_view
from friday.orchestration.web_research_consumption import WebResearchConsumptionV1

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MIME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}\Z")
_FILENAME_EXT_RE = re.compile(
    r"(?i)\.(?:pdf|zip|tar|tgz|gz|7z|rar|png|jpe?g|gif|webp|txt|csv|xlsx?|docx?|json|bin|mp3|mp4|wav)\Z"
)
_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tgz", ".tar.gz")
_ARCHIVE_MIME_TYPES = frozenset(
    {
        "application/zip",
        "application/x-zip-compressed",
        "application/x-tar",
        "application/gzip",
        "application/x-gtar",
    }
)
_TABLE_KINDS = frozenset({"table", "sheet"})
_BLOCKED = object()


def _opaque_id(value: object) -> str | None:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        return None
    if (
        "/" in value
        or "\\" in value
        or ".." in value
        or value.startswith("~")
        or _FILENAME_EXT_RE.search(value)
    ):
        return None
    return value


def _sha256(value: object) -> str | None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        return None
    return value


def _mime(value: object) -> str | None:
    if type(value) is not str or _MIME_RE.fullmatch(value) is None:
        return None
    return value


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _metadata(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if type(value) is not str or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _digest_from_row(row: Mapping[str, Any]) -> str | None:
    metadata = _metadata(row.get("metadata_json"))
    return _sha256(metadata.get("sha256")) or _sha256(row.get("content_hash")) or _sha256(row.get("sha256"))


def _call_storage(storage: object, name: str, *args: object) -> object:
    method = getattr(storage, name, None)
    if not callable(method):
        return _BLOCKED
    try:
        return method(*args)
    except Exception:
        return _BLOCKED


def _blocked_file() -> dict[str, object]:
    return {"file_id": "blocked", "sha256": "not-a-digest"}


def _first_present(values: Sequence[object], *, blocked: dict[str, object]) -> dict[str, object] | None:
    found: dict[str, object] | None = None
    for item in values:
        if item is _BLOCKED:
            return blocked
        if found is None and isinstance(item, dict):
            found = item
    return found


def _file_from_observed(item: object) -> object:
    raw = _mapping(item)
    if raw is None:
        return _BLOCKED
    file_id = _opaque_id(raw.get("file_id", raw.get("id")))
    digest = _sha256(raw.get("sha256", raw.get("digest", raw.get("file_sha256"))))
    if file_id is None:
        return _BLOCKED if raw.get("file_id", raw.get("id")) is not None else None
    if digest is None:
        return None
    fact: dict[str, object] = {"file_id": file_id, "sha256": digest}
    mime = _mime(raw.get("mime_type", raw.get("mime")))
    if mime is not None:
        fact["mime_type"] = mime
    return fact


def _archive_from_observed(item: object) -> object:
    raw = _mapping(item)
    if raw is None:
        return _BLOCKED
    archive_id = _opaque_id(raw.get("archive_id", raw.get("id")))
    digest = _sha256(raw.get("sha256", raw.get("digest")))
    members = raw.get("member_count")
    if archive_id is None:
        return _BLOCKED if raw.get("archive_id", raw.get("id")) is not None else None
    if digest is None or type(members) is not int:
        return None
    return {"archive_id": archive_id, "sha256": digest, "member_count": members}


def _table_from_observed(item: object) -> object:
    raw = _mapping(item)
    if raw is None:
        return _BLOCKED
    table_id = _opaque_id(raw.get("table_id", raw.get("id")))
    digest = _sha256(raw.get("sha256", raw.get("digest", raw.get("table_sha256"))))
    if table_id is None:
        return _BLOCKED if raw.get("table_id", raw.get("id")) is not None else None
    if digest is None:
        return None
    fact: dict[str, object] = {"table_id": table_id, "sha256": digest}
    for field in ("row_count", "column_count"):
        value = raw.get(field)
        if type(value) is int:
            fact[field] = value
    return fact


def _archive_from_store(storage: object, archive_id: object, user_id: str) -> object:
    opaque = _opaque_id(archive_id)
    if opaque is None:
        return _BLOCKED
    row = _call_storage(storage, "get_raw_object", opaque, user_id)
    if row is _BLOCKED:
        return _BLOCKED
    mapped = _mapping(row)
    if mapped is None:
        return None
    row_id = _opaque_id(mapped.get("id")) or opaque
    digest = _digest_from_row(mapped)
    members = mapped.get("member_count")
    if type(members) is not int:
        members = _metadata(mapped.get("metadata_json")).get("member_count")
    if digest is None or type(members) is not int:
        return None
    return {"archive_id": row_id, "sha256": digest, "member_count": members}


def _file_from_store(storage: object, file_id: object, user_id: str) -> object:
    opaque = _opaque_id(file_id)
    if opaque is None:
        return _BLOCKED
    row = _call_storage(storage, "get_raw_object", opaque, user_id)
    if row is _BLOCKED:
        return _BLOCKED
    mapped = _mapping(row)
    if mapped is None:
        return None
    row_id = _opaque_id(mapped.get("id")) or opaque
    digest = _digest_from_row(mapped)
    if digest is None:
        return None
    fact: dict[str, object] = {"file_id": row_id, "sha256": digest}
    mime = _mime(_metadata(mapped.get("metadata_json")).get("mime_type"))
    if mime is not None:
        fact["mime_type"] = mime
    return fact


def _table_from_store(storage: object, table_id: object, user_id: str) -> object:
    opaque = _opaque_id(table_id)
    if opaque is None:
        return _BLOCKED
    row = _call_storage(storage, "get_knowledge_object", opaque, user_id)
    if row is _BLOCKED:
        return _BLOCKED
    mapped = _mapping(row)
    if mapped is None:
        return None
    kind = str(mapped.get("knowledge_kind") or "").strip().casefold()
    if kind not in _TABLE_KINDS:
        return None
    row_id = _opaque_id(mapped.get("id")) or opaque
    digest = _digest_from_row(mapped)
    if digest is None:
        return None
    return {"table_id": row_id, "sha256": digest}


def _conversation_from_store(storage: object, conversation_id: object, user_id: str, turn: str) -> object:
    opaque = _opaque_id(conversation_id)
    if opaque is None:
        return _BLOCKED
    row = _call_storage(storage, "get_conversation", opaque, user_id)
    if row is _BLOCKED:
        return _BLOCKED
    mapped = _mapping(row)
    if mapped is None:
        return None
    row_id = _opaque_id(mapped.get("id")) or opaque
    return {"conversation_id": row_id, "authenticated_turn_id": turn}


def _archive_item(item: Mapping[str, Any]) -> bool:
    mime = str(item.get("mime_type") or item.get("mime") or "").strip().casefold()
    name = str(item.get("filename") or "").strip().casefold()
    return mime in _ARCHIVE_MIME_TYPES or name.endswith(_ARCHIVE_SUFFIXES)


def _flag_digest(organ: str, turn: str, extra: str) -> str:
    return hashlib.sha256(f"{organ}|{turn}|{extra}".encode()).hexdigest()


def _extract_response(response: Mapping[str, Any] | None) -> dict[str, Any]:
    if response is None:
        return {}
    context = response.get("context")
    context_map = context if isinstance(context, Mapping) else {}
    files: list[object] = []
    archives: list[object] = []
    raw_files = response.get("files")
    if isinstance(raw_files, list):
        for item in raw_files:
            mapped = _mapping(item)
            if mapped is None:
                continue
            if _archive_item(mapped) and type(mapped.get("member_count")) is int:
                archives.append(mapped)
            else:
                files.append(mapped)
    tables: list[object] = []
    for key in ("tables", "knowledge_objects"):
        listed = response.get(key)
        if not isinstance(listed, list):
            continue
        for item in listed:
            mapped = _mapping(item)
            if mapped is None:
                continue
            kind = str(mapped.get("knowledge_kind") or "").strip().casefold()
            if key == "knowledge_objects" and kind not in _TABLE_KINDS:
                continue
            tables.append(mapped)
    web = response.get("web_research_consumption", response.get("web_consumption"))
    return {
        "conversation_id": response.get("conversation_id"),
        "files": files,
        "archives": archives,
        "tables": tables,
        "web": web,
        "engineer": response.get("engineer") is True or context_map.get("engineer") is True,
        "coding": response.get("coding") is True or context_map.get("coding") is True,
        "engineer_current_advisories": response.get("engineer_current_advisories") is True
        or context_map.get("engineer_current_advisories") is True,
        "coding_current_docs": response.get("coding_current_docs") is True
        or context_map.get("coding_current_docs") is True,
        "status": response.get("status"),
        "execution": response.get("execution"),
        "restarted": response.get("restarted"),
        "revoked": response.get("revoked"),
        "publication_claimed": response.get("publication_claimed"),
    }


def observe_mixed_journey(
    projection_id: str,
    authenticated_turn_id: str,
    *,
    storage: object | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    file_ids: Sequence[str] = (),
    archive_ids: Sequence[str] = (),
    table_ids: Sequence[str] = (),
    files: Sequence[object] | None = None,
    archives: Sequence[object] | None = None,
    tables: Sequence[object] | None = None,
    web: object = None,
    engineer: bool = False,
    coding: bool = False,
    engineer_current_advisories: bool = False,
    coding_current_docs: bool = False,
    status: str | None = None,
    execution: str | None = None,
    restarted: bool | None = None,
    revoked: bool | None = None,
    publication_claimed: bool | None = None,
    elapsed_sec: float = 0,
    revision: int = 1,
    stage: MixedStatusStage | str | None = None,
    response: Mapping[str, Any] | None = None,
    effect_owners: object = None,
    publishers: object = None,
) -> MixedJourneyStoreProjectionV1:
    """Compose one store-backed mixed projection from durable identities."""

    extracted = _extract_response(response)
    if conversation_id is None:
        conversation_id = extracted.get("conversation_id")
    if files is None:
        files = extracted.get("files") or ()
    if archives is None:
        archives = extracted.get("archives") or ()
    if tables is None:
        tables = extracted.get("tables") or ()
    if web is None:
        web = extracted.get("web")
    engineer = engineer or bool(extracted.get("engineer"))
    coding = coding or bool(extracted.get("coding"))
    engineer_current_advisories = engineer_current_advisories or bool(
        extracted.get("engineer_current_advisories")
    )
    coding_current_docs = coding_current_docs or bool(extracted.get("coding_current_docs"))
    if status is None and extracted.get("status") is not None:
        status = extracted["status"] if type(extracted.get("status")) is str else status
    if execution is None and extracted.get("execution") is not None:
        execution = extracted["execution"] if type(extracted.get("execution")) is str else execution
    if restarted is None and type(extracted.get("restarted")) is bool:
        restarted = extracted["restarted"]
    if revoked is None and type(extracted.get("revoked")) is bool:
        revoked = extracted["revoked"]
    if publication_claimed is None and type(extracted.get("publication_claimed")) is bool:
        publication_claimed = extracted["publication_claimed"]
    engineer = engineer or engineer_current_advisories
    coding = coding or coding_current_docs

    observed_files = [_file_from_observed(item) for item in files or ()]
    observed_archives = [_archive_from_observed(item) for item in archives or ()]
    observed_tables = [_table_from_observed(item) for item in tables or ()]
    stored_files: list[object] = []
    stored_archives: list[object] = []
    stored_tables: list[object] = []
    stored_conversation: object = None
    if storage is not None:
        if type(user_id) is not str or not user_id:
            return build_mixed_journey_store_projection(
                projection_id,
                authenticated_turn_id,
                file=_blocked_file(),
            )
        stored_files = [_file_from_store(storage, item, user_id) for item in file_ids]
        stored_archives = [_archive_from_store(storage, item, user_id) for item in archive_ids]
        stored_tables = [_table_from_store(storage, item, user_id) for item in table_ids]
        if conversation_id is not None:
            stored_conversation = _conversation_from_store(
                storage, conversation_id, user_id, authenticated_turn_id
            )
    elif file_ids or archive_ids or table_ids:
        if any(_opaque_id(item) is None for item in (*file_ids, *archive_ids, *table_ids)):
            return build_mixed_journey_store_projection(
                projection_id,
                authenticated_turn_id,
                file=_blocked_file(),
            )

    file_fact = _first_present((*observed_files, *stored_files), blocked=_blocked_file())
    archive_fact = _first_present(
        (*observed_archives, *stored_archives),
        blocked={"archive_id": "blocked", "sha256": "not-a-digest", "member_count": 0},
    )
    table_fact = _first_present(
        (*observed_tables, *stored_tables),
        blocked={"table_id": "blocked", "sha256": "not-a-digest"},
    )
    conversation_fact: dict[str, object] | None
    if stored_conversation is _BLOCKED:
        conversation_fact = {"conversation_id": "blocked", "authenticated_turn_id": authenticated_turn_id}
    elif isinstance(stored_conversation, dict):
        conversation_fact = stored_conversation
    elif conversation_id is not None:
        opaque = _opaque_id(conversation_id)
        conversation_fact = (
            {"conversation_id": opaque, "authenticated_turn_id": authenticated_turn_id}
            if opaque is not None
            else {"conversation_id": "/blocked", "authenticated_turn_id": authenticated_turn_id}
        )
    else:
        conversation_fact = None
    web_fact: WebResearchConsumptionV1 | Mapping[str, object] | None
    if web is None or isinstance(web, (WebResearchConsumptionV1, Mapping)):
        web_fact = web
    else:
        web_fact = {"usability": "blocked"}

    file_value = build_mixed_journey_file_facts(file_fact)
    archive_value = build_mixed_journey_archive_facts(archive_fact)
    conversation_value = build_mixed_journey_conversation_facts(conversation_fact)
    web_value = build_mixed_journey_web_facts(web_fact)
    table_value = build_mixed_journey_table_facts(table_fact)
    if any(
        component.state.value == "blocked"
        for component in (file_value, archive_value, conversation_value, web_value, table_value)
    ):
        return build_mixed_journey_store_projection(
            projection_id,
            authenticated_turn_id,
            file=file_fact,
            archive=archive_fact,
            conversation=conversation_fact,
            web=web_fact,
            table=table_fact,
        )

    organs_facts = MixedJourneyOrgansFactsV1(
        file=file_value.state is MixedJourneyFileFactsState.PRESENT,
        archive=archive_value.state is MixedJourneyArchiveFactsState.PRESENT,
        conversation=conversation_value.state is MixedJourneyConversationFactsState.PRESENT,
        web=web_value.state is MixedJourneyWebFactsState.PRESENT,
        table=table_value.state is MixedJourneyTableFactsState.PRESENT,
        engineer=engineer,
        coding=coding,
    )
    if not any(
        (
            organs_facts.file,
            organs_facts.archive,
            organs_facts.conversation,
            organs_facts.web,
            organs_facts.table,
            organs_facts.engineer,
            organs_facts.coding,
        )
    ):
        return build_mixed_journey_store_projection(projection_id, authenticated_turn_id)
    organs = build_mixed_journey_organs(projection_id, authenticated_turn_id, facts=organs_facts)
    summaries: dict[str, str] = {}
    for name, value in (
        ("file", file_value),
        ("archive", archive_value),
        ("conversation", conversation_value),
        ("web", web_value),
        ("table", table_value),
    ):
        digest = value.summary_digest
        if value.state.value == "present" and type(digest) is str:
            summaries[name] = digest
    if engineer:
        summaries["engineer"] = _flag_digest(
            "engineer", authenticated_turn_id, f"advisories={int(engineer_current_advisories)}"
        )
    if coding:
        summaries["coding"] = _flag_digest(
            "coding", authenticated_turn_id, f"current_docs={int(coding_current_docs)}"
        )
    coverage = build_mixed_journey_coverage(projection_id, authenticated_turn_id, organs, summaries)
    mixed_stage = MixedStatusStage.COMPOSING_RESULT if stage is None else stage
    progress = build_mixed_operation_progress(
        mixed_stage,
        elapsed_sec,
        operation_id=projection_id,
        authenticated_turn_id=authenticated_turn_id,
        revision=max(1, int(revision)),
        organ_total=len(tuple(name for name in ORGAN_NAMES if organs.is_present(name))),
        gathered_organs=len(summaries),
    )
    shared = build_shared_operation_view(
        projection_id,
        authenticated_turn_id,
        progress,
        secondary={"present": False},
        pending_work_owner="primary",
        effect_owners=effect_owners,
        publishers=publishers,
    )
    return build_mixed_journey_store_projection(
        projection_id,
        authenticated_turn_id,
        file=file_fact,
        archive=archive_fact,
        conversation=conversation_fact,
        web=web_fact,
        table=table_fact,
        shared_operation_view=shared,
        organs=organs,
        coverage=coverage,
        status="unknown" if status is None else status,
        execution="unknown" if execution is None else execution,
        restarted=False if restarted is None else restarted,
        revoked=False if revoked is None else revoked,
        publication_claimed=False if publication_claimed is None else publication_claimed,
        effect_owners=effect_owners,
        publishers=publishers,
    )
