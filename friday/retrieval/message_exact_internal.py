"""Direct code-owned adapter for exact current-conversation transcripts.

This is deliberately not an ``ExecutionKernel`` tool.  A trusted caller passes
one already-authenticated turn and owns the SQLite transaction.  The adapter
reauthorizes both required permissions before the storage layer may inspect a
conversation, count messages, or read a body.  Its model projection and its
late, body-free publication decision are separate values.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from friday.orchestration.supervisor_contracts import CONVERSATION_WINDOW_READ_ID
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    ConversationScopeKind,
    TurnContextError,
    TurnContextIssuer,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.message_exact_contract import (
    MESSAGE_EXACT_REQUEST_SCHEMA,
    MessageExactContractError,
    MessageExactPage,
    MessageExactProjection,
    MessageExactPublicationDecision,
    MessageExactPublicationStatus,
    MessageExactRequest,
    _create_message_exact_publication_decision,
    project_message_exact_page,
)

MESSAGE_EXACT_INTERNAL_ADAPTER_SCHEMA: Final = "friday.message-exact-internal-adapter.v1"
MESSAGE_EXACT_INTERNAL_ADAPTER_ID: Final = (
    "friday.retrieval.message_exact_internal.MessageExactInternalAdapter"
)
MESSAGE_EXACT_SECURITY_IDS: Final = ("conversations.read", "search.use")


class MessageExactInternalError(RuntimeError):
    """The direct exact-message lane could not preserve its closed contract."""


class MessageExactReadDenied(PermissionError):
    """Fresh transactional authority denied the initial private read."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise MessageExactInternalError("message-exact adapter binding is invalid") from None


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class MessageExactAdapterBinding:
    """Static identity consumed by a later operational-capability resolver."""

    adapter_id: str = MESSAGE_EXACT_INTERNAL_ADAPTER_ID
    capability_id: str = CONVERSATION_WINDOW_READ_ID
    security_ids: tuple[str, ...] = MESSAGE_EXACT_SECURITY_IDS
    request_schema: str = MESSAGE_EXACT_REQUEST_SCHEMA
    effect_class: str = "read"
    model_visible: bool = False

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if (
            self.adapter_id != MESSAGE_EXACT_INTERNAL_ADAPTER_ID
            or self.capability_id != CONVERSATION_WINDOW_READ_ID
            or self.security_ids != MESSAGE_EXACT_SECURITY_IDS
            or self.request_schema != MESSAGE_EXACT_REQUEST_SCHEMA
            or self.effect_class != "read"
            or self.model_visible is not False
        ):
            raise MessageExactInternalError("message-exact adapter binding is not closed")

    def payload(self) -> dict[str, object]:
        self._validate()
        return {
            "adapter_id": self.adapter_id,
            "capability_id": self.capability_id,
            "effect_class": self.effect_class,
            "model_visible": self.model_visible,
            "request_schema": self.request_schema,
            "schema": MESSAGE_EXACT_INTERNAL_ADAPTER_SCHEMA,
            "security_ids": list(self.security_ids),
        }

    def canonical_sha256(self) -> str:
        return _sha256(self.payload())


MESSAGE_EXACT_ADAPTER_BINDING: Final = MessageExactAdapterBinding()


class _AuthorizationDenied(Exception):
    __slots__ = ()


