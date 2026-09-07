"""Process-local consume of one current-file + archive + public-web comparison.

Authority, durable storage and Telegram transport stay outside this module.
Tests inject prepared evidence.  Production prep is fail-closed when any lane
is missing.  The only final carrier is text.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.file_evidence_reader import PreparedFileEvidence
from friday.orchestration.engineer_result_carrier import EngineerResultCarrierKind
from friday.orchestration.mixed_file_archive_web_comparison import (
    MixedFileArchiveWebComparison,
    MixedFileArchiveWebComparisonError,
    compare_current_file_archive_with_web,
    mixed_file_archive_web_source_evidence_identity,
)
from friday.orchestration.mixed_file_archive_web_query import mixed_file_archive_web_turn_is_admitted
from friday.orchestration.transient_web_comparison import TransientWebComparisonEvidence
from friday.orchestration.web_provider_policy import WebProviderId
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionReason,
    WebResearchConsumptionState,
    WebResearchConsumptionV1,
)
from friday.organs.mixed_journey.observe import observe_mixed_journey
from friday.permissions import ActorContext

_DIGEST_RE_EMPTY = "0" * 64
_PLAN_SCHEMA = "friday.mixed-file-archive-web-consume-plan.v1"
_UNAVAILABLE = "Сравнение текущего файла, архивной ревизии и веба недоступно: доказательства не подготовлены."
_CANCELLED = "Сравнение текущего файла, архива и веба отменено по вашему запросу."
_RESTART_UNAVAILABLE = (
    "Сравнение нельзя безопасно продолжить после перезапуска: веб-источники не повторяются."
)
_NOT_ADMITTED = "Сравнение текущего файла, архивной ревизии и веба не принято."
_FAILED = "Сравнение текущего файла, архивной ревизии и веба не выполнено."


class MixedJourneyConsumeState(StrEnum):
    ADMITTED = "admitted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PUBLICATION_PREPARED = "publication_prepared"
    PUBLICATION_COMMITTED = "publication_committed"
    SEND_KNOWN = "send_known"
    SEND_UNKNOWN = "send_unknown"


class MixedJourneyConsumeError(ValueError):
    """Consume identity or transition is outside the closed ledger."""


@dataclass(frozen=True, slots=True)
class MixedJourneyConsumeKey:
    projection_id: str
    authenticated_turn_id: str
    source_identity_sha256: str

    def __post_init__(self) -> None:
        for label, value in (
            ("projection_id", self.projection_id),
            ("authenticated_turn_id", self.authenticated_turn_id),
            ("source_identity_sha256", self.source_identity_sha256),
        ):
            if type(value) is not str or not value:
                raise MixedJourneyConsumeError(f"{label} is invalid")
        if len(self.source_identity_sha256) != 64:
            raise MixedJourneyConsumeError("source_identity_sha256 is invalid")


@dataclass
class MixedJourneyConsumeRecord:
    key: MixedJourneyConsumeKey
    state: MixedJourneyConsumeState
    web_started: bool = False
    reply: dict[str, Any] | None = None


class MixedJourneyConsumeLedger:
    """Process-private identity ledger.  Duplicate identities are idempotent."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str, str], MixedJourneyConsumeRecord] = {}

    def _token(self, key: MixedJourneyConsumeKey) -> tuple[str, str, str]:
        return (key.projection_id, key.authenticated_turn_id, key.source_identity_sha256)

    def get(self, key: MixedJourneyConsumeKey) -> MixedJourneyConsumeRecord | None:
        with self._lock:
            record = self._records.get(self._token(key))
            return None if record is None else record

    def admit(self, key: MixedJourneyConsumeKey) -> MixedJourneyConsumeRecord:
        with self._lock:
            token = self._token(key)
            existing = self._records.get(token)
            if existing is not None:
                return existing
            record = MixedJourneyConsumeRecord(key=key, state=MixedJourneyConsumeState.ADMITTED)
            self._records[token] = record
            return record

    def mark(
        self,
        key: MixedJourneyConsumeKey,
        state: MixedJourneyConsumeState,
        *,
        reply: dict[str, Any] | None = None,
        web_started: bool | None = None,
    ) -> MixedJourneyConsumeRecord:
        with self._lock:
            token = self._token(key)
            record = self._records.get(token)
            if record is None:
                raise MixedJourneyConsumeError("consume identity is not admitted")
            if (
                record.state is MixedJourneyConsumeState.CANCELLED
                and state is not MixedJourneyConsumeState.CANCELLED
            ):
                return record
            record.state = state
            if reply is not None:
                record.reply = reply
            if web_started is not None:
                record.web_started = web_started
            return record

    def cancel(self, key: MixedJourneyConsumeKey) -> MixedJourneyConsumeRecord:
        with self._lock:
            token = self._token(key)
            record = self._records.get(token)
            if record is None:
                record = MixedJourneyConsumeRecord(key=key, state=MixedJourneyConsumeState.CANCELLED)
                self._records[token] = record
                return record
            if record.state in {
                MixedJourneyConsumeState.SEND_KNOWN,
                MixedJourneyConsumeState.SEND_UNKNOWN,
                MixedJourneyConsumeState.PUBLICATION_COMMITTED,
            }:
                return record
            record.state = MixedJourneyConsumeState.CANCELLED
            return record


