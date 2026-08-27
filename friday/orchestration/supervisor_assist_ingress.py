"""Body-free ingress identity for the promoted supervisor assist journey.

The request body and caller-provided source reference remain at the HTTP
boundary.  This module carries only their already-bounded SHA-256 identities
and a closed relationship to one exact durable WorkGraph.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from friday.pending_durable_turn import PendingDurableTurnAdmission

SUPERVISOR_ASSIST_INGRESS_BINDING_SCHEMA = "friday.supervisor-assist-ingress-binding.v1"
SUPERVISOR_ASSIST_INGRESS_METADATA_KEY = "semantic_supervisor_ingress_binding"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class SupervisorAssistPendingRelation(StrEnum):
    """The only relationships an active assist graph recognizes."""

    ROOT_REPLAY = "root_replay"
    NEW_TURN = "new_turn"
    EXPLICIT_CANCEL = "explicit_cancel"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class SupervisorAssistIngressBindingV1:
    """Code-owned projection of one successfully claimed idempotent request."""

    source_ref_sha256: str
    request_fingerprint_sha256: str

    def __post_init__(self) -> None:
        if (
            _DIGEST_RE.fullmatch(self.source_ref_sha256) is None
            or _DIGEST_RE.fullmatch(self.request_fingerprint_sha256) is None
        ):
            raise ValueError("assist ingress binding requires exact SHA-256 identities")

    @classmethod
    def from_claimed_request(
        cls,
        *,
        source_ref: str,
        request_fingerprint_sha256: str,
    ) -> SupervisorAssistIngressBindingV1:
        """Project a key only after its surrounding request claim succeeded."""

        if (
            type(source_ref) is not str
            or not source_ref
            or source_ref != source_ref.strip()
            or len(source_ref) > 500
        ):
            raise ValueError("claimed request source reference is invalid")
        return cls(
            source_ref_sha256=hashlib.sha256(source_ref.encode("utf-8", errors="strict")).hexdigest(),
            request_fingerprint_sha256=request_fingerprint_sha256,
        )

    def payload(self) -> dict[str, str]:
        return {
            "schema": SUPERVISOR_ASSIST_INGRESS_BINDING_SCHEMA,
            "source_ref_sha256": self.source_ref_sha256,
            "request_fingerprint_sha256": self.request_fingerprint_sha256,
        }

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.payload())

    @classmethod
    def parse(cls, value: object) -> SupervisorAssistIngressBindingV1:
        if type(value) is not dict or set(value) != {
            "schema",
            "source_ref_sha256",
            "request_fingerprint_sha256",
        }:
            raise ValueError("stored assist ingress binding shape is invalid")
        item = value
        if item.get("schema") != SUPERVISOR_ASSIST_INGRESS_BINDING_SCHEMA:
            raise ValueError("stored assist ingress binding schema is invalid")
        return cls(
            source_ref_sha256=str(item.get("source_ref_sha256") or ""),
            request_fingerprint_sha256=str(item.get("request_fingerprint_sha256") or ""),
        )


def attach_supervisor_assist_ingress_binding(
    metadata: Mapping[str, object],
    binding: SupervisorAssistIngressBindingV1,
) -> dict[str, object]:
    """Persist only the two body-free roots needed to reconstruct a restart surface."""

    if not isinstance(metadata, Mapping) or type(binding) is not SupervisorAssistIngressBindingV1:
        raise TypeError("assist ingress metadata requires an exact binding")
    result = dict(metadata)
    result[SUPERVISOR_ASSIST_INGRESS_METADATA_KEY] = binding.payload()
    return result


def load_supervisor_assist_ingress_binding(
    metadata: Mapping[str, object],
) -> SupervisorAssistIngressBindingV1:
    if not isinstance(metadata, Mapping):
        raise TypeError("assist ingress metadata must be a mapping")
    return SupervisorAssistIngressBindingV1.parse(metadata.get(SUPERVISOR_ASSIST_INGRESS_METADATA_KEY))


@dataclass(frozen=True, slots=True)
class SupervisorAssistPendingDecision:
    """Closed pre-ingestion decision for one exact active assist graph."""

    relation: SupervisorAssistPendingRelation
    person_id: str
    conversation_id: str
    pending: PendingDurableTurnAdmission | None = field(default=None, repr=False)
    root_request_binding_sha256: str | None = None
    current_request_binding_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.relation, SupervisorAssistPendingRelation):
            raise TypeError("assist pending relation is invalid")
        if _USER_ID_RE.fullmatch(self.person_id) is None:
            raise ValueError("assist pending person scope is invalid")
        if _CONVERSATION_ID_RE.fullmatch(self.conversation_id) is None:
            raise ValueError("assist pending conversation scope is invalid")
        if self.current_request_binding_sha256 is not None and (
            _DIGEST_RE.fullmatch(self.current_request_binding_sha256) is None
        ):
            raise ValueError("assist pending current request binding is invalid")
        if self.relation is SupervisorAssistPendingRelation.UNCERTAIN:
            if self.pending is not None or self.root_request_binding_sha256 is not None:
                raise ValueError("uncertain assist pending decision cannot claim a graph")
            return
        if (
            type(self.pending) is not PendingDurableTurnAdmission
            or not self.pending.is_owned
            or self.pending.work_graph_id is None
            or self.pending.work_item_id is not None
            or not self.pending.matches_scope(
                person_id=self.person_id,
                conversation_id=self.conversation_id,
            )
            or self.root_request_binding_sha256 is None
            or _DIGEST_RE.fullmatch(self.root_request_binding_sha256) is None
            or self.current_request_binding_sha256 is None
        ):
            raise ValueError("assist pending graph binding is invalid")
        same_request = hmac.compare_digest(
            self.root_request_binding_sha256,
            self.current_request_binding_sha256,
        )
        if self.relation is SupervisorAssistPendingRelation.ROOT_REPLAY and not same_request:
            raise ValueError("root replay does not match the admitted request")
        if self.relation is SupervisorAssistPendingRelation.NEW_TURN and same_request:
            raise ValueError("new turn reuses the admitted request binding")
        if self.relation is SupervisorAssistPendingRelation.EXPLICIT_CANCEL and same_request:
            raise ValueError("cancellation reuses the admitted request binding")

    @classmethod
    def for_graph(
        cls,
        *,
        relation: SupervisorAssistPendingRelation,
        pending: PendingDurableTurnAdmission,
        root_request_binding_sha256: str,
        current: SupervisorAssistIngressBindingV1,
    ) -> SupervisorAssistPendingDecision:
        if relation not in {
            SupervisorAssistPendingRelation.ROOT_REPLAY,
            SupervisorAssistPendingRelation.NEW_TURN,
            SupervisorAssistPendingRelation.EXPLICIT_CANCEL,
        }:
            raise ValueError("assist graph decision relation is not actionable")
        return cls(
            relation=relation,
            person_id=pending.person_id,
            conversation_id=pending.conversation_id,
            pending=pending,
            root_request_binding_sha256=root_request_binding_sha256,
            current_request_binding_sha256=current.canonical_sha256(),
        )

    @classmethod
    def uncertain(
        cls,
        *,
        person_id: str,
        conversation_id: str,
        current: SupervisorAssistIngressBindingV1 | None = None,
    ) -> SupervisorAssistPendingDecision:
        return cls(
            relation=SupervisorAssistPendingRelation.UNCERTAIN,
            person_id=person_id,
            conversation_id=conversation_id,
            current_request_binding_sha256=(None if current is None else current.canonical_sha256()),
        )

    @property
    def suppresses_ingestion(self) -> bool:
        return self.relation is not SupervisorAssistPendingRelation.NEW_TURN

    @property
    def permits_legacy(self) -> bool:
        return self.relation is SupervisorAssistPendingRelation.NEW_TURN

    def matches_message(self, message: object) -> bool:
        """Reject a carried cancel/new relation that disagrees with current text."""

        if type(message) is not str or not message:
            return False
        if self.relation is SupervisorAssistPendingRelation.UNCERTAIN:
            return True
        explicit_cancel = message.strip().casefold() in {"отмена", "cancel"}
        return explicit_cancel is (self.relation is SupervisorAssistPendingRelation.EXPLICIT_CANCEL)


__all__ = [
    "SUPERVISOR_ASSIST_INGRESS_BINDING_SCHEMA",
    "SUPERVISOR_ASSIST_INGRESS_METADATA_KEY",
    "SUPERVISOR_ASSIST_INGRESS_BINDING_SCHEMA",
    "SupervisorAssistIngressBindingV1",
    "SupervisorAssistPendingDecision",
    "SupervisorAssistPendingRelation",
    "attach_supervisor_assist_ingress_binding",
    "load_supervisor_assist_ingress_binding",
]
