"""Fresh file authority for a cached mixed answer, without executing the turn."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from friday.file_evidence_reader import (
    PinnedFileEvidenceReference,
    historical_file_selection_token,
    prepare_pinned_file_evidence,
    prepare_registered_file_evidence,
    reauthorize_prepared_file_evidence_in_transaction,
)
from friday.orchestration.mixed_file_archive_web_query import mixed_file_archive_web_cues_present
from friday.orchestration.web_research_consumption import WebResearchConsumptionV1
from friday.organs.mixed_journey.observe import MIXED_SOURCE_FACTS, MIXED_SOURCE_FACTS_SCHEMA
from friday.organs.mixed_journey.runtime import MIXED_ANSWER_MODE, MIXED_SOURCE_RECEIPT
from friday.permissions import ActorContext, AuthorizationService
from friday.storage._core import read_only_storage_snapshot

_UNAVAILABLE = "Сохранённое сравнение недоступно: доступ к его источникам изменился."


def _requires_mixed_sources(metadata: Mapping[str, Any]) -> bool:
    return (
        metadata.get("answer_mode") == MIXED_ANSWER_MODE
        or MIXED_SOURCE_RECEIPT in metadata
        or MIXED_SOURCE_FACTS in metadata
    )


def authorized_mixed_reply_delivery(
    message_id: str,
    answer_sha256: str,
    *,
    storage: Any,
    settings: Any,
    actor: ActorContext,
    classify_non_mixed: bool = False,
) -> dict[str, Any] | None:
    """Authorize one bridge delivery attempt using server-owned source facts.

    The caller supplies only the message identity and the exact answer digest.
    Neither source receipts nor a replacement answer can come from the bridge.
    """
    deadline = time.monotonic() + 4.5
    try:
        with read_only_storage_snapshot(storage, absolute_deadline=deadline) as conn:
            if not AuthorizationService(storage).authorize_in_transaction(conn, actor, "chat.use").allowed:
                return None
            message = storage.get_message(message_id, actor.own_id)
            if (
                not message
                or message["role"] != "assistant"
                or hashlib.sha256(message["content"].encode("utf-8")).hexdigest() != answer_sha256
                or not storage.get_conversation(message["conversation_id"], actor.own_id)
            ):
                return None
            metadata = json.loads(message["metadata_json"])
            if not isinstance(metadata, Mapping):
                return None
            if not _requires_mixed_sources(metadata):
                # A reconnecting bridge cannot classify an answer from its cache.
                # This only proves the exact owned body belongs to another lane;
                # it grants no file/archive authority for that lane.
                return (
                    {"conversation_id": message["conversation_id"], "mixed_required": False}
                    if classify_non_mixed
                    else None
                )
        result = {
            "message_id": message_id,
            "conversation_id": message["conversation_id"],
            "message": message["content"],
            "message_format": metadata.get("message_format", "plain"),
            "context": {"answer_mode": MIXED_ANSWER_MODE},
        }
        return _authorized_mixed_reply_sources(
            result, metadata, storage=storage, settings=settings, actor=actor, deadline=deadline
        )
    except Exception:
        return None


def reauthorize_mixed_cached_reply(
    result: dict[str, Any],
    *,
    storage: Any,
    settings: Any,
    actor: ActorContext,
    absolute_deadline: float | None = None,
    request_message: str = "",
) -> dict[str, Any]:
    """Return a source-bound cache or a body-free refusal; never rerun work."""

    context = result.get("context")
    cache_claims_mixed = isinstance(context, Mapping) and context.get("answer_mode") == MIXED_ANSWER_MODE
    # The original authenticated request can require refusal even if a broken
    # cache has lost both its discriminator and its durable message identity.
    request_claims_mixed = mixed_file_archive_web_cues_present(request_message)
    refused = {
        "message": _UNAVAILABLE,
        "message_format": "plain",
        "files": [],
        "citations": [],
        "web_sources": [],
        "verified": False,
        "context": {"interaction_mode": "dialogue"},
    }
    try:
        deadline = time.monotonic() + 4.5
        if absolute_deadline is not None:
            if type(absolute_deadline) not in {int, float} or not math.isfinite(absolute_deadline):
                return refused
            deadline = min(deadline, absolute_deadline)
        if time.monotonic() >= deadline:
            return refused
        message_id = result.get("message_id")
        conversation_id = result.get("conversation_id")
        if type(message_id) is not str:
            return refused if cache_claims_mixed or request_claims_mixed else result
        message = storage.get_message(message_id, actor.own_id)
        if (
            not message
            or message["role"] != "assistant"
            or message["conversation_id"] != conversation_id
            or message["content"] != result.get("message")
            or not storage.get_conversation(conversation_id, actor.own_id)
        ):
            return refused
        metadata = json.loads(message["metadata_json"])
        if not isinstance(metadata, Mapping):
            return refused
        # Cached context is presentation data, never the authority deciding
        # whether an owned durable answer needs source reauthorization.
        durable_mixed = _requires_mixed_sources(metadata)
        if not durable_mixed and not cache_claims_mixed and not request_claims_mixed:
            return result
        if not cache_claims_mixed:
            return refused
        message_format = metadata.get("message_format", "plain")
        if (
            message_format not in {"plain", "markdown"}
            or result.get("message_format", "plain") != message_format
        ):
            return refused
        if (
            _authorized_mixed_reply_sources(
                result, metadata, storage=storage, settings=settings, actor=actor, deadline=deadline
            )
            is None
        ):
            return refused
        return result
    except Exception:  # A malformed or unreadable cache never grants source authority.
        return refused


def _closed_mixed_projection(result: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct body-free facts; nested durable JSON is never forwarded."""
    receipt = metadata[MIXED_SOURCE_RECEIPT]
    web = WebResearchConsumptionV1(**metadata["web_research_consumption"])
    if web.consumption_id != web.authenticated_turn_id:
        raise ValueError("mixed consumption belongs to another turn")
    facts = {
        "schema": MIXED_SOURCE_FACTS_SCHEMA,
        "authenticated_turn_id": web.authenticated_turn_id,
        "files": [
            {"file_id": receipt["current"]["raw_object_id"], "sha256": receipt["current"]["content_sha256"]}
        ],
        "archives": [
            {
                "archive_id": receipt["archive"]["raw_object_id"],
                "sha256": receipt["archive"]["content_sha256"],
                "member_count": 1,
            }
        ],
    }
    # Exact JSON equality also distinguishes bool from int in member_count.
    if json.dumps(metadata[MIXED_SOURCE_FACTS], sort_keys=True) != json.dumps(facts, sort_keys=True):
        raise ValueError("mixed source facts do not match their receipt and turn")
    return {
        "conversation_id": result["conversation_id"],
        "message_format": metadata.get("message_format", "plain"),
        MIXED_SOURCE_FACTS: facts,
        "web_research_consumption": asdict(web),
    }