_PROCESS_LEDGER = MixedJourneyConsumeLedger()


def _chat_dict(
    *,
    message: str,
    conversation_id: str = "",
    message_id: str | None = None,
    citations: list[dict[str, str]] | None = None,
    web_sources: list[dict[str, str]] | None = None,
    web_evidence_status: str = "none",
    context: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    knowledge_objects: list[dict[str, Any]] | None = None,
    web_research_consumption: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "message": message,
        "message_format": "plain",
        "verified": False,
        "citations": citations or [],
        "tools_used": [],
        "files": files or [],
        "voice": None,
        "web_evidence_status": web_evidence_status,
        "web_evidence_scope": "none" if web_evidence_status == "none" else "open_search",
        "web_sources": web_sources or [],
        "attachment_context_available": False,
        "context": context or {},
    }
    if knowledge_objects is not None:
        payload["knowledge_objects"] = knowledge_objects
    if web_research_consumption is not None:
        payload["web_research_consumption"] = web_research_consumption
    return payload


def _plan_sha256(message: str, source_identity: str) -> str:
    material = f"{_PLAN_SCHEMA}\0{message}\0{source_identity}".encode()
    return hashlib.sha256(material).hexdigest()


def _web_consumption(
    turn_id: str,
    source_count: int,
    selected_provider_id: str | None,
) -> WebResearchConsumptionV1:
    provider: str | None = None
    if type(selected_provider_id) is str:
        try:
            provider = WebProviderId(selected_provider_id.strip().casefold()).value
        except ValueError:
            provider = None
    usable = source_count > 0 and provider is not None
    return WebResearchConsumptionV1(
        "mixed.file.archive.web",
        turn_id,
        WebResearchConsumptionState.CONSUMABLE if usable else WebResearchConsumptionState.UNAVAILABLE,
        provider if usable else None,
        source_count if usable else 0,
        WebResearchConsumptionReason.PRIMARY_SOURCES
        if usable
        else WebResearchConsumptionReason.NO_ADMITTED_SOURCES,
    )


def _observe_identity(
    *,
    projection_id: str,
    turn_id: str,
    prepared_file: PreparedFileEvidence,
    prepared_archive: PreparedFileEvidence,
    web_consumption: WebResearchConsumptionV1,
    answer_sha256: str,
) -> Any:
    file_id = prepared_file.raw_ids[0]
    archive_id = prepared_archive.raw_ids[0]
    file_digest = prepared_file.snapshot_tokens[0].source.identity_sha256
    archive_digest = prepared_archive.snapshot_tokens[0].source.identity_sha256
    table_id = f"tbl_{answer_sha256[:16]}"
    return observe_mixed_journey(
        projection_id,
        turn_id,
        files=({"id": file_id, "sha256": file_digest},),
        archives=({"id": archive_id, "sha256": archive_digest, "member_count": 1},),
        tables=({"id": table_id, "sha256": answer_sha256},),
        web=web_consumption,
    )


def _publish(
    ledger: MixedJourneyConsumeLedger,
    key: MixedJourneyConsumeKey,
    reply: dict[str, Any],
    publisher: Callable[[dict[str, Any]], bool] | None,
) -> dict[str, Any]:
    existing = ledger.get(key)
    if existing is not None and existing.state in {
        MixedJourneyConsumeState.SEND_KNOWN,
        MixedJourneyConsumeState.SEND_UNKNOWN,
    }:
        return existing.reply or reply
    ledger.mark(key, MixedJourneyConsumeState.PUBLICATION_PREPARED, reply=reply)
    ledger.mark(key, MixedJourneyConsumeState.PUBLICATION_COMMITTED, reply=reply)
    if publisher is None:
        ledger.mark(key, MixedJourneyConsumeState.SEND_KNOWN, reply=reply)
        return reply
    try:
        known = bool(publisher(reply))
    except Exception:
        known = False
    ledger.mark(
        key,
        MixedJourneyConsumeState.SEND_KNOWN if known else MixedJourneyConsumeState.SEND_UNKNOWN,
        reply=reply,
    )
    return reply