class MessageExactInternalAdapter:
    """Authorize, prepare, project, and late-revalidate one exact page."""

    __slots__ = ("_authorization", "_issuer")

    def __init__(
        self,
        authorization: AuthorizationService,
        issuer: TurnContextIssuer,
    ) -> None:
        if type(authorization) is not AuthorizationService:
            raise TypeError("message-exact adapter requires AuthorizationService")
        if type(issuer) is not TurnContextIssuer:
            raise TypeError("message-exact adapter requires TurnContextIssuer")
        self._authorization = authorization
        self._issuer = issuer

    @property
    def binding(self) -> MessageExactAdapterBinding:
        return MESSAGE_EXACT_ADAPTER_BINDING

    def _admitted_scope(
        self,
        context: AuthenticatedTurnContext,
        request: MessageExactRequest,
    ) -> tuple[AuthenticatedTurnContext, ActorContext]:
        if type(context) is not AuthenticatedTurnContext or type(request) is not MessageExactRequest:
            raise MessageExactInternalError("message-exact call requires exact typed inputs")
        admitted = self._issuer.require_context(context)
        authority = admitted.authority
        if (
            authority.conversation.kind is not ConversationScopeKind.EXISTING
            or authority.conversation_id != request.conversation_id
            or type(authority.actor) is not ActorContext
            or admitted.identity.authority_sha256 != authority.canonical_sha256()
        ):
            raise MessageExactInternalError(
                "message-exact request escaped its authenticated current conversation"
            )
        return admitted, authority.actor

    def _authorization_bindings(
        self,
        conn: sqlite3.Connection,
        actor: ActorContext,
    ) -> tuple[tuple[str, str, str], ...]:
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            raise MessageExactInternalError(
                "message-exact adapter requires a caller-owned SQLite transaction"
            )
        principal_id = actor.own_id
        bindings: list[tuple[str, str, str]] = []
        for security_id in MESSAGE_EXACT_SECURITY_IDS:
            decision = self._authorization.authorize_in_transaction(conn, actor, security_id)
            preset_key = decision.preset_key
            if not decision.allowed:
                raise _AuthorizationDenied
            if (
                decision.security_id != security_id
                or decision.user_id != principal_id
                or type(preset_key) is not str
                or not preset_key
                or preset_key != preset_key.strip()
            ):
                raise MessageExactInternalError(
                    "message-exact transactional authorization binding is invalid"
                )
            bindings.append((security_id, decision.user_id, preset_key))
        return tuple(bindings)

    def _storage_authority(
        self,
        conn: sqlite3.Connection,
        *,
        admitted: AuthenticatedTurnContext,
        actor: ActorContext,
        request: MessageExactRequest,
    ) -> Any:
        from friday.storage._message_exact_internal import (
            _issue_message_exact_storage_authority_in_transaction,
        )

        authorization_bindings = self._authorization_bindings(conn, actor)
        return _issue_message_exact_storage_authority_in_transaction(
            conn,
            request=request,
            principal_id=actor.own_id,
            turn_id=admitted.turn_id,
            turn_authority_sha256=admitted.identity.authority_sha256,
            context_authority_sha256=admitted.context_authority_sha256,
            tenant_binding_sha256=admitted.authority.tenant_binding_sha256,
            person_binding_sha256=admitted.authority.person_binding_sha256,
            adapter_binding_sha256=self.binding.canonical_sha256(),
            authorization_bindings=authorization_bindings,
        )

    def prepare_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        context: AuthenticatedTurnContext,
        request: MessageExactRequest,
    ) -> MessageExactPage:
        """Freshly authorize before storage may inspect scope, counts, or bodies.

        ``request`` is an internal input: runtime activation must have bound its
        accepted boundary row to this exact durable ingress.  The foundation
        additionally proves that the supplied row is an owned user boundary in
        the authenticated current conversation.
        """

        admitted, actor = self._admitted_scope(context, request)
        try:
            storage_authority = self._storage_authority(
                conn,
                admitted=admitted,
                actor=actor,
                request=request,
            )
        except _AuthorizationDenied:
            raise MessageExactReadDenied("message-exact read authorization denied") from None
        from friday.storage._message_exact_internal import (
            select_message_exact_page_in_transaction,
        )

        return select_message_exact_page_in_transaction(
            conn,
            storage_authority,
            request=request,
        )

    def project_for_model(self, page: MessageExactPage) -> MessageExactProjection:
        """Return the only model-safe view; cursor and all identities stay private."""

        return project_message_exact_page(page)

    def reauthorize_for_publication_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        context: AuthenticatedTurnContext,
        page: MessageExactPage,
    ) -> MessageExactPublicationDecision:
        """Reauthorize and reselect the exact page before final publication.

        Every non-authorized status is body-free.  The already-produced model
        draft is deliberately not accepted by this method and cannot influence
        the decision.
        """

        if type(page) is not MessageExactPage or not page._is_process_owned():
            raise MessageExactInternalError("message-exact publication requires its private page")
        status = MessageExactPublicationStatus.UNAVAILABLE
        try:
            admitted, actor = self._admitted_scope(context, page.request)
            storage_authority = self._storage_authority(
                conn,
                admitted=admitted,
                actor=actor,
                request=page.request,
            )
        except _AuthorizationDenied:
            status = MessageExactPublicationStatus.DENIED
        except (MessageExactContractError, MessageExactInternalError, TurnContextError):
            status = MessageExactPublicationStatus.UNAVAILABLE
        except Exception:  # noqa: BLE001 - unavailable authority denies publication
            status = MessageExactPublicationStatus.UNAVAILABLE
        else:
            try:
                from friday.storage._message_exact_internal import (
                    MessageExactStorageDrift,
                    reselect_message_exact_page_in_transaction,
                )

                current = reselect_message_exact_page_in_transaction(
                    conn,
                    storage_authority,
                    page,
                )
            except MessageExactStorageDrift:
                status = MessageExactPublicationStatus.DRIFTED
            except Exception:  # noqa: BLE001 - a failed private recheck denies publication
                status = MessageExactPublicationStatus.UNAVAILABLE
            else:
                status = (
                    MessageExactPublicationStatus.AUTHORIZED
                    if current.selection_handle == page.selection_handle
                    and current.authority_handle == page.authority_handle
                    else MessageExactPublicationStatus.DRIFTED
                )
        return _create_message_exact_publication_decision(page=page, status=status)


__all__ = [
    "MESSAGE_EXACT_ADAPTER_BINDING",
    "MESSAGE_EXACT_INTERNAL_ADAPTER_ID",
    "MESSAGE_EXACT_INTERNAL_ADAPTER_SCHEMA",
    "MESSAGE_EXACT_SECURITY_IDS",
    "MessageExactAdapterBinding",
    "MessageExactInternalAdapter",
    "MessageExactInternalError",
    "MessageExactReadDenied",
]
