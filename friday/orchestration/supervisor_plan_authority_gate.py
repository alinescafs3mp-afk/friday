"""Fresh production admission gate used immediately before plan minting."""

from __future__ import annotations

from typing import Any

from friday.orchestration.supervisor_assist_surface import CurrentFileWebAssistSurface
from friday.orchestration.supervisor_contracts import canonical_sha256
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityBoundary,
    PlanAuthorityDecision,
    PlanAuthorityReason,
    PlanAuthorityScope,
    PlanSourceBinding,
    attest_plan_authority,
    authority_witness_sha256,
    current_raw_source_matches,
    source_bindings_sha256,
)
from friday.orchestration.transient_web_comparison import TRANSIENT_WEB_SECURITY_ID
from friday.permissions import AuthorizationService
from friday.source_identity import raw_source_identity_sha256
from friday.storage._base import normalize_conversation_mode
from friday.storage._core import read_only_storage_snapshot

_FILE_SECURITY_ID = "files.read"
_REQUIRED_SECURITY_IDS = tuple(sorted((_FILE_SECURITY_ID, TRANSIENT_WEB_SECURITY_ID)))


class SupervisorAssistPlanAuthorityGate:
    """Reauthorize one personal source in a fresh read snapshot before mint."""

    __slots__ = ("_authorization", "_storage")

    def __init__(self, storage: Any, authorization: AuthorizationService) -> None:
        if not hasattr(storage, "conn"):
            raise TypeError("plan authority gate requires storage")
        if type(authorization) is not AuthorizationService:
            raise TypeError("plan authority gate requires AuthorizationService")
        self._storage = storage
        self._authorization = authorization

    def __call__(
        self,
        surface: CurrentFileWebAssistSurface,
        boundary: PlanAuthorityBoundary,
    ) -> PlanAuthorityDecision:
        if (
            type(surface) is not CurrentFileWebAssistSurface
            or type(boundary) is not PlanAuthorityBoundary
            or boundary.scope is not PlanAuthorityScope.ASSIST_EXECUTION
            or surface.actor.user_id != surface.actor.own_id
            or boundary.required_security_ids != _REQUIRED_SECURITY_IDS
            or len(boundary.required_security_ids) != 2
        ):
            return PlanAuthorityDecision.rejected(PlanAuthorityReason.INVALID_BOUNDARY)
        try:
            with read_only_storage_snapshot(self._storage) as conn:
                user = conn.execute(
                    "SELECT status FROM users WHERE id=?",
                    (surface.actor.own_id,),
                ).fetchone()
                conversation = conn.execute(
                    "SELECT mode,is_archived FROM conversations WHERE id=? AND user_id=?",
                    (surface.conversation_id, surface.actor.user_id),
                ).fetchone()
                if (
                    user is None
                    or str(user["status"] or "") != "active"
                    or conversation is None
                    or int(conversation["is_archived"]) != 0
                    or normalize_conversation_mode(conversation["mode"]) != "dialogue"
                ):
                    return PlanAuthorityDecision.rejected(PlanAuthorityReason.DENIED)
                row = conn.execute(
                    """SELECT id,source,source_ref,content_type,received_at,content_hash,
                              raw_content AS _raw_content,metadata_json AS _raw_metadata
                         FROM raw_objects
                        WHERE id=? AND user_id=? AND deleted_at IS NULL""",
                    (surface.attachment.raw_object_id, surface.actor.user_id),
                ).fetchone()
                if row is None:
                    return PlanAuthorityDecision.rejected(PlanAuthorityReason.SOURCE_DRIFT)
                projection = dict(row)
                source_identity = raw_source_identity_sha256(projection)
                content_sha256 = str(projection.get("content_hash") or "")
                try:
                    expected = PlanSourceBinding.current_raw_object(
                        raw_object_id=surface.attachment.raw_object_id,
                        source_identity_sha256=source_identity,
                        content_sha256=content_sha256,
                    )
                except (TypeError, ValueError):
                    return PlanAuthorityDecision.rejected(PlanAuthorityReason.SOURCE_DRIFT)
                if boundary.source_bindings_sha256 != source_bindings_sha256(
                    (expected,)
                ) or not current_raw_source_matches(
                    expected,
                    raw_object_id=surface.attachment.raw_object_id,
                    source_identity_sha256=surface.attachment.source_identity_sha256,
                    content_sha256=surface.attachment_content_sha256,
                ):
                    return PlanAuthorityDecision.rejected(PlanAuthorityReason.SOURCE_DRIFT)
                decisions = tuple(
                    self._authorization.authorize_in_transaction(
                        conn,
                        surface.actor,
                        security_id,
                    )
                    for security_id in boundary.required_security_ids
                )
                if not all(item.allowed for item in decisions):
                    return PlanAuthorityDecision.rejected(PlanAuthorityReason.DENIED)
                permission_state_sha256 = canonical_sha256(
                    {
                        "schema": "friday.supervisor-plan-permission-state.private.v1",
                        "security_ids": list(boundary.required_security_ids),
                        "allowed": [item.allowed for item in decisions],
                    }
                )
                witness = authority_witness_sha256(
                    boundary,
                    expected.canonical_sha256(),
                    permission_state_sha256,
                )
        except Exception:
            return PlanAuthorityDecision.rejected(PlanAuthorityReason.DENIED)
        return attest_plan_authority(boundary, witness_sha256=witness)


__all__ = ["SupervisorAssistPlanAuthorityGate"]