def _persist(
    storage: Any,
    *,
    person_id: str,
    conversation_id: str | None,
    message: str,
    text: str,
    metadata: dict[str, Any],
) -> tuple[str, str | None]:
    if storage is None:
        return conversation_id or "", None
    persisted_id = conversation_id
    conversation = storage.get_conversation(conversation_id, person_id) if conversation_id else None
    if conversation is None:
        created = storage.create_conversation(person_id, title=(message or "Сравнение")[:80] or "Сравнение")
        persisted_id = str(created["id"])
    else:
        persisted_id = str(conversation["id"])
    storage.store_message(
        persisted_id,
        person_id,
        "user",
        message or "",
        metadata={"interaction_mode": "mixed", "had_attachments": True},
    )
    assistant = storage.store_message(
        persisted_id,
        person_id,
        "assistant",
        text,
        metadata={"interaction_mode": "mixed", **metadata},
    )
    return persisted_id, assistant.get("id") if isinstance(assistant, Mapping) else None


async def handle_mixed_file_archive_web_turn(
    *,
    user_id: str,
    actor: ActorContext,
    message: str,
    conversation_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    storage: Any = None,
    model: Any = None,
    turn_deadline: float | None = None,
    prepared_file: PreparedFileEvidence | None = None,
    prepared_archive: PreparedFileEvidence | None = None,
    web_evidence: TransientWebComparisonEvidence | None = None,
    authenticated_turn_id: str | None = None,
    projection_id: str | None = None,
    ledger: MixedJourneyConsumeLedger | None = None,
    publisher: Callable[[dict[str, Any]], bool] | None = None,
    restarted: bool = False,
    cancel_event: asyncio.Event | None = None,
    accepted_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Admit, compare and publish one mixed file/archive/web turn as text."""

    if not actor.shared_tenant and actor.user_id != user_id and not actor.is_owner:
        raise PermissionError("actor cannot chat as another user")
    person_id = actor.own_id if actor.shared_tenant else user_id
    if not mixed_file_archive_web_turn_is_admitted(message, attachments=attachments):
        return _chat_dict(message=_NOT_ADMITTED, conversation_id=conversation_id or "")
    if (
        type(prepared_file) is not PreparedFileEvidence
        or type(prepared_archive) is not PreparedFileEvidence
        or type(web_evidence) is not TransientWebComparisonEvidence
        or model is None
    ):
        return _chat_dict(message=_UNAVAILABLE, conversation_id=conversation_id or "")

    _file_sha, _archive_sha, _web_sha, source_identity = mixed_file_archive_web_source_evidence_identity(
        prepared_file,
        prepared_archive,
        web_evidence,
    )
    turn_id = authenticated_turn_id if authenticated_turn_id else "mixed.file.archive.web"
    journey_id = projection_id if projection_id else f"mixed:{turn_id}"
    key = MixedJourneyConsumeKey(journey_id, turn_id, source_identity)
    active_ledger = ledger if ledger is not None else _PROCESS_LEDGER
    existing = active_ledger.get(key)
    if (
        existing is not None
        and existing.reply is not None
        and existing.state
        in {
            MixedJourneyConsumeState.COMPLETED,
            MixedJourneyConsumeState.PUBLICATION_PREPARED,
            MixedJourneyConsumeState.PUBLICATION_COMMITTED,
            MixedJourneyConsumeState.SEND_KNOWN,
            MixedJourneyConsumeState.SEND_UNKNOWN,
        }
    ):
        if existing.state in {
            MixedJourneyConsumeState.SEND_KNOWN,
            MixedJourneyConsumeState.SEND_UNKNOWN,
        }:
            return existing.reply
        return _publish(active_ledger, key, existing.reply, publisher)
    if (
        restarted
        and existing is not None
        and existing.web_started
        and existing.state
        in {
            MixedJourneyConsumeState.RUNNING,
            MixedJourneyConsumeState.FAILED,
            MixedJourneyConsumeState.CANCELLED,
        }
    ):
        return _chat_dict(message=_RESTART_UNAVAILABLE, conversation_id=conversation_id or "")

    record = active_ledger.admit(key)
    if record.state is MixedJourneyConsumeState.CANCELLED:
        return _chat_dict(message=_CANCELLED, conversation_id=conversation_id or "")
    active_ledger.mark(key, MixedJourneyConsumeState.RUNNING, web_started=True)
    if cancel_event is not None and cancel_event.is_set():
        active_ledger.cancel(key)
        return _chat_dict(message=_CANCELLED, conversation_id=conversation_id or "")

    deadline = float(turn_deadline) if turn_deadline is not None else time.monotonic() + 30.0
    plan = accepted_plan_sha256 or _plan_sha256(message, source_identity)
    compare_task = asyncio.create_task(
        compare_current_file_archive_with_web(
            model,
            request=message,
            accepted_plan_sha256=plan,
            prepared_file=prepared_file,
            prepared_archive=prepared_archive,
            web_evidence=web_evidence,
            absolute_deadline=deadline,
        )
    )
    waiters: set[asyncio.Task[Any]] = {compare_task}
    cancel_waiter: asyncio.Task[Any] | None = None
    if cancel_event is not None:
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        waiters.add(cancel_waiter)
    done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    if cancel_event is not None and cancel_event.is_set():
        compare_task.cancel()
        if cancel_waiter is not None:
            cancel_waiter.cancel()
        active_ledger.cancel(key)
        return _chat_dict(message=_CANCELLED, conversation_id=conversation_id or "")
    if cancel_waiter is not None and not cancel_waiter.done():
        cancel_waiter.cancel()
    try:
        comparison = compare_task.result()
    except MixedFileArchiveWebComparisonError:
        active_ledger.mark(key, MixedJourneyConsumeState.FAILED)
        return _chat_dict(message=_FAILED, conversation_id=conversation_id or "")
    except Exception:
        active_ledger.mark(key, MixedJourneyConsumeState.FAILED)
        return _chat_dict(message=_FAILED, conversation_id=conversation_id or "")
    if type(comparison) is not MixedFileArchiveWebComparison:
        active_ledger.mark(key, MixedJourneyConsumeState.FAILED)
        return _chat_dict(message=_FAILED, conversation_id=conversation_id or "")
    current = active_ledger.get(key)
    if current is not None and current.state is MixedJourneyConsumeState.CANCELLED:
        return _chat_dict(message=_CANCELLED, conversation_id=conversation_id or "")

    web_sources = [
        {"url": str(payload["url"]), "title": str(payload["title"])}
        for source in web_evidence.sources
        if (payload := source.synthesis_payload()) and str(payload.get("url") or "").strip()
    ]
    citations = [{"label": label} for label in comparison.citation_labels]
    answer_sha256 = hashlib.sha256(comparison.answer.encode("utf-8")).hexdigest()
    web_consumption = _web_consumption(
        turn_id,
        len(web_evidence.sources),
        web_evidence.selected_provider_id,
    )
    observed = _observe_identity(
        projection_id=journey_id,
        turn_id=turn_id,
        prepared_file=prepared_file,
        prepared_archive=prepared_archive,
        web_consumption=web_consumption,
        answer_sha256=answer_sha256,
    )
    table_id = f"tbl_{answer_sha256[:16]}"
    context = {
        "interaction_mode": "mixed",
        "carrier": EngineerResultCarrierKind.TEXT.value,
        "mixed_consume_state": MixedJourneyConsumeState.COMPLETED.value,
        "mixed_organs": list(observed.view.organs.present_organs) if observed.view is not None else [],
        "mixed_archive_member_count": 1,
        "mixed_source_identity_sha256": source_identity,
    }
    metadata = {
        "carrier": EngineerResultCarrierKind.TEXT.value,
        "mixed_source_identity_sha256": source_identity,
        "web_evidence_status": "sourced" if web_sources else "empty",
    }
    persisted_id, assistant_id = _persist(
        storage,
        person_id=person_id,
        conversation_id=conversation_id,
        message=message,
        text=comparison.answer,
        metadata=metadata,
    )
    reply = _chat_dict(
        message=comparison.answer,
        conversation_id=persisted_id,
        message_id=assistant_id,
        citations=citations,
        web_sources=web_sources,
        web_evidence_status="sourced" if web_sources else "empty",
        context=context,
        files=[],
        knowledge_objects=[{"id": table_id, "knowledge_kind": "table", "sha256": answer_sha256}],
        web_research_consumption=web_consumption,
    )
    active_ledger.mark(key, MixedJourneyConsumeState.COMPLETED, reply=reply)
    return _publish(active_ledger, key, reply, publisher)


__all__ = [
    "MixedJourneyConsumeError",
    "MixedJourneyConsumeKey",
    "MixedJourneyConsumeLedger",
    "MixedJourneyConsumeState",
    "handle_mixed_file_archive_web_turn",
]