def _authorized_mixed_reply_sources(
    result: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    storage: Any,
    settings: Any,
    actor: ActorContext,
    deadline: float,
) -> dict[str, Any] | None:
    """Bind delivery projection, message and fresh capabilities to one snapshot."""
    receipt = metadata[MIXED_SOURCE_RECEIPT]
    if (
        metadata.get("answer_mode") != MIXED_ANSWER_MODE
        or metadata.get("message_format", "plain") not in {"plain", "markdown"}
        or result.get("message_format", "plain") != metadata.get("message_format", "plain")
        or type(receipt) is not dict
        or set(receipt) != {"current", "archive", "archive_filename", "answer_sha256"}
        or receipt["answer_sha256"] != hashlib.sha256(result["message"].encode("utf-8")).hexdigest()
    ):
        return None
    pins = [PinnedFileEvidenceReference(**receipt[lane]) for lane in ("current", "archive")]
    if pins[0].raw_object_id == pins[1].raw_object_id:
        return None
    authorization = AuthorizationService(storage)
    current = prepare_pinned_file_evidence(
        storage,
        authorization,
        settings.files_dir,
        actor,
        uploaded_by=actor.own_id,
        reference=pins[0],
        max_bytes=settings.max_upload_bytes,
        absolute_deadline=deadline,
    )
    filename = receipt["archive_filename"]
    if type(filename) is not str:
        return None
    archive = prepare_registered_file_evidence(
        storage,
        authorization,
        settings.files_dir,
        actor,
        uploaded_by=actor.own_id,
        selection=historical_file_selection_token(
            tenant_id=actor.user_id,
            uploaded_by=actor.own_id,
            kind="exact_filename" if filename else "exact_raw_id",
            raw_ids=(pins[1].raw_object_id,),
            filename=filename,
        ),
        max_bytes=settings.max_upload_bytes,
        absolute_deadline=deadline,
    )
    archive_snapshot = archive.snapshot_tokens[0]
    if (
        archive_snapshot.source.identity_sha256 != pins[1].source_identity_sha256
        or archive_snapshot.content_sha256 != pins[1].content_sha256
    ):
        return None
    with read_only_storage_snapshot(storage, absolute_deadline=deadline) as conn:
        message = conn.execute(
            """SELECT message.* FROM messages message
               JOIN conversations conversation ON conversation.id=message.conversation_id
               WHERE message.id=? AND message.user_id=? AND conversation.user_id=?""",
            (result["message_id"], actor.own_id, actor.own_id),
        ).fetchone()
        if (
            message is None
            or message["role"] != "assistant"
            or message["conversation_id"] != result["conversation_id"]
            or message["content"] != result["message"]
            or not authorization.authorize_in_transaction(conn, actor, "chat.use").allowed
        ):
            return None
        final_metadata = json.loads(message["metadata_json"])
        # Sources were prepared outside this transaction. A changed receipt or
        # projection invalidates that preparation; never authorize B and emit A.
        if final_metadata != metadata:
            return None
        projection = _closed_mixed_projection(result, final_metadata)
        if (
            not all(
                reauthorize_prepared_file_evidence_in_transaction(
                    conn,
                    authorization,
                    settings.files_dir,
                    actor,
                    prepared,
                    max_bytes=settings.max_upload_bytes,
                    storage=storage,
                )
                for prepared in (current, archive)
            )
            or time.monotonic() >= deadline
        ):
            return None
        # Valid UNAVAILABLE web facts still permit an explicitly partial answer.
        return projection


__all__ = ["authorized_mixed_reply_delivery", "reauthorize_mixed_cached_reply"]
