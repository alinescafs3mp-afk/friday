"""Body-free reconstruction of one durable assist request after process restart."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebWorkGraph,
)
from friday.orchestration.contracts import TurnInput
from friday.orchestration.current_file_web_comparison import (
    current_file_web_request_is_admitted,
)
from friday.orchestration.router import ReadOnlyAttachmentReference
from friday.orchestration.supervisor_assist_ingress import (
    SUPERVISOR_ASSIST_INGRESS_METADATA_KEY,
    load_supervisor_assist_ingress_binding,
)
from friday.orchestration.supervisor_assist_surface import CurrentFileWebAssistSurface
from friday.orchestration.transient_web_comparison import seal_explicit_public_web_query
from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import raw_source_identity_sha256
from friday.storage._base import normalize_conversation_mode
from friday.storage._core import read_only_storage_snapshot


class SupervisorAssistRecoveryDataError(RuntimeError):
    """A graph claims restart material whose persisted shape is corrupt."""


@dataclass(frozen=True, slots=True, repr=False)
class RecoveredAssistSurface:
    graph: CompareCurrentFileWebWorkGraph
    surface: CurrentFileWebAssistSurface = field(repr=False)


class SupervisorAssistRecoverySurfaceLoader:
    """Reconstruct exact request/source roots without restoring old authority."""

    __slots__ = ("_authorization", "_storage")

    def __init__(self, storage: Any, authorization: AuthorizationService) -> None:
        if not hasattr(storage, "conn"):
            raise TypeError("assist recovery loader requires storage")
        if not isinstance(authorization, AuthorizationService):
            raise TypeError("assist recovery loader requires AuthorizationService")
        self._storage = storage
        self._authorization = authorization

    @staticmethod
    def _attachment_name(raw_metadata: object) -> str:
        try:
            metadata = json.loads(str(raw_metadata or "{}"))
        except (TypeError, ValueError):
            return "recovered-current-file"
        if not isinstance(metadata, dict):
            return "recovered-current-file"
        for key in ("filename", "name", "original_name"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip() and len(value.strip()) <= 255:
                return value.strip()
        return "recovered-current-file"

    def __call__(self, graph: CompareCurrentFileWebWorkGraph) -> RecoveredAssistSurface | None:
        if (
            type(graph) is not CompareCurrentFileWebWorkGraph
            or graph.state is not CompareCurrentFileWebGraphState.ACTIVE
            or not graph.has_exact_request_binding
        ):
            return None
        with read_only_storage_snapshot(self._storage) as conn:
            row = conn.execute(
                """SELECT boundary.content AS _anchor_content,
                          boundary.metadata_json AS _anchor_metadata,
                          conversation.mode AS _conversation_mode,
                          conversation.is_archived AS _conversation_archived,
                          owner.status AS _owner_status,
                          source.id,source.source,source.source_ref,
                          source.content_type,source.received_at,source.content_hash,
                          source.raw_content AS _raw_content,
                          source.metadata_json AS _raw_metadata
                     FROM messages boundary
                     JOIN conversations conversation
                       ON conversation.id=boundary.conversation_id
                      AND conversation.user_id=boundary.user_id
                     JOIN users owner ON owner.id=boundary.user_id
                     JOIN raw_objects source
                       ON source.id=? AND source.user_id=boundary.user_id
                    WHERE boundary.id=? AND boundary.user_id=?
                      AND boundary.conversation_id=? AND boundary.role='user'
                      AND source.deleted_at IS NULL""",
                (
                    graph.current_file_raw_object_id,
                    graph.anchor_user_message_id,
                    graph.user_id,
                    graph.conversation_id,
                ),
            ).fetchone()
        if (
            row is None
            or str(row["_owner_status"] or "") != "active"
            or int(row["_conversation_archived"]) != 0
            or normalize_conversation_mode(row["_conversation_mode"]) != "dialogue"
        ):
            return None
        projection = dict(row)
        if (
            raw_source_identity_sha256(projection) != graph.current_file_source_identity_sha256
            or str(row["content_hash"] or "") != graph.current_file_content_sha256
        ):
            return None
        try:
            metadata = json.loads(str(row["_anchor_metadata"] or "{}"))
        except (TypeError, ValueError) as exc:
            raise SupervisorAssistRecoveryDataError("assist recovery anchor metadata is corrupt") from exc
        if not isinstance(metadata, dict):
            raise SupervisorAssistRecoveryDataError("assist recovery anchor metadata is not an object")
        if SUPERVISOR_ASSIST_INGRESS_METADATA_KEY not in metadata:
            # Exact schema-45 graphs admitted before restart roots were persisted
            # remain owned until their bounded TTL retirement; they are not guessed.
            return None
        try:
            ingress = load_supervisor_assist_ingress_binding(metadata)
        except (TypeError, ValueError) as exc:
            raise SupervisorAssistRecoveryDataError("assist recovery ingress binding is corrupt") from exc
        if ingress.canonical_sha256() != graph.anchor_request_binding_sha256:
            raise SupervisorAssistRecoveryDataError(
                "assist recovery ingress binding does not match the durable graph"
            )
        actor = self._authorization.actor_for_user(
            graph.user_id,
            source="semantic-recovery",
        )
        if type(actor) is not ActorContext or actor.user_id != graph.user_id or actor.own_id != graph.user_id:
            return None
        message = str(row["_anchor_content"] or "")
        if not current_file_web_request_is_admitted(message):
            return None
        turn = TurnInput.from_chat(
            message=message,
            actor=actor,
            conversation_id=graph.conversation_id,
            attachments=[
                {
                    "mime_type": str(row["content_type"] or "application/octet-stream"),
                    # Only the resulting boolean descriptor reaches planning.
                    # The durable body stays behind the authorized file reader.
                    "transient_text": "available",
                }
            ],
            enable_tools=True,
            synthetic_document_notice=False,
            mode=None,
            reply_to=None,
            quoted_attachment_reference=False,
            reply_assistant_reference=False,
        )
        surface = CurrentFileWebAssistSurface(
            turn=turn,
            actor=actor,
            conversation_id=graph.conversation_id,
            attachment=ReadOnlyAttachmentReference(
                ordinal=1,
                raw_object_id=graph.current_file_raw_object_id,
                source_identity_sha256=graph.current_file_source_identity_sha256,
                name=self._attachment_name(row["_raw_metadata"]),
                media_type=str(row["content_type"] or "application/octet-stream"),
            ),
            attachment_content_sha256=graph.current_file_content_sha256,
            web_plan=seal_explicit_public_web_query(
                current_user_message=message,
                actor=actor,
                conversation_id=graph.conversation_id,
            ),
            ingress_binding=ingress,
        )
        return RecoveredAssistSurface(graph=graph, surface=surface)


__all__ = [
    "RecoveredAssistSurface",
    "SupervisorAssistRecoveryDataError",
    "SupervisorAssistRecoverySurfaceLoader",
]
