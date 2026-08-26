"""Backend coordinator for exact, durable host capability plans."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import socket
import stat
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from friday.file_delivery import (
    AuthorizedFileReadError,
    FileRecordUnavailable,
    read_authorized_file,
    read_current_message_upload_file,
)
from friday.permissions import ActorContext, AuthorizationError
from friday_package_broker.approval import (
    PackageApprovalSigner,
    load_backend_approval_signing_key,
)
from friday_package_broker.contracts import (
    BROKER_RECEIPT_SCHEMA_VERSION,
    AptInstallPlan,
    BrokerContractError,
    PackagePostconditionState,
    PackageReconciliationReceipt,
    PackageRef,
    PackageTransactionReceipt,
    TransactionOutcome,
)

from .adapters.base import HostAdapter
from .adapters.jq import MAX_JQ_INPUT_BYTES, JqAdapter
from .adapters.nmap import NmapAdapter
from .approval_summary import host_action_approval_summary, package_install_approval_summary
from .capability_catalog import BUILTIN_CATALOG, CapabilityEntry
from .client import (
    HostControlClient,
    HostControlClientError,
    HostControlOutcomeUnknown,
    HostControlRejected,
    HostControlUnavailable,
)
from .contracts import (
    AdapterState,
    ContractError,
    ExecutableAttestation,
    ParsedActionResult,
    canonical_json_bytes,
    decode_canonical_json,
)
from .jobs import HostJobStore, HostJobTransitionError
from .network_approval import NetworkApprovalProof, NetworkApprovalSigner
from .plans import HostActionPlan, WorkspaceGrant, create_action_plan
from .policy import (
    NetworkPolicy,
    NetworkTargetSnapshot,
    assert_target_snapshot_current,
    normalize_network_targets,
)
from .receipts import HostActionReceipt, verify_action_receipt
from .result_projection import project_action_result

_SAFE_REF = re.compile(r"^evidence/[A-Za-z0-9_.-]{1,220}$")
_PACKAGE_RECORD_FIELDS = frozenset(
    {
        "error_code",
        "execution_started_at",
        "expires_at",
        "idempotent",
        "plan_digest",
        "plan_id",
        "receipt",
        "status",
        "transaction_digest",
        "transaction_id",
        "updated_at",
    }
)
_PACKAGE_RECONCILIATION_FIELDS = frozenset(
    {
        "error_code",
        "idempotent",
        "plan_digest",
        "plan_id",
        "reconciliation",
        "status",
        "transaction_digest",
        "transaction_id",
        "updated_at",
    }
)
_KNOWN_PRE_ADMISSION_REJECTIONS = frozenset(
    {
        "approval_required",
        "execution_disabled",
        "job_plan_conflict",
        "network_approval_binding_mismatch",
        "network_approval_claim_missing",
        "network_approval_expired",
        "network_approval_fields_invalid",
        "network_approval_from_future",
        "network_approval_required",
        "network_approval_replayed",
        "network_approval_signature_invalid",
        "network_approval_unavailable",
        "network_approval_unexpected",
        "plan_envelope_mismatch",
        "request_rejected",
    }
)
ACTION_QUEUE_WAIT_SECONDS = 5.0


class HostCapabilityUnavailable(RuntimeError):
    """The host agent/capability is unavailable before action admission."""


class HostActionUnknown(RuntimeError):
    """An admitted host action needs reconciliation before any retry."""


@dataclass(frozen=True, slots=True)
class PreparedHostAction:
    plan: HostActionPlan
    adapter: HostAdapter
    attestation: ExecutableAttestation
    job: dict[str, Any]


def _verified_jq_output_attachment(
    *,
    prepared: PreparedHostAction,
    receipt: HostActionReceipt,
    parsed: ParsedActionResult,
    verified_evidence: dict[str, bytes],
    terminal_status: str,
    receipt_digest: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Project the exact verified jq stdout as an out-of-band user artifact."""

    if (
        not isinstance(prepared.adapter, JqAdapter)
        or terminal_status != "completed"
        or receipt.effect_outcome.value != "succeeded"
        or parsed.parser_status.value != "complete"
        or parsed.coverage.grade.value != "complete"
    ):
        return None
    raw_refs = tuple(item for item in parsed.evidence if item.media_type == "application/json")
    if len(raw_refs) != 1 or raw_refs[0] not in receipt.evidence:
        raise ContractError("jq output lacks one receipt-bound JSON evidence object")
    raw_ref = raw_refs[0]
    payload = verified_evidence.get(raw_ref.evidence_id)
    if (
        payload is None
        or len(payload) != raw_ref.size_bytes
        or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), raw_ref.sha256)
    ):
        raise ContractError("jq output evidence identity is invalid")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError("jq output artifact is not exact JSON") from exc
    structured = parsed.structured
    if not isinstance(structured, dict) or set(structured) != {"result"} or decoded != structured["result"]:
        raise ContractError("jq output artifact disagrees with the verified parser result")
    return {
        "content_base64": base64.b64encode(payload).decode("ascii"),
        "filename": f"jq-result-{job_id[-8:]}.json",
        "host_receipt_sha256": receipt_digest,
        "kind": "document",
        "mime_type": "application/json",
    }


class HostControlService:
    """One code-owned planning/execution seam shared by the agent tools."""

    def __init__(self, ctx: Any, client: HostControlClient) -> None:
        self._ctx = ctx
        self._settings = ctx.settings
        self._storage = ctx.storage
        self._authorization = ctx.auth
        self._client = client
        self._jobs = HostJobStore(ctx.storage)
        self._package_approval_signer_cache: PackageApprovalSigner | None = None
        self._network_approval_signer_cache: NetworkApprovalSigner | None = None
        self._action_slots = asyncio.Semaphore(ctx.settings.host_action_max_concurrency)
        adapters: tuple[HostAdapter, ...] = (NmapAdapter(), JqAdapter())
        self._adapters = {item.spec.adapter_id: item for item in adapters}

    async def _handshake(self) -> dict[str, Any]:
        try:
            handshake = await self._client.handshake(timeout_sec=2.0)
        except HostControlUnavailable as exc:
            raise HostCapabilityUnavailable("host agent is unavailable") from exc
        current_policy = self._current_network_policy()
        expected_policy_digest = current_policy.digest
        observed_policy_digest = handshake.get("network_policy_digest")
        if not isinstance(observed_policy_digest, str) or not hmac.compare_digest(
            observed_policy_digest,
            expected_policy_digest,
        ):
            raise HostCapabilityUnavailable("backend and host agent network policy identities do not match")
        if current_policy.allow_public:
            try:
                expected_approval_key_digest = self._network_approval_signer().public_key_digest
            except (OSError, ValueError) as exc:
                raise HostCapabilityUnavailable("network approval signer is unavailable") from exc
            observed_approval_key_digest = handshake.get("network_approval_public_key_digest")
            if not isinstance(observed_approval_key_digest, str) or not hmac.compare_digest(
                observed_approval_key_digest,
                expected_approval_key_digest,
            ):
                raise HostCapabilityUnavailable("backend and host agent network approval keys do not match")
        return handshake

    async def _inventory(self) -> tuple[dict[str, Any], dict[str, ExecutableAttestation]]:
        handshake = await self._handshake()
        raw_inventory = handshake.get("inventory")
        if not isinstance(raw_inventory, list) or len(raw_inventory) > 64:
            raise HostCapabilityUnavailable("host agent inventory is invalid")
        states: dict[str, AdapterState] = {}
        attestations: dict[str, ExecutableAttestation] = {}
        for raw in raw_inventory:
            if not isinstance(raw, dict):
                raise HostCapabilityUnavailable("host agent inventory is invalid")
            adapter_id = str(raw.get("adapter_id") or "")
            if adapter_id not in self._adapters or adapter_id in states:
                raise HostCapabilityUnavailable("host agent inventory identity is invalid")
            try:
                state = AdapterState(str(raw.get("state") or ""))
            except ValueError as exc:
                raise HostCapabilityUnavailable("host agent inventory state is invalid") from exc
            states[adapter_id] = state
            payload = raw.get("attestation")
            if state is AdapterState.AVAILABLE:
                try:
                    attestation = ExecutableAttestation.from_payload(payload)
                except ContractError as exc:
                    raise HostCapabilityUnavailable("host executable attestation is invalid") from exc
                if attestation.adapter_id != adapter_id:
                    raise HostCapabilityUnavailable("host executable attestation identity is invalid")
                attestations[adapter_id] = attestation
            elif payload is not None:
                raise HostCapabilityUnavailable("unavailable adapter carried an attestation")
        return states, attestations

    def _current_network_policy(self) -> NetworkPolicy:
        return NetworkPolicy(
            connected_cidrs=(),
            allowed_cidrs=tuple(getattr(self._settings, "host_allowed_cidrs", ())),
            allow_public=bool(getattr(self._settings, "host_public_network_enabled", False)),
        )

    def _assert_current_target_policy(self, plan: HostActionPlan) -> None:
        payload = plan.target_snapshot
        if plan.adapter_id != "network.nmap":
            if payload is not None:
                raise ContractError("non-network action carried network target authority")
            return
        if payload is None:
            raise ContractError("network action lacks an exact target snapshot")
        snapshot = NetworkTargetSnapshot.from_payload(payload)
        if snapshot.digest != plan.target_snapshot_digest:
            raise ContractError("network target snapshot identity drifted")
        assert_target_snapshot_current(snapshot, self._current_network_policy())

    async def _entries(self) -> tuple[CapabilityEntry, ...]:
        states, attestations = await self._inventory()
        candidates = {
            adapter_id: f"candidate_{hashlib.sha256(adapter_id.encode()).hexdigest()[:32]}"
            for adapter_id, state in states.items()
            if adapter_id == "network.nmap" and state is AdapterState.MISSING_PACKAGE
        }
        return BUILTIN_CATALOG.entries(
            adapter_states=states,
            attestation_digests={key: value.digest for key, value in attestations.items()},
            candidate_refs=candidates,
        )

    async def search(
        self,
        *,
        query: str,
        category: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        entries = await self._entries()
        found = BUILTIN_CATALOG.search(query, entries=entries, category=category, limit=limit)
        return {
            "capabilities": [item.to_public_payload() for item in found],
            "count": len(found),
            "host_agent_id": self._settings.host_agent_id,
        }

    async def describe(self, *, capability_id: str) -> dict[str, Any]:
        entries = await self._entries()
        entry = BUILTIN_CATALOG.describe(capability_id, entries=entries)
        adapter = self._adapters[entry.adapter_id]
        return {
            **entry.to_public_payload(),
            "action_contracts": [
                {
                    "action_id": action.action_id,
                    "execution_profile": action.execution_profile.value,
                    "max_output_bytes": action.max_output_bytes,
                    "risk_class": action.risk_class.value,
                    "summary": action.summary,
                    "timeout_sec": action.timeout_sec,
                }
                for action in adapter.spec.actions
                if action.capability_id == capability_id
            ],
            "packages": [item.name for item in adapter.spec.packages],
        }

    async def prepare_network_action(
        self,
        *,
        actor: ActorContext,
        capability_id: str,
        action_id: str,
        targets: list[str],
        ports: list[int] | None,
        conversation_id: str,
        source_message_id: str,
        expected_target_snapshot: dict[str, Any] | None = None,
    ) -> PreparedHostAction | dict[str, Any]:
        states, attestations = await self._inventory()
        entry = BUILTIN_CATALOG.describe(
            capability_id,
            entries=BUILTIN_CATALOG.entries(
                adapter_states=states,
                attestation_digests={key: value.digest for key, value in attestations.items()},
                candidate_refs={
                    key: f"candidate_{hashlib.sha256(key.encode()).hexdigest()[:32]}"
                    for key, state in states.items()
                    if key == "network.nmap" and state is AdapterState.MISSING_PACKAGE
                },
            ),
        )
        if entry.adapter_id != "network.nmap":
            raise ContractError("this action tool accepts only the reviewed nmap capability")
        adapter = self._adapters[entry.adapter_id]
        action = adapter.spec.action(action_id)
        if action.capability_id != capability_id:
            raise ContractError("action does not belong to the selected capability")
        self._require_action_capability(actor, action.security_id)
        if entry.state not in {AdapterState.AVAILABLE, AdapterState.MISSING_PACKAGE}:
            return {
                "adapter_id": entry.adapter_id,
                "capability_id": capability_id,
                "status": entry.state.value,
            }
        policy = self._current_network_policy()

        def resolve(host: str) -> tuple[str, ...]:
            return tuple(
                dict.fromkeys(
                    str(item[4][0])
                    for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                    if item[4]
                )
            )

        async with asyncio.timeout(5.0):
            snapshot = await asyncio.to_thread(
                normalize_network_targets,
                targets,
                policy,
                resolver=resolve,
            )
        raw_arguments: dict[str, Any] = {"target_snapshot_digest": snapshot.digest}
        if action_id == "selected_ports":
            raw_arguments["ports"] = ports
        elif ports is not None:
            raise ContractError("ports are supported only by selected_ports")
        normalized = adapter.normalize_arguments(
            action_id,
            raw_arguments,
            target_snapshot=snapshot,
        )
        if expected_target_snapshot is not None and snapshot.to_payload() != expected_target_snapshot:
            raise ContractError("network target resolution changed while acquiring the capability")
        if action.timeout_sec > self._settings.host_action_default_timeout_sec:
            raise ContractError("adapter timeout exceeds the configured host-action ceiling")
        if action.max_output_bytes > self._settings.host_action_max_output_bytes:
            raise ContractError("adapter output exceeds the configured host-action ceiling")
        if entry.state is AdapterState.MISSING_PACKAGE:
            if not self._settings.host_package_install_enabled:
                return {
                    "adapter_id": entry.adapter_id,
                    "capability_id": capability_id,
                    "package_candidate_ref": entry.package_candidate_ref,
                    "packages": [item.name for item in adapter.spec.packages],
                    "status": "missing_package",
                }
            return await self._prepare_install_then_action(
                actor=actor,
                adapter=adapter,
                capability_id=capability_id,
                action_id=action_id,
                targets=targets,
                ports=ports,
                normalized_arguments=normalized,
                target_snapshot=snapshot.to_payload(),
                conversation_id=conversation_id,
                source_message_id=source_message_id,
            )
        attestation = attestations[entry.adapter_id]
        identity_payload = {
            "action_id": action_id,
            "actor_own_id": actor.own_id,
            "adapter_id": adapter.spec.adapter_id,
            "conversation_id": conversation_id,
            "normalized_arguments": normalized,
            "source_message_id": source_message_id,
            "target_snapshot": snapshot.to_payload(),
        }
        identity = hashlib.sha256(canonical_json_bytes(identity_payload)).hexdigest()
        job_id = f"hjob_{identity[:32]}"
        existing = self._jobs.get(
            job_id,
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
        )
        if existing is not None:
            durable_plan = HostActionPlan.from_payload(existing["plan"])
            if (
                durable_plan.digest != existing["plan_digest"]
                or durable_plan.executable_attestation_digest != attestation.digest
            ):
                raise HostActionUnknown("durable host action identity drifted")
            return PreparedHostAction(durable_plan, adapter, attestation, existing)
        plan = create_action_plan(
            plan_id=f"plan:{identity[:32]}",
            actor_user_id=actor.user_id,
            actor_own_id=actor.own_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            host_agent_id=self._settings.host_agent_id,
            idempotency_key=f"action:{identity}",
            adapter=adapter.spec,
            action=action,
            normalized_arguments=normalized,
            executable_attestation=attestation,
            target_snapshot=snapshot.to_payload(),
            now=int(time.time()),
            ttl_sec=900,
        )
        job, _created = self._jobs.create_or_get(
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            host_agent_id=plan.host_agent_id,
            capability_id=plan.capability_id,
            adapter_id=plan.adapter_id,
            adapter_version=plan.implementation_version,
            action_id=plan.action_id,
            normalized_arguments=plan.normalized_arguments,
            plan=plan.to_payload(),
            plan_digest=plan.digest,
            risk_class=plan.risk_class.value,
            authorization_basis=plan.security_id,
            idempotency_key=plan.idempotency_key,
            continuation={
                "conversation_id": conversation_id,
                "source_message_id": source_message_id,
            },
            awaiting_approval=snapshot.approval_required,
            ttl_sec=min(3600, action.timeout_sec + 600),
            job_id=job_id,
        )
        # The immutable job may predate this reconstruction.  Always execute its
        # stored plan, never the newly rendered equivalent with a fresh plan id.
        durable_plan = HostActionPlan.from_payload(job["plan"])
        if durable_plan.digest != job["plan_digest"]:
            raise HostActionUnknown("durable host action plan failed its digest")
        return PreparedHostAction(durable_plan, adapter, attestation, job)

    async def prepare_file_action(
        self,
        *,
        actor: ActorContext,
        capability_id: str,
        action_id: str,
        raw_id: str,
        fields: list[str],
        compact: bool,
        conversation_id: str,
        source_message_id: str,
    ) -> PreparedHostAction | dict[str, Any]:
        """Bind one authorized Raw file to the reviewed jq extraction adapter."""

        states, attestations = await self._inventory()
        entry = BUILTIN_CATALOG.describe(
            capability_id,
            entries=BUILTIN_CATALOG.entries(
                adapter_states=states,
                attestation_digests={key: value.digest for key, value in attestations.items()},
                candidate_refs={},
            ),
        )
        if entry.adapter_id != "data.jq":
            raise ContractError("this file action accepts only the reviewed jq capability")
        adapter = self._adapters[entry.adapter_id]
        action = adapter.spec.action(action_id)
        if action.capability_id != capability_id:
            raise ContractError("action does not belong to the selected capability")
        self._require_action_capability(actor, action.security_id)
        self._require_action_capability(actor, "files.read")
        if entry.state is not AdapterState.AVAILABLE:
            return {
                "adapter_id": entry.adapter_id,
                "capability_id": capability_id,
                "packages": [item.name for item in adapter.spec.packages],
                "status": entry.state.value,
            }
        if action.timeout_sec > self._settings.host_action_default_timeout_sec:
            raise ContractError("adapter timeout exceeds the configured host-action ceiling")
        if action.max_output_bytes > self._settings.host_action_max_output_bytes:
            raise ContractError("adapter output exceeds the configured host-action ceiling")
        if re.fullmatch(r"raw_[0-9a-f]{16}", str(raw_id or "")) is None:
            raise ContractError("jq input is not an opaque Raw file handle")
        input_limit = min(
            MAX_JQ_INPUT_BYTES,
            int(getattr(self._settings, "max_upload_bytes", MAX_JQ_INPUT_BYTES)),
        )
        try:
            stored = await asyncio.to_thread(
                read_authorized_file,
                self._storage,
                Path(self._settings.files_dir),
                raw_id,
                actor.user_id,
                person_id=actor.own_id,
                max_bytes=input_limit,
            )
        except (AuthorizedFileReadError, FileRecordUnavailable, OSError, ValueError):
            try:
                stored = await asyncio.to_thread(
                    read_current_message_upload_file,
                    self._storage,
                    Path(self._settings.files_dir),
                    raw_id,
                    actor.user_id,
                    person_id=actor.own_id,
                    conversation_id=conversation_id,
                    source_message_id=source_message_id,
                    max_bytes=input_limit,
                )
            except (AuthorizedFileReadError, FileRecordUnavailable, OSError, ValueError) as exc:
                raise ContractError("jq input file is unavailable to this actor") from exc
        input_digest = hashlib.sha256(stored.content).hexdigest()
        grant_seed = hashlib.sha256(
            canonical_json_bytes(
                {
                    "actor_own_id": actor.own_id,
                    "compact": compact,
                    "fields": fields,
                    "input_sha256": input_digest,
                    "raw_id": raw_id,
                }
            )
        ).hexdigest()
        grant_id = f"grant_{grant_seed[:16]}"
        normalized = adapter.normalize_arguments(
            action_id,
            {"compact": compact, "fields": fields, "input_grant": grant_id},
        )
        identity = hashlib.sha256(
            canonical_json_bytes(
                {
                    "action_id": action_id,
                    "actor_own_id": actor.own_id,
                    "adapter_id": adapter.spec.adapter_id,
                    "conversation_id": conversation_id,
                    "input_sha256": input_digest,
                    "normalized_arguments": normalized,
                    "raw_id": raw_id,
                    "source_message_id": source_message_id,
                }
            )
        ).hexdigest()
        job_id = f"hjob_{identity[:32]}"
        relative_path = _stage_exact_job_input(
            Path(self._settings.host_job_root),
            job_id=job_id,
            content=stored.content,
            content_sha256=input_digest,
        )
        grant = WorkspaceGrant(
            grant_id=grant_id,
            actor_own_id=actor.own_id,
            access="read",
            relative_path=relative_path,
            identity_sha256=input_digest,
        )
        existing = self._jobs.get(job_id, user_id=actor.user_id, actor_own_id=actor.own_id)
        if existing is not None:
            durable_plan = HostActionPlan.from_payload(existing["plan"])
            if (
                durable_plan.digest != existing["plan_digest"]
                or durable_plan.executable_attestation_digest != attestations[entry.adapter_id].digest
                or durable_plan.workspace_grants != (grant,)
            ):
                raise HostActionUnknown("durable jq action identity drifted")
            return PreparedHostAction(
                durable_plan,
                adapter,
                attestations[entry.adapter_id],
                existing,
            )
        plan = create_action_plan(
            plan_id=f"plan:{identity[:32]}",
            actor_user_id=actor.user_id,
            actor_own_id=actor.own_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            host_agent_id=self._settings.host_agent_id,
            idempotency_key=f"action:{identity}",
            adapter=adapter.spec,
            action=action,
            normalized_arguments=normalized,
            executable_attestation=attestations[entry.adapter_id],
            workspace_grants=(grant,),
            now=int(time.time()),
            ttl_sec=900,
        )
        job, _created = self._jobs.create_or_get(
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            host_agent_id=plan.host_agent_id,
            capability_id=plan.capability_id,
            adapter_id=plan.adapter_id,
            adapter_version=plan.implementation_version,
            action_id=plan.action_id,
            normalized_arguments=plan.normalized_arguments,
            plan=plan.to_payload(),
            plan_digest=plan.digest,
            risk_class=plan.risk_class.value,
            authorization_basis=plan.security_id,
            idempotency_key=plan.idempotency_key,
            continuation={
                "conversation_id": conversation_id,
                "raw_id_sha256": hashlib.sha256(raw_id.encode("utf-8")).hexdigest(),
                "source_message_id": source_message_id,
            },
            awaiting_approval=False,
            ttl_sec=min(3600, action.timeout_sec + 600),
            job_id=job_id,
        )
        durable_plan = HostActionPlan.from_payload(job["plan"])
        if durable_plan.digest != job["plan_digest"] or durable_plan.workspace_grants != (grant,):
            raise HostActionUnknown("durable jq action plan failed its identity")
        return PreparedHostAction(durable_plan, adapter, attestations[entry.adapter_id], job)

    async def _prepare_install_then_action(
        self,
        *,
        actor: ActorContext,
        adapter: HostAdapter,
        capability_id: str,
        action_id: str,
        targets: list[str],
        ports: list[int] | None,
        normalized_arguments: dict[str, Any],
        target_snapshot: dict[str, Any],
        conversation_id: str,
        source_message_id: str,
    ) -> dict[str, Any]:
        self._require_action_capability(actor, "host.packages.install")
        packages = tuple(PackageRef(item.name) for item in adapter.spec.packages if item.manager == "apt")
        if not packages or len(packages) != len(adapter.spec.packages):
            raise ContractError("adapter does not have one closed APT acquisition plan")
        identity_payload = {
            "action_id": action_id,
            "actor_own_id": actor.own_id,
            "adapter_id": adapter.spec.adapter_id,
            "conversation_id": conversation_id,
            "normalized_arguments": normalized_arguments,
            "source_message_id": source_message_id,
            "target_snapshot": target_snapshot,
        }
        identity = hashlib.sha256(canonical_json_bytes(identity_payload)).hexdigest()
        action_job_id = f"hjob_{identity[:32]}"
        package_identity = hashlib.sha256(
            canonical_json_bytes(
                {
                    "action_job_id": action_job_id,
                    "domain": "package-install-v1",
                    "requested": [item.to_payload() for item in packages],
                }
            )
        ).hexdigest()
        package_job_id = f"hjob_{package_identity[:32]}"
        idempotency_key = f"install:{package_identity}"
        continuation = {
            "action_id": action_id,
            "action_job_id": action_job_id,
            "capability_id": capability_id,
            "conversation_id": conversation_id,
            "kind": "install_then_host_action",
            "ports": ports,
            "source_message_id": source_message_id,
            "target_snapshot": target_snapshot,
            "targets": targets,
        }
        continuation_digest = hashlib.sha256(canonical_json_bytes(continuation)).hexdigest()
        normalized_install = {
            "continuation_digest": continuation_digest,
            "requested": [item.to_payload() for item in packages],
        }
        existing = self._jobs.get(
            package_job_id,
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
        )
        if existing is not None:
            return self._existing_install_response(
                existing,
                actor=actor,
                packages=packages,
                continuation=continuation,
                normalized_install=normalized_install,
                idempotency_key=idempotency_key,
            )
        try:
            response = await self._client.call(
                "PackagePlanInstall",
                {
                    "continuation_work_item_id": package_job_id,
                    "original_task_ref": source_message_id,
                    "requested": [item.to_payload() for item in packages],
                },
                job_id=package_job_id,
                actor_id=actor.user_id,
                own_id=actor.own_id,
                idempotency_key=idempotency_key,
                plan_digest="0" * 64,
                effectful=False,
                timeout_sec=60.0,
            )
        except (HostControlRejected, HostControlUnavailable) as exc:
            raise HostCapabilityUnavailable("exact package planning is unavailable") from exc
        plan = self._validate_package_plan_record(
            response,
            include_plan=True,
            actor=actor,
            continuation_id=package_job_id,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
        )
        if tuple(item.name for item in plan.transaction.requested) != tuple(item.name for item in packages):
            raise ContractError("package broker resolved another requested package set")
        remaining_ttl = plan.expires_at - int(time.time())
        if remaining_ttl <= 0:
            raise ContractError("package plan expired before approval")
        job, _created = self._jobs.create_or_get(
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            host_agent_id=self._settings.host_agent_id,
            capability_id=capability_id,
            adapter_id="package.apt",
            adapter_version=1,
            action_id="install",
            normalized_arguments=normalized_install,
            plan=plan.to_payload(),
            plan_digest=plan.digest,
            risk_class="package_mutation",
            authorization_basis="host.packages.install",
            idempotency_key=idempotency_key,
            continuation=continuation,
            awaiting_approval=True,
            ttl_sec=remaining_ttl,
            job_id=package_job_id,
        )
        if job["status"] != "awaiting_approval":
            return {
                "job_id": job["id"],
                "package_plan_id": plan.plan_id,
                "status": job["status"],
            }
        approval = self._request_install_approval(job, plan, actor=actor)
        return {
            "approval_id": approval["id"],
            "effect_boundary_crossed": False,
            "error_code": "approval_required",
            "job_id": job["id"],
            "ok": False,
            "package_plan_id": plan.plan_id,
            "status": "awaiting_approval",
            "summary": approval["summary"],
        }

    def _existing_install_response(
        self,
        job: dict[str, Any],
        *,
        actor: ActorContext,
        packages: tuple[PackageRef, ...],
        continuation: dict[str, Any],
        normalized_install: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        try:
            plan = AptInstallPlan.from_payload(job["plan"])
        except (BrokerContractError, KeyError, TypeError) as exc:
            raise HostActionUnknown("durable package plan is invalid") from exc
        if (
            job.get("adapter_id") != "package.apt"
            or job.get("action_id") != "install"
            or job.get("authorization_basis") != "host.packages.install"
            or job.get("idempotency_key") != idempotency_key
            or job.get("normalized_arguments") != normalized_install
            or job.get("continuation") != continuation
            or plan.digest != job.get("plan_digest")
            or plan.continuation_work_item_id != job.get("id")
            or plan.actor_user_id != actor.user_id
            or plan.actor_own_id != actor.own_id
            or tuple(item.name for item in plan.transaction.requested)
            != tuple(item.name for item in packages)
        ):
            raise HostActionUnknown("durable package continuation identity drifted")
        status = str(job.get("status") or "")
        if status == "awaiting_approval":
            approval = self._request_install_approval(job, plan, actor=actor)
            return {
                "approval_id": approval["id"],
                "effect_boundary_crossed": False,
                "error_code": "approval_required",
                "job_id": job["id"],
                "ok": False,
                "package_plan_id": plan.plan_id,
                "status": status,
                "summary": approval["summary"],
            }
        return {
            "job_id": job["id"],
            "package_plan_id": plan.plan_id,
            "reconciliation_required": bool(job.get("reconciliation_required")),
            "status": status,
        }

    def _validate_package_plan_record(
        self,
        response: dict[str, Any],
        *,
        include_plan: bool,
        actor: ActorContext,
        continuation_id: str,
        source_message_id: str,
        idempotency_key: str,
    ) -> AptInstallPlan:
        expected = _PACKAGE_RECORD_FIELDS | ({"plan"} if include_plan else set())
        if set(response) != expected or response.get("status") != "planned":
            raise ContractError("package broker plan response is invalid")
        raw_plan = response.get("plan")
        if not isinstance(raw_plan, dict):
            raise ContractError("package broker omitted the exact package plan")
        try:
            plan = AptInstallPlan.from_payload(raw_plan)
        except BrokerContractError as exc:
            raise ContractError("package broker plan contract is invalid") from exc
        if (
            plan.actor_user_id != actor.user_id
            or plan.actor_own_id != actor.own_id
            or plan.continuation_work_item_id != continuation_id
            or plan.original_task_ref != source_message_id
            or response.get("plan_id") != plan.plan_id
            or response.get("plan_digest") != plan.digest
            or response.get("transaction_digest") != plan.transaction.digest
            or response.get("receipt") is not None
            or response.get("transaction_id") is not None
            or response.get("execution_started_at") is not None
            or response.get("error_code") is not None
            or not isinstance(response.get("idempotent"), bool)
            or not isinstance(response.get("expires_at"), int)
            or response.get("expires_at") != plan.expires_at
        ):
            raise ContractError("package broker plan identity drifted")
        if not idempotency_key.startswith("install:"):
            raise ContractError("package install idempotency identity is invalid")
        return plan

    def _request_install_approval(
        self,
        job: dict[str, Any],
        plan: AptInstallPlan,
        *,
        actor: ActorContext,
    ) -> dict[str, Any]:
        existing_id = str(job.get("approval_id") or "")
        if existing_id:
            existing = self._storage.get_action_approval(existing_id, actor.user_id)
            if existing is not None:
                return existing
            raise HostActionUnknown("durable package approval disappeared")
        remaining_ttl = plan.expires_at - int(time.time())
        if remaining_ttl <= 0:
            raise ContractError("package plan expired before approval")
        source = self._storage.get_message(plan.original_task_ref, actor.own_id)
        if (
            not isinstance(source, dict)
            or source.get("role") != "user"
            or str(source.get("conversation_id") or "") != str(job.get("conversation_id") or "")
            or str(job.get("source_message_id") or "") != plan.original_task_ref
        ):
            raise HostActionUnknown("package approval source message identity drifted")
        payload = {
            "job_id": job["id"],
            "package_plan": plan.to_payload(),
            "plan_digest": plan.digest,
        }
        approval = self._storage.create_action_approval(
            actor.user_id,
            tool="software_install_execute",
            payload=payload,
            summary=package_install_approval_summary(
                plan,
                expected_capabilities=("network.nmap.scan",),
                original_request=str(source.get("content") or ""),
            ),
            risk="high",
            requested_by=actor.own_id,
            conversation_id=str(job.get("conversation_id") or "") or None,
            ttl_sec=remaining_ttl,
        )
        self._jobs.bind_approval(
            str(job["id"]),
            str(approval["id"]),
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
        )
        return approval

    def _close_pre_effect_action(
        self,
        job_id: str,
        *,
        actor: ActorContext,
        outcome_code: str,
    ) -> bool:
        """Best-effort terminal close while durable state still proves no send."""

        current = self._jobs.get(job_id, user_id=actor.user_id, actor_own_id=actor.own_id)
        if current is None:
            return False
        status = str(current.get("status") or "")
        if status not in {"planned", "awaiting_approval", "approved", "admitted"}:
            return False
        try:
            self._jobs.transition(
                job_id,
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status=status,
                status="failed",
                stage="queue",
                outcome_code=outcome_code,
                error_code=outcome_code,
            )
        except HostJobTransitionError:
            return False
        return True

    @contextlib.asynccontextmanager
    async def _bounded_action_slot(
        self,
        job_id: str,
        *,
        actor: ActorContext,
    ) -> AsyncIterator[None]:
        """Wait briefly without spending an approval's whole action timeout."""

        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._action_slots.acquire(),
                    timeout=ACTION_QUEUE_WAIT_SECONDS,
                )
                acquired = True
            except TimeoutError as exc:
                closed = self._close_pre_effect_action(
                    job_id,
                    actor=actor,
                    outcome_code="action_queue_busy_before_send",
                )
                if not closed:
                    raise HostActionUnknown(
                        "host action queue state no longer proves a pre-effect refusal"
                    ) from exc
                raise HostCapabilityUnavailable("host action queue is busy") from exc
            yield
        except asyncio.CancelledError:
            closed = self._close_pre_effect_action(
                job_id,
                actor=actor,
                outcome_code="action_queue_cancelled_before_send",
            )
            if closed:
                # Convert cancellation only when the durable transition proves
                # the request never crossed into `running`. This lets the
                # approval envelope settle as failed instead of uncertain.
                raise HostCapabilityUnavailable("host action queue wait was cancelled before send") from None
            raise
        finally:
            if acquired:
                self._action_slots.release()

    async def run_prepared(
        self,
        prepared: PreparedHostAction,
        *,
        actor: ActorContext,
        approval_receipt_id: str | None = None,
    ) -> dict[str, Any]:
        job = prepared.job
        status = str(job.get("status") or "")
        if status in {"completed", "partial", "failed", "cancelled", "unknown", "reconciling"}:
            return await self.status(actor=actor, job_id=str(job["id"]))
        self._assert_current_target_policy(prepared.plan)
        if status not in {"planned", "awaiting_approval", "approved", "admitted"}:
            return await self.status(actor=actor, job_id=str(job["id"]))
        if status == "awaiting_approval" and approval_receipt_id is None:
            raise ContractError("host action still requires an exact approval")
        async with self._bounded_action_slot(str(job["id"]), actor=actor):
            durable = self._jobs.get(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
            )
            if (
                durable is None
                or durable.get("plan") != prepared.plan.to_payload()
                or durable.get("plan_digest") != prepared.plan.digest
                or durable.get("idempotency_key") != prepared.plan.idempotency_key
            ):
                raise HostActionUnknown("durable host action identity drifted before send")
            job = durable
            status = str(job.get("status") or "")
            if status in {
                "completed",
                "partial",
                "failed",
                "cancelled",
                "unknown",
                "reconciling",
                "running",
            }:
                return await self.status(actor=actor, job_id=str(job["id"]))
            if status == "awaiting_approval":
                if approval_receipt_id is None:
                    raise ContractError("host action still requires an exact approval")
                job = self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="awaiting_approval",
                    status="approved",
                    stage="approval",
                    outcome_code="approval_claimed",
                )
                status = "approved"
            if status in {"planned", "approved"}:
                job = self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status=status,
                    status="admitted",
                    stage="agent_admission",
                    outcome_code="request_prepared",
                )
                status = "admitted"
            if status != "admitted":
                return await self.status(actor=actor, job_id=str(job["id"]))
            # Recheck at the last backend-owned seam. A queued action must not
            # outlive an operator revoke merely because its plan was prepared.
            try:
                self._require_fresh_action_capability(actor, prepared.plan.security_id)
                self._assert_current_target_policy(prepared.plan)
            except AuthorizationError:
                self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="admitted",
                    status="failed",
                    stage="authorization",
                    outcome_code="authorization_revoked_before_send",
                    error_code="authorization_revoked_before_send",
                )
                raise
            except ContractError:
                self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="admitted",
                    status="failed",
                    stage="target_policy",
                    outcome_code="target_policy_revoked_before_send",
                    error_code="target_policy_changed",
                )
                raise
            try:
                network_approval_proof = self._issue_network_approval_proof_for_send(
                    prepared,
                    actor=actor,
                    approval_receipt_id=approval_receipt_id,
                )
            except ContractError:
                self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="admitted",
                    status="failed",
                    stage="approval",
                    outcome_code="network_approval_invalid_before_send",
                    error_code="network_approval_invalid_before_send",
                )
                raise
            except HostCapabilityUnavailable:
                self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="admitted",
                    status="failed",
                    stage="approval",
                    outcome_code="network_approval_unavailable_before_send",
                    error_code="network_approval_unavailable_before_send",
                )
                raise
            job = self._jobs.transition(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="admitted",
                status="running",
                stage="host_process",
                outcome_code="request_sent",
            )
            try:
                request_body = {"plan": prepared.plan.to_payload()}
                if network_approval_proof is not None:
                    request_body["network_approval_proof"] = network_approval_proof.to_payload()
                response = await self._client.call(
                    "RunAction",
                    request_body,
                    job_id=str(job["id"]),
                    actor_id=actor.user_id,
                    own_id=actor.own_id,
                    idempotency_key=prepared.plan.idempotency_key,
                    plan_digest=prepared.plan.digest,
                    approval_receipt_id=approval_receipt_id,
                    effectful=True,
                    timeout_sec=min(3_600.0, prepared.plan.timeout_sec + 30.0),
                )
            except HostControlUnavailable as exc:
                self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="running",
                    status="failed",
                    stage="agent_admission",
                    outcome_code="agent_unavailable_before_send",
                    error_code=exc.code,
                )
                raise HostCapabilityUnavailable("host agent is unavailable") from exc
            except HostControlOutcomeUnknown as exc:
                self._mark_unknown(job, actor, "agent_response_lost")
                raise HostActionUnknown("host action outcome is unknown") from exc
            except HostControlRejected as exc:
                if exc.code not in _KNOWN_PRE_ADMISSION_REJECTIONS:
                    self._mark_unknown(job, actor, "agent_rejected_after_send")
                    raise HostActionUnknown("host action outcome is unknown") from exc
                self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="running",
                    status="failed",
                    stage="agent_admission",
                    outcome_code="agent_rejected",
                    error_code=exc.code,
                )
                raise HostCapabilityUnavailable("host agent rejected the action plan") from exc
            except BaseException as exc:
                self._mark_unknown(job, actor, "backend_interrupted_after_admission")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise HostActionUnknown("host action outcome is unknown") from exc
        try:
            return self._settle_response(prepared, actor, job, response)
        except HostActionUnknown:
            raise
        except (ContractError, HostJobTransitionError, KeyError, TypeError, ValueError) as exc:
            self._mark_unknown(job, actor, "response_validation_failed")
            raise HostActionUnknown("host action response failed validation") from exc

    def request_action_approval(
        self,
        prepared: PreparedHostAction,
        *,
        actor: ActorContext,
    ) -> dict[str, Any]:
        job = prepared.job
        existing_id = str(job.get("approval_id") or "")
        if existing_id:
            existing = self._storage.get_action_approval(existing_id, actor.user_id)
            if existing is not None:
                return existing
            raise HostActionUnknown("durable host action approval disappeared")
        payload = {
            "job_id": str(job["id"]),
            "plan": prepared.plan.to_payload(),
            "plan_digest": prepared.plan.digest,
        }
        remaining_ttl = prepared.plan.expires_at - int(time.time())
        if remaining_ttl <= 0:
            raise ContractError("host action plan expired before approval")
        approval = self._storage.create_action_approval(
            actor.user_id,
            tool="host_action_execute",
            payload=payload,
            summary=host_action_approval_summary(prepared.plan),
            risk="high",
            requested_by=actor.own_id,
            conversation_id=prepared.plan.conversation_id,
            ttl_sec=remaining_ttl,
        )
        self._jobs.bind_approval(
            str(job["id"]),
            str(approval["id"]),
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
        )
        return approval

    def _issue_network_approval_proof_for_send(
        self,
        prepared: PreparedHostAction,
        *,
        actor: ActorContext,
        approval_receipt_id: str | None,
    ) -> NetworkApprovalProof | None:
        """Mint public-network authority only at the last pre-effect seam."""

        plan = prepared.plan
        if plan.adapter_id != "network.nmap":
            if approval_receipt_id is not None:
                raise ContractError("non-network action carried network approval authority")
            return None
        snapshot_payload = plan.target_snapshot
        if snapshot_payload is None:
            raise ContractError("network action lacks an exact target snapshot")
        snapshot = NetworkTargetSnapshot.from_payload(snapshot_payload)
        if not snapshot.approval_required:
            if approval_receipt_id is not None:
                raise ContractError("private network action cannot consume a public approval")
            return None
        if approval_receipt_id is None:
            raise ContractError("public network action lacks an exact approval")

        durable_job = self._jobs.get(
            str(prepared.job["id"]),
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
        )
        if (
            durable_job is None
            or durable_job.get("status") != "admitted"
            or str(durable_job.get("approval_id") or "") != approval_receipt_id
            or durable_job.get("plan") != plan.to_payload()
            or durable_job.get("plan_digest") != plan.digest
        ):
            raise ContractError("public network action authority drifted before send")
        approval = self._storage.get_action_approval(approval_receipt_id, actor.user_id)
        expected_approval_payload = {
            "job_id": str(durable_job["id"]),
            "plan": plan.to_payload(),
            "plan_digest": plan.digest,
        }
        approval_payload_digest = str((approval or {}).get("payload_hash") or "")
        if (
            not approval
            or approval.get("status") != "claimed"
            or approval.get("tool") != "host_action_execute"
            or approval.get("requested_by") != actor.own_id
            or approval.get("payload") != expected_approval_payload
            or re.fullmatch(r"[0-9a-f]{64}", approval_payload_digest) is None
        ):
            raise ContractError("public network approval drifted before send")

        current = int(time.time())
        proof_expires_at = min(plan.expires_at, current + 120)
        if proof_expires_at <= current:
            raise ContractError("network approval expired before agent admission")
        try:
            return self._network_approval_signer().issue(
                host_agent_id=plan.host_agent_id,
                approval_receipt_id=approval_receipt_id,
                approval_payload_digest=approval_payload_digest,
                plan_id=plan.plan_id,
                plan_digest=plan.digest,
                job_id=str(durable_job["id"]),
                execution_idempotency_key=plan.idempotency_key,
                actor_user_id=actor.user_id,
                actor_own_id=actor.own_id,
                issued_at=current,
                expires_at=proof_expires_at,
            )
        except (OSError, ValueError, BrokerContractError) as exc:
            raise HostCapabilityUnavailable("network approval signer is unavailable") from exc

    async def execute_approved_action(
        self,
        *,
        actor: ActorContext,
        job_id: str,
        plan: dict[str, Any],
        plan_digest: str,
    ) -> dict[str, Any]:
        job = self._jobs.get(job_id, user_id=actor.user_id, actor_own_id=actor.own_id)
        if job is None:
            raise ContractError("approved host action job is not executable")
        durable_plan = HostActionPlan.from_payload(job["plan"])
        supplied_plan = HostActionPlan.from_payload(plan)
        if (
            durable_plan != supplied_plan
            or durable_plan.digest != plan_digest
            or job.get("plan_digest") != plan_digest
        ):
            raise ContractError("approved host action plan drifted")
        status = str(job.get("status") or "")
        if status in {
            "completed",
            "partial",
            "failed",
            "cancelled",
            "unknown",
            "reconciling",
            "running",
        }:
            return await self.status(actor=actor, job_id=job_id)
        if status not in {"awaiting_approval", "approved", "admitted"}:
            raise ContractError("approved host action job is not executable")
        self._assert_current_target_policy(durable_plan)
        approval_id = str(job.get("approval_id") or "")
        approval = self._storage.get_action_approval(approval_id, actor.user_id)
        if (
            not approval
            or approval.get("status") != "claimed"
            or approval.get("tool") != "host_action_execute"
            or approval.get("requested_by") != actor.own_id
        ):
            raise ContractError("host action approval claim is not current")
        snapshot_payload = durable_plan.target_snapshot
        if snapshot_payload is None:
            raise ContractError("approved network action lacks a target snapshot")
        snapshot = NetworkTargetSnapshot.from_payload(snapshot_payload)
        if not snapshot.approval_required:
            raise ContractError("private network action cannot consume a public approval")
        approval_payload = approval.get("payload")
        expected_approval_payload = {
            "job_id": job_id,
            "plan": durable_plan.to_payload(),
            "plan_digest": durable_plan.digest,
        }
        approval_payload_digest = str(approval.get("payload_hash") or "")
        if approval_payload != expected_approval_payload or not re.fullmatch(
            r"[0-9a-f]{64}", approval_payload_digest
        ):
            raise ContractError("host action approval payload drifted")
        _states, attestations = await self._inventory()
        attestation = attestations.get(durable_plan.adapter_id)
        if attestation is None or attestation.digest != durable_plan.executable_attestation_digest:
            raise ContractError("host executable changed after approval")
        adapter = self._adapters.get(durable_plan.adapter_id)
        if adapter is None:
            raise ContractError("approved host adapter is unavailable")
        self._require_action_capability(actor, durable_plan.security_id)
        return await self.run_prepared(
            PreparedHostAction(durable_plan, adapter, attestation, job),
            actor=actor,
            approval_receipt_id=approval_id,
        )

    async def execute_approved_install(
        self,
        *,
        actor: ActorContext,
        job_id: str,
        package_plan: dict[str, Any],
        plan_digest: str,
    ) -> dict[str, Any]:
        job = self._jobs.get(job_id, user_id=actor.user_id, actor_own_id=actor.own_id)
        if job is None or job.get("status") != "awaiting_approval":
            raise ContractError("approved package acquisition is not executable")
        try:
            durable_plan = AptInstallPlan.from_payload(job["plan"])
            supplied_plan = AptInstallPlan.from_payload(package_plan)
        except BrokerContractError as exc:
            raise ContractError("approved package plan is invalid") from exc
        if (
            durable_plan != supplied_plan
            or durable_plan.digest != plan_digest
            or job.get("plan_digest") != plan_digest
            or durable_plan.continuation_work_item_id != job_id
            or durable_plan.actor_user_id != actor.user_id
            or durable_plan.actor_own_id != actor.own_id
        ):
            raise ContractError("approved package plan drifted")
        continuation = self._validate_install_continuation(job)
        approval_id = str(job.get("approval_id") or "")
        approval = self._storage.get_action_approval(approval_id, actor.user_id)
        if (
            not approval
            or approval.get("status") != "claimed"
            or approval.get("tool") != "software_install_execute"
            or approval.get("requested_by") != actor.own_id
        ):
            raise ContractError("package approval claim is not current")
        self._require_action_capability(actor, "host.packages.install")
        current = int(time.time())
        proof_expires_at = min(durable_plan.expires_at, current + 120)
        if proof_expires_at <= current:
            raise ContractError("package approval expired before broker admission")
        try:
            approval_proof = self._package_approval_signer().issue(
                broker_id=durable_plan.broker_id,
                approval_receipt_id=approval_id,
                approval_payload_digest=str(approval.get("payload_hash") or ""),
                plan_id=durable_plan.plan_id,
                plan_digest=durable_plan.digest,
                actor_user_id=actor.user_id,
                actor_own_id=actor.own_id,
                continuation_work_item_id=job_id,
                execution_idempotency_key=str(job["idempotency_key"]),
                issued_at=current,
                expires_at=proof_expires_at,
            )
        except (OSError, ValueError, BrokerContractError) as exc:
            raise HostCapabilityUnavailable("package approval signer is unavailable") from exc
        job = self._jobs.transition(
            job_id,
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status="awaiting_approval",
            status="approved",
            stage="approval",
            outcome_code="approval_claimed",
        )
        job = self._jobs.transition(
            job_id,
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status="approved",
            status="admitted",
            stage="package_broker",
            outcome_code="exact_plan_admitted",
        )
        job = self._jobs.transition(
            job_id,
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status="admitted",
            status="running",
            stage="package_transaction",
            outcome_code="request_sent",
        )
        try:
            response = await self._client.call(
                "PackageExecuteInstall",
                {
                    "approval_proof": approval_proof.to_payload(),
                    "plan_id": durable_plan.plan_id,
                },
                job_id=job_id,
                actor_id=actor.user_id,
                own_id=actor.own_id,
                idempotency_key=str(job["idempotency_key"]),
                plan_digest=durable_plan.digest,
                approval_receipt_id=approval_id,
                effectful=True,
                timeout_sec=3_600.0,
            )
        except HostControlOutcomeUnknown as exc:
            self._mark_unknown(job, actor, "package_response_lost")
            raise HostActionUnknown("package transaction outcome is unknown") from exc
        except HostControlUnavailable as exc:
            self._jobs.transition(
                job_id,
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="running",
                status="failed",
                stage="package_broker",
                outcome_code="agent_unavailable_before_send",
                error_code=exc.code,
            )
            raise HostCapabilityUnavailable("host agent is unavailable") from exc
        except HostControlRejected as exc:
            self._jobs.transition(
                job_id,
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="running",
                status="failed",
                stage="package_broker",
                outcome_code="package_plan_rejected",
                error_code=exc.code,
            )
            raise HostCapabilityUnavailable("package broker rejected the exact plan") from exc
        except BaseException as exc:
            self._mark_unknown(job, actor, "backend_interrupted_after_package_admission")
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HostActionUnknown("package transaction outcome is unknown") from exc
        if response.get("status") == "unknown" and response.get("receipt") is None:
            self._mark_unknown(job, actor, "package_broker_outcome_unknown")
            raise HostActionUnknown("package transaction outcome is unknown")
        if response.get("status") == "failed_before_effect" and response.get("receipt") is None:
            try:
                error_code = self._validate_package_pre_effect_failure_record(
                    response,
                    plan=durable_plan,
                )
            except (ContractError, KeyError, TypeError, ValueError) as exc:
                self._mark_unknown(job, actor, "package_response_invalid")
                raise HostActionUnknown("package transaction response failed validation") from exc
            failed = self._jobs.transition(
                job_id,
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="running",
                status="failed",
                stage="package_transaction",
                outcome_code="failed_before_effect",
                error_code=error_code,
            )
            return {
                "error_code": error_code,
                "job_id": job_id,
                "package_outcome": "failed_before_effect",
                "status": failed["status"],
            }
        try:
            receipt = self._validate_package_execution_record(
                response,
                plan=durable_plan,
                approval_id=approval_id,
                idempotency_key=str(job["idempotency_key"]),
            )
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            self._mark_unknown(job, actor, "package_response_invalid")
            raise HostActionUnknown("package transaction response failed validation") from exc
        receipt_digest = hashlib.sha256(receipt.canonical_bytes()).hexdigest()
        receipt_ref = f"broker:{receipt.transaction_id}"
        if receipt.outcome is TransactionOutcome.FAILED_BEFORE_EFFECT:
            failed = self._jobs.transition(
                job_id,
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="running",
                status="failed",
                stage="package_transaction",
                outcome_code=receipt.outcome.value,
                receipt_ref=receipt_ref,
                receipt_digest=receipt_digest,
                error_code=receipt.error_code or "package_failed_before_effect",
            )
            return {
                "error_code": failed["error_code"],
                "job_id": job_id,
                "package_outcome": receipt.outcome.value,
                "receipt_digest": receipt_digest,
                "status": failed["status"],
            }
        if receipt.outcome is TransactionOutcome.CANCELLED_BEFORE_COMMIT:
            cancelled = self._jobs.transition(
                job_id,
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="running",
                status="cancelled",
                stage="package_transaction",
                outcome_code=receipt.outcome.value,
                receipt_ref=receipt_ref,
                receipt_digest=receipt_digest,
                error_code="cancelled_before_commit",
            )
            return {
                "job_id": job_id,
                "package_outcome": receipt.outcome.value,
                "receipt_digest": receipt_digest,
                "status": cancelled["status"],
            }
        if receipt.outcome is TransactionOutcome.UNKNOWN:
            self._mark_unknown(job, actor, "package_receipt_outcome_unknown")
            raise HostActionUnknown("package transaction requires reconciliation")

        return await self._finalize_install_success(
            actor=actor,
            job=job,
            continuation=continuation,
            receipt=receipt,
            expected_status="running",
            reconciled=False,
        )

    def _package_approval_signer(self) -> PackageApprovalSigner:
        signer = self._package_approval_signer_cache
        if signer is not None:
            return signer
        path = getattr(self._settings, "host_approval_signing_key_file", None)
        if path is None:
            raise ContractError("package approval signing key is not configured")
        signer = PackageApprovalSigner(load_backend_approval_signing_key(path))
        self._package_approval_signer_cache = signer
        return signer

    def _network_approval_signer(self) -> NetworkApprovalSigner:
        signer = self._network_approval_signer_cache
        if signer is not None:
            return signer
        path = getattr(self._settings, "host_approval_signing_key_file", None)
        if path is None:
            raise ContractError("network approval signing key is not configured")
        signer = NetworkApprovalSigner(load_backend_approval_signing_key(path))
        self._network_approval_signer_cache = signer
        return signer

    @staticmethod
    def _validate_install_continuation(job: dict[str, Any]) -> dict[str, Any]:
        continuation = job.get("continuation")
        expected_fields = {
            "action_id",
            "action_job_id",
            "capability_id",
            "conversation_id",
            "kind",
            "ports",
            "source_message_id",
            "target_snapshot",
            "targets",
        }
        if not isinstance(continuation, dict) or set(continuation) != expected_fields:
            raise ContractError("package continuation is invalid")
        if (
            continuation.get("kind") != "install_then_host_action"
            or continuation.get("capability_id") != "network.nmap.scan"
            or continuation.get("action_id") not in {"discover", "services", "selected_ports"}
            or not isinstance(continuation.get("conversation_id"), str)
            or continuation.get("conversation_id") != job.get("conversation_id")
            or not isinstance(continuation.get("source_message_id"), str)
            or continuation.get("source_message_id") != job.get("source_message_id")
            or not isinstance(continuation.get("action_job_id"), str)
            or re.fullmatch(r"hjob_[0-9a-f]{32}", continuation["action_job_id"]) is None
            or continuation["action_job_id"] == job.get("id")
            or not isinstance(continuation.get("target_snapshot"), dict)
        ):
            raise ContractError("package continuation identity is invalid")
        targets = continuation.get("targets")
        if (
            not isinstance(targets, list)
            or not 1 <= len(targets) <= 16
            or any(not isinstance(item, str) or not 1 <= len(item) <= 253 for item in targets)
        ):
            raise ContractError("package continuation targets are invalid")
        ports = continuation.get("ports")
        if ports is not None and (
            not isinstance(ports, list)
            or len(ports) > 64
            or any(
                isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 65_535
                for item in ports
            )
        ):
            raise ContractError("package continuation ports are invalid")
        if continuation["action_id"] == "selected_ports" and not ports:
            raise ContractError("selected-port continuation lacks ports")
        if continuation["action_id"] != "selected_ports" and ports is not None:
            raise ContractError("package continuation has unexpected ports")
        normalized = job.get("normalized_arguments")
        digest = hashlib.sha256(canonical_json_bytes(continuation)).hexdigest()
        if (
            not isinstance(normalized, dict)
            or set(normalized) != {"continuation_digest", "requested"}
            or not isinstance(normalized.get("continuation_digest"), str)
            or not hmac.compare_digest(normalized["continuation_digest"], digest)
        ):
            raise ContractError("package continuation digest is invalid")
        return continuation

    async def _finalize_install_success(
        self,
        *,
        actor: ActorContext,
        job: dict[str, Any],
        continuation: dict[str, Any],
        receipt: PackageTransactionReceipt,
        expected_status: str,
        reconciled: bool,
    ) -> dict[str, Any]:
        if receipt.schema_version != BROKER_RECEIPT_SCHEMA_VERSION:
            raise ContractError("legacy package receipt cannot activate a capability")
        receipt_digest = hashlib.sha256(receipt.canonical_bytes()).hexdigest()
        return await self._activate_and_resume_after_install(
            actor=actor,
            job=job,
            continuation=continuation,
            expected_status=expected_status,
            reconciled=reconciled,
            receipt_ref=f"broker:{receipt.transaction_id}",
            receipt_digest=receipt_digest,
            package_evidence={
                "package_outcome": receipt.outcome.value,
                "receipt_digest": receipt_digest,
                "reboot_required": receipt.reboot_required,
            },
        )

    async def _finalize_reconciled_install_success(
        self,
        *,
        actor: ActorContext,
        job: dict[str, Any],
        continuation: dict[str, Any],
        receipt: PackageReconciliationReceipt,
        expected_status: str,
    ) -> dict[str, Any]:
        if (
            receipt.transaction_outcome is not TransactionOutcome.UNKNOWN
            or receipt.postcondition_state is not PackagePostconditionState.DESIRED
            or not receipt.postcondition_satisfied
        ):
            raise ContractError("package reconciliation does not prove the desired postcondition")
        receipt_digest = hashlib.sha256(receipt.canonical_bytes()).hexdigest()
        return await self._activate_and_resume_after_install(
            actor=actor,
            job=job,
            continuation=continuation,
            expected_status=expected_status,
            reconciled=True,
            receipt_ref=f"broker-reconciliation:{receipt.reconciliation_id}",
            receipt_digest=receipt_digest,
            package_evidence={
                "package_outcome": TransactionOutcome.UNKNOWN.value,
                "postcondition_satisfied": True,
                "reconciliation_state": receipt.postcondition_state.value,
                "receipt_digest": receipt_digest,
            },
        )

    async def _activate_and_resume_after_install(
        self,
        *,
        actor: ActorContext,
        job: dict[str, Any],
        continuation: dict[str, Any],
        expected_status: str,
        reconciled: bool,
        receipt_ref: str,
        receipt_digest: str,
        package_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            states, attestations = await self._inventory()
            adapter_id = BUILTIN_CATALOG.describe(
                str(continuation["capability_id"]),
                entries=BUILTIN_CATALOG.entries(
                    adapter_states=states,
                    attestation_digests={key: value.digest for key, value in attestations.items()},
                ),
            ).adapter_id
            capability_activated = (
                states.get(adapter_id) is AdapterState.AVAILABLE and adapter_id in attestations
            )
        except (ContractError, HostCapabilityUnavailable, KeyError, TypeError, ValueError):
            capability_activated = False
        observed_outcome = "completed" if capability_activated else "partial"
        terminal = self._jobs.transition(
            str(job["id"]),
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status=expected_status,
            status="reconciled" if reconciled else observed_outcome,
            stage="reconciliation" if reconciled else "post_install_attestation",
            outcome_code=("capability_activated" if capability_activated else "adapter_unavailable"),
            receipt_ref=receipt_ref,
            receipt_digest=receipt_digest,
            error_code=None if capability_activated else "post_install_attestation_failed",
        )
        package_result = {
            "capability_activated": capability_activated,
            "job_id": job["id"],
            "package_status": terminal["status"],
            "terminal_outcome": observed_outcome,
            **package_evidence,
        }
        if not capability_activated:
            return {**package_result, "status": terminal["status"]}
        try:
            resumed = await self.prepare_network_action(
                actor=actor,
                capability_id=str(continuation["capability_id"]),
                action_id=str(continuation["action_id"]),
                targets=list(continuation["targets"]),
                ports=None if continuation["ports"] is None else list(continuation["ports"]),
                conversation_id=str(continuation["conversation_id"]),
                source_message_id=str(continuation["source_message_id"]),
                expected_target_snapshot=continuation["target_snapshot"],
            )
        except (AuthorizationError, ContractError, HostCapabilityUnavailable) as exc:
            return {
                **package_result,
                "resumed": {
                    "error_code": (
                        "target_or_policy_drift"
                        if isinstance(exc, ContractError)
                        else "host_action_unavailable"
                    ),
                    "status": "failed",
                },
                "status": "partial",
            }
        if isinstance(resumed, dict):
            return {**package_result, "resumed": resumed, "status": "partial"}
        if resumed.job.get("id") != continuation["action_job_id"]:
            return {
                **package_result,
                "resumed": {"error_code": "action_identity_drift", "status": "failed"},
                "status": "partial",
            }
        if resumed.job.get("status") == "awaiting_approval":
            action_approval = self.request_action_approval(resumed, actor=actor)
            return {
                **package_result,
                "resumed": {
                    "approval_id": action_approval["id"],
                    "job_id": resumed.job["id"],
                    "status": "awaiting_approval",
                    "summary": action_approval["summary"],
                },
                "status": terminal["status"],
            }
        try:
            action_result = await self.run_prepared(resumed, actor=actor)
        except HostActionUnknown:
            return {
                **package_result,
                "resumed": {"job_id": resumed.job["id"], "status": "unknown"},
                "status": "unknown",
            }
        except (AuthorizationError, ContractError, HostCapabilityUnavailable):
            return {
                **package_result,
                "resumed": {"job_id": resumed.job["id"], "status": "failed"},
                "status": "partial",
            }
        return {**package_result, "resumed": action_result, "status": action_result["status"]}

    @staticmethod
    def _validate_package_pre_effect_failure_record(
        response: dict[str, Any],
        *,
        plan: AptInstallPlan,
    ) -> str:
        """Accept only the broker's receiptless, never-claimed expiry result."""

        updated_at = response.get("updated_at")
        if (
            set(response) != _PACKAGE_RECORD_FIELDS
            or response.get("status") != "failed_before_effect"
            or response.get("error_code") != "plan_expired"
            or response.get("plan_id") != plan.plan_id
            or response.get("plan_digest") != plan.digest
            or response.get("transaction_digest") != plan.transaction.digest
            or response.get("expires_at") != plan.expires_at
            or not isinstance(response.get("idempotent"), bool)
            or response.get("transaction_id") is not None
            or response.get("execution_started_at") is not None
            or response.get("receipt") is not None
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, int)
            or updated_at < plan.created_at
        ):
            raise ContractError("package pre-effect failure response is invalid")
        return "plan_expired"

    @staticmethod
    def _validate_package_execution_record(
        response: dict[str, Any],
        *,
        plan: AptInstallPlan,
        approval_id: str,
        idempotency_key: str,
    ) -> PackageTransactionReceipt:
        if set(response) != _PACKAGE_RECORD_FIELDS or response.get("plan_id") != plan.plan_id:
            raise ContractError("package execution response is invalid")
        raw_receipt = response.get("receipt")
        if not isinstance(raw_receipt, dict):
            raise ContractError("package execution receipt is absent")
        try:
            receipt = PackageTransactionReceipt.from_payload(raw_receipt)
        except BrokerContractError as exc:
            raise ContractError("package execution receipt is invalid") from exc
        if receipt.schema_version != BROKER_RECEIPT_SCHEMA_VERSION:
            raise ContractError("legacy package receipt cannot activate a capability")
        status = response.get("status")
        expected_status = {
            TransactionOutcome.COMPLETED: "completed",
            TransactionOutcome.ALREADY_SATISFIED: "completed",
            TransactionOutcome.FAILED_BEFORE_EFFECT: "failed_before_effect",
            TransactionOutcome.CANCELLED_BEFORE_COMMIT: "cancelled_before_commit",
            TransactionOutcome.UNKNOWN: "unknown",
        }[receipt.outcome]
        execution_started_at = response.get("execution_started_at")
        updated_at = response.get("updated_at")
        if (
            response.get("plan_digest") != plan.digest
            or response.get("transaction_digest") != plan.transaction.digest
            or response.get("transaction_id") != receipt.transaction_id
            or status != expected_status
            or response.get("expires_at") != plan.expires_at
            or not isinstance(response.get("idempotent"), bool)
            or isinstance(execution_started_at, bool)
            or not isinstance(execution_started_at, int)
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, int)
            or receipt.plan_id != plan.plan_id
            or receipt.broker_id != plan.broker_id
            or receipt.approved_plan_digest != plan.digest
            or receipt.executed_transaction_digest != plan.transaction.digest
            or receipt.approval_receipt_id != approval_id
            or receipt.idempotency_key != idempotency_key
            or receipt.error_code != response.get("error_code")
            or receipt.started_at < execution_started_at
            or receipt.finished_at > updated_at
        ):
            raise ContractError("package execution receipt drifted from approval")
        if receipt.outcome in {TransactionOutcome.COMPLETED, TransactionOutcome.ALREADY_SATISFIED}:
            installed = {(item.name, item.architecture): item.version for item in receipt.after}
            if any(
                installed.get((item.name, item.architecture or "")) != item.version
                for item in plan.transaction.requested
            ):
                raise ContractError("package execution postcondition is incomplete")
        return receipt

    def _settle_response(
        self,
        prepared: PreparedHostAction,
        actor: ActorContext,
        job: dict[str, Any],
        response: dict[str, Any],
        *,
        expected_job_status: str = "running",
        reconciled: bool = False,
    ) -> dict[str, Any]:
        if response.get("job_id") != job["id"]:
            self._mark_unknown(job, actor, "job_identity_mismatch")
            raise HostActionUnknown("host response names another job")
        raw_receipt = response.get("receipt")
        if not isinstance(raw_receipt, dict):
            status = str(response.get("status") or "")
            if status == "failed" and response.get("error_code") == "runner_unavailable":
                terminal = self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status=expected_job_status,
                    status="reconciled" if reconciled else "failed",
                    stage="host_process",
                    outcome_code="runner_unavailable",
                    error_code="runner_unavailable",
                )
                return {
                    "error_code": "runner_unavailable",
                    "job_id": terminal["id"],
                    "status": terminal["status"],
                    "terminal_outcome": "failed",
                }
            self._mark_unknown(job, actor, "receipt_missing")
            raise HostActionUnknown("host response lacks a verifiable receipt")
        receipt = HostActionReceipt.from_payload(raw_receipt)
        execution = prepared.adapter.build_execution(prepared.plan, prepared.attestation)
        verification = verify_action_receipt(
            receipt,
            plan=prepared.plan,
            execution=execution,
            signature_verifier=self._client.verify_receipt_signature,
        )
        claimed_status = str(response.get("status") or "")
        expected_status = {
            "succeeded": "completed",
            "partial": "partial",
            "failed": "failed",
            "cancelled": "cancelled",
            "unknown": "unknown",
        }[verification.outcome.value]
        if claimed_status != expected_status:
            self._mark_unknown(job, actor, "outcome_mismatch")
            raise HostActionUnknown("host receipt outcome and response disagree")
        evidence_paths = response.get("evidence_paths")
        if not isinstance(evidence_paths, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or _SAFE_REF.fullmatch(value) is None
            for key, value in evidence_paths.items()
        ):
            self._mark_unknown(job, actor, "evidence_paths_invalid")
            raise HostActionUnknown("host evidence references are invalid")
        receipt_ref = str(response.get("receipt_path") or "")
        if _SAFE_REF.fullmatch(receipt_ref) is None:
            self._mark_unknown(job, actor, "receipt_path_invalid")
            raise HostActionUnknown("host receipt reference is invalid")
        try:
            verified_evidence = self._verify_evidence_files(
                str(job["id"]), receipt, evidence_paths, receipt_ref
            )
        except (ContractError, OSError, ValueError) as exc:
            self._mark_unknown(job, actor, "evidence_verification_failed")
            raise HostActionUnknown("host evidence files failed verification") from exc
        result_ref = ""
        parsed_payload: bytes | None = None
        for evidence in receipt.evidence:
            if receipt.parsed_result_digest == evidence.sha256:
                result_ref = str(evidence_paths.get(evidence.evidence_id) or "")
                parsed_payload = verified_evidence.get(evidence.evidence_id)
                break
        if receipt.parsed_result_digest is not None and (
            _SAFE_REF.fullmatch(result_ref) is None or parsed_payload is None
        ):
            self._mark_unknown(job, actor, "parsed_result_evidence_missing")
            raise HostActionUnknown("parsed host result lacks matching evidence")
        if parsed_payload is None:
            self._mark_unknown(job, actor, "parsed_result_evidence_missing")
            raise HostActionUnknown("parsed host result is absent")
        decoded = decode_canonical_json(parsed_payload)
        parsed = ParsedActionResult.from_payload(decoded)
        if parsed.digest != receipt.parsed_result_digest:
            self._mark_unknown(job, actor, "parsed_result_digest_mismatch")
            raise HostActionUnknown("parsed host result identity is invalid")
        projection = project_action_result(parsed)
        claimed_projection = response.get("result")
        if not isinstance(claimed_projection, dict) or not hmac.compare_digest(
            canonical_json_bytes(claimed_projection), canonical_json_bytes(projection)
        ):
            self._mark_unknown(job, actor, "result_projection_mismatch")
            raise HostActionUnknown("host result projection is invalid")
        terminal = self._jobs.transition(
            str(job["id"]),
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status=expected_job_status,
            status="reconciled" if reconciled else expected_status,
            stage="receipt",
            outcome_code=verification.outcome.value,
            systemd_unit=receipt.process.unit_id,
            result_ref=result_ref or None,
            receipt_ref=receipt_ref,
            receipt_digest=verification.receipt_digest,
            error_code=None if expected_status in {"completed", "partial"} else expected_status,
        )
        settled: dict[str, Any] = {
            "coverage": projection.get("coverage"),
            "evidence": projection.get("evidence"),
            "job_id": terminal["id"],
            "parser_status": projection.get("parser_status"),
            "receipt_digest": verification.receipt_digest,
            "result": projection.get("result"),
            "status": terminal["status"],
            "terminal_outcome": expected_status,
            "warnings": projection.get("warnings"),
        }
        output_attachment = _verified_jq_output_attachment(
            prepared=prepared,
            receipt=receipt,
            parsed=parsed,
            verified_evidence=verified_evidence,
            terminal_status=expected_status,
            receipt_digest=verification.receipt_digest,
            job_id=str(terminal["id"]),
        )
        if output_attachment is not None:
            settled["_attachment"] = output_attachment
        return settled

    def _mark_unknown(self, job: dict[str, Any], actor: ActorContext, code: str) -> None:
        with contextlib.suppress(HostJobTransitionError):
            self._jobs.transition(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="running",
                status="unknown",
                stage="reconciliation",
                outcome_code=code,
                error_code=code,
            )

    def _restore_unknown(self, job: dict[str, Any], actor: ActorContext, code: str) -> None:
        with contextlib.suppress(HostJobTransitionError):
            self._jobs.transition(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="reconciling",
                status="unknown",
                stage="reconciliation",
                outcome_code=code,
                error_code=code,
            )

    def _settle_status_response(
        self,
        *,
        actor: ActorContext,
        job: dict[str, Any],
        plan: HostActionPlan,
        response: dict[str, Any],
        expected_job_status: str,
    ) -> dict[str, Any] | None:
        if response.get("job_id") != job["id"]:
            if expected_job_status == "reconciling":
                self._restore_unknown(job, actor, "job_identity_mismatch")
            else:
                self._mark_unknown(job, actor, "job_identity_mismatch")
            raise HostActionUnknown("host status response names another job")
        raw_receipt = response.get("receipt")
        if isinstance(raw_receipt, dict):
            receipt = HostActionReceipt.from_payload(raw_receipt)
            adapter = self._adapters.get(plan.adapter_id)
            if adapter is None:
                raise HostActionUnknown("durable host adapter is unavailable")
            return self._settle_response(
                PreparedHostAction(plan, adapter, receipt.executable_attestation, job),
                actor,
                job,
                response,
                expected_job_status=expected_job_status,
                reconciled=expected_job_status == "reconciling",
            )
        agent_status = str(response.get("status") or "")
        if agent_status == "cancelled" and response.get("cancelled") is True:
            terminal = self._jobs.transition(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status=expected_job_status,
                status="reconciled" if expected_job_status == "reconciling" else "cancelled",
                stage="reconciliation" if expected_job_status == "reconciling" else "cancellation",
                outcome_code="cancellation_observed",
                error_code="cancelled",
            )
            return {
                "job_id": terminal["id"],
                "status": terminal["status"],
                "terminal_outcome": "cancelled",
            }
        if agent_status == "failed" and response.get("error_code") == "runner_unavailable":
            terminal = self._jobs.transition(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status=expected_job_status,
                status="reconciled" if expected_job_status == "reconciling" else "failed",
                stage="reconciliation" if expected_job_status == "reconciling" else "host_process",
                outcome_code="runner_unavailable",
                error_code="runner_unavailable",
            )
            return {
                "error_code": "runner_unavailable",
                "job_id": terminal["id"],
                "status": terminal["status"],
                "terminal_outcome": "failed",
            }
        if agent_status in {"admitted", "running", "unknown"}:
            if expected_job_status == "reconciling":
                self._restore_unknown(job, actor, "agent_outcome_still_unknown")
            elif agent_status == "unknown":
                self._mark_unknown(job, actor, "agent_outcome_unknown")
            return None
        if expected_job_status == "reconciling":
            self._restore_unknown(job, actor, "agent_status_invalid")
        else:
            self._mark_unknown(job, actor, "agent_status_invalid")
        raise HostActionUnknown("host status response is invalid")

    async def _package_status(
        self,
        *,
        actor: ActorContext,
        job: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            plan = AptInstallPlan.from_payload(job["plan"])
        except (BrokerContractError, KeyError, TypeError) as exc:
            raise ContractError("durable package plan is invalid") from exc
        continuation = self._validate_install_continuation(job)
        local_status = str(job["status"])
        if local_status in {"completed", "partial", "failed", "cancelled", "reconciled"}:
            return self._status_projection(job, agent=None)
        if local_status in {"planned", "awaiting_approval", "approved", "admitted"}:
            return self._status_projection(job, agent=None)
        if local_status == "unknown":
            try:
                job = self._jobs.transition(
                    str(job["id"]),
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="unknown",
                    status="reconciling",
                    stage="package_reconciliation",
                    outcome_code="status_requested",
                )
            except HostJobTransitionError:
                refreshed = self._jobs.get(str(job["id"]), user_id=actor.user_id, actor_own_id=actor.own_id)
                if refreshed is None:
                    raise ContractError("package job was not found") from None
                return self._status_projection(refreshed, agent=None)
            local_status = "reconciling"
        try:
            response = await self._client.call(
                "PackageStatus",
                {"plan_id": plan.plan_id},
                job_id=str(job["id"]),
                actor_id=actor.user_id,
                own_id=actor.own_id,
                idempotency_key=str(job["idempotency_key"]),
                plan_digest="0" * 64,
                effectful=False,
            )
        except HostControlClientError:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "package_reconciliation_unavailable")
                refreshed = self._jobs.get(str(job["id"]), user_id=actor.user_id, actor_own_id=actor.own_id)
                assert refreshed is not None
                job = refreshed
            return self._status_projection(job, agent=None)
        if response.get("status") == "unknown" and set(response) == {
            "error_code",
            "job_id",
            "status",
        }:
            if response.get("job_id") != job["id"]:
                self._restore_or_mark_package_unknown(
                    job, actor, local_status, "package_status_identity_mismatch"
                )
                raise HostActionUnknown("package status response names another job")
            self._restore_or_mark_package_unknown(job, actor, local_status, "package_broker_outcome_unknown")
            refreshed = self._jobs.get(str(job["id"]), user_id=actor.user_id, actor_own_id=actor.own_id)
            assert refreshed is not None
            return self._status_projection(refreshed, agent=response)
        if (
            local_status == "reconciling"
            and response.get("status") == "unknown"
            and response.get("error_code") == "broker_restart_after_effect_claim"
            and response.get("receipt") is None
        ):
            return await self._reconcile_package_restart(
                actor=actor,
                job=job,
                plan=plan,
                continuation=continuation,
            )
        try:
            receipt = self._validate_package_status_record(response, plan=plan, job=job)
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            self._restore_or_mark_package_unknown(job, actor, local_status, "package_status_response_invalid")
            raise HostActionUnknown("package status response failed validation") from exc
        broker_status = str(response["status"])
        if broker_status == "completed":
            assert receipt is not None
            return await self._finalize_install_success(
                actor=actor,
                job=job,
                continuation=continuation,
                receipt=receipt,
                expected_status=local_status,
                reconciled=local_status == "reconciling",
            )
        if broker_status in {"failed_before_effect", "cancelled_before_commit", "planned"}:
            terminal_outcome = (
                "failed" if broker_status in {"failed_before_effect", "planned"} else "cancelled"
            )
            receipt_digest = (
                hashlib.sha256(receipt.canonical_bytes()).hexdigest() if receipt is not None else None
            )
            terminal = self._jobs.transition(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status=local_status,
                status="reconciled" if local_status == "reconciling" else terminal_outcome,
                stage="package_reconciliation",
                outcome_code=("transaction_not_started" if broker_status == "planned" else broker_status),
                receipt_ref=(f"broker:{receipt.transaction_id}" if receipt is not None else None),
                receipt_digest=receipt_digest,
                error_code=(
                    str(response.get("error_code") or "package_failed_before_effect")
                    if terminal_outcome == "failed"
                    else "cancelled_before_commit"
                ),
            )
            return {
                "agent": response,
                "job_id": terminal["id"],
                "status": terminal["status"],
                "terminal_outcome": terminal_outcome,
            }
        if broker_status == "executing":
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "package_transaction_in_progress")
        else:
            self._restore_or_mark_package_unknown(job, actor, local_status, "package_broker_outcome_unknown")
        refreshed = self._jobs.get(str(job["id"]), user_id=actor.user_id, actor_own_id=actor.own_id)
        assert refreshed is not None
        return self._status_projection(refreshed, agent=response)

    async def _reconcile_package_restart(
        self,
        *,
        actor: ActorContext,
        job: dict[str, Any],
        plan: AptInstallPlan,
        continuation: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await self._client.call(
                "PackageReconcileInstall",
                {"plan_id": plan.plan_id},
                job_id=str(job["id"]),
                actor_id=actor.user_id,
                own_id=actor.own_id,
                idempotency_key=str(job["idempotency_key"]),
                plan_digest=plan.digest,
                effectful=False,
            )
            receipt = self._validate_package_reconciliation_record(
                response,
                plan=plan,
                job=job,
            )
        except (HostControlClientError, ContractError, KeyError, TypeError, ValueError):
            self._restore_unknown(job, actor, "package_reconciliation_unavailable")
            refreshed = self._jobs.get(str(job["id"]), user_id=actor.user_id, actor_own_id=actor.own_id)
            assert refreshed is not None
            return self._status_projection(refreshed, agent=None)
        if receipt.postcondition_state is PackagePostconditionState.DESIRED:
            return await self._finalize_reconciled_install_success(
                actor=actor,
                job=job,
                continuation=continuation,
                receipt=receipt,
                expected_status="reconciling",
            )
        receipt_digest = hashlib.sha256(receipt.canonical_bytes()).hexdigest()
        if receipt.postcondition_state is PackagePostconditionState.PRE_STATE:
            terminal = self._jobs.transition(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="reconciling",
                status="reconciled",
                stage="package_reconciliation",
                outcome_code="no_desired_postcondition",
                receipt_ref=f"broker-reconciliation:{receipt.reconciliation_id}",
                receipt_digest=receipt_digest,
                error_code=None,
            )
            return {
                "agent": response,
                "job_id": terminal["id"],
                "package_outcome": TransactionOutcome.UNKNOWN.value,
                "postcondition_satisfied": False,
                "receipt_digest": receipt_digest,
                "safe_to_replan": True,
                "status": terminal["status"],
                "terminal_outcome": "unknown",
            }
        self._restore_unknown(job, actor, receipt.error_code or "package_state_unavailable")
        refreshed = self._jobs.get(str(job["id"]), user_id=actor.user_id, actor_own_id=actor.own_id)
        assert refreshed is not None
        return self._status_projection(refreshed, agent=response)

    def _restore_or_mark_package_unknown(
        self,
        job: dict[str, Any],
        actor: ActorContext,
        local_status: str,
        code: str,
    ) -> None:
        if local_status == "reconciling":
            self._restore_unknown(job, actor, code)
        else:
            self._mark_unknown(job, actor, code)

    def _validate_package_status_record(
        self,
        response: dict[str, Any],
        *,
        plan: AptInstallPlan,
        job: dict[str, Any],
    ) -> PackageTransactionReceipt | None:
        if set(response) != _PACKAGE_RECORD_FIELDS:
            raise ContractError("package status response fields are invalid")
        status = response.get("status")
        transaction_id = response.get("transaction_id")
        execution_started_at = response.get("execution_started_at")
        error_code = response.get("error_code")
        if (
            status
            not in {
                "planned",
                "executing",
                "completed",
                "cancelled_before_commit",
                "failed_before_effect",
                "unknown",
            }
            or response.get("plan_id") != plan.plan_id
            or response.get("plan_digest") != plan.digest
            or response.get("transaction_digest") != plan.transaction.digest
            or response.get("expires_at") != plan.expires_at
            or not isinstance(response.get("idempotent"), bool)
            or isinstance(response.get("updated_at"), bool)
            or not isinstance(response.get("updated_at"), int)
            or (transaction_id is None) != (execution_started_at is None)
        ):
            raise ContractError("package status response identity is invalid")
        if transaction_id is not None and (
            not isinstance(transaction_id, str)
            or isinstance(execution_started_at, bool)
            or not isinstance(execution_started_at, int)
        ):
            raise ContractError("package status execution identity is invalid")
        raw_receipt = response.get("receipt")
        if status in {"planned", "cancelled_before_commit"} and (
            transaction_id is not None or raw_receipt is not None or error_code is not None
        ):
            raise ContractError("unexecuted package status carries execution evidence")
        if status == "executing" and (
            transaction_id is None or raw_receipt is not None or error_code is not None
        ):
            raise ContractError("executing package status is invalid")
        if status == "completed" and raw_receipt is None:
            raise ContractError("completed package status lacks a receipt")
        if status == "failed_before_effect" and not isinstance(error_code, str):
            raise ContractError("failed package status lacks an error")
        if status == "unknown" and (transaction_id is None or not isinstance(error_code, str)):
            raise ContractError("unknown package status lacks durable attempt evidence")
        if raw_receipt is None:
            return None
        approval_id = str(job.get("approval_id") or "")
        if not approval_id:
            raise ContractError("package receipt has no approval identity")
        return self._validate_package_execution_record(
            response,
            plan=plan,
            approval_id=approval_id,
            idempotency_key=str(job["idempotency_key"]),
        )

    @staticmethod
    def _validate_package_reconciliation_record(
        response: dict[str, Any],
        *,
        plan: AptInstallPlan,
        job: dict[str, Any],
    ) -> PackageReconciliationReceipt:
        if (
            set(response) != _PACKAGE_RECONCILIATION_FIELDS
            or response.get("status") != "unknown"
            or response.get("error_code") != "broker_restart_after_effect_claim"
            or response.get("plan_id") != plan.plan_id
            or response.get("plan_digest") != plan.digest
            or response.get("transaction_digest") != plan.transaction.digest
            or not isinstance(response.get("idempotent"), bool)
            or isinstance(response.get("updated_at"), bool)
            or not isinstance(response.get("updated_at"), int)
        ):
            raise ContractError("package reconciliation response is invalid")
        try:
            receipt = PackageReconciliationReceipt.from_payload(response["reconciliation"])
        except BrokerContractError as exc:
            raise ContractError("package reconciliation receipt is invalid") from exc
        approval_id = str(job.get("approval_id") or "")
        if (
            not approval_id
            or response.get("transaction_id") != receipt.transaction_id
            or receipt.plan_id != plan.plan_id
            or receipt.broker_id != plan.broker_id
            or receipt.plan_digest != plan.digest
            or receipt.transaction_digest != plan.transaction.digest
            or receipt.approval_receipt_id != approval_id
            or receipt.actor_user_id != plan.actor_user_id
            or receipt.actor_own_id != plan.actor_own_id
            or receipt.continuation_work_item_id != plan.continuation_work_item_id
            or receipt.reconciliation_idempotency_key != job.get("idempotency_key")
            or receipt.transaction_outcome is not TransactionOutcome.UNKNOWN
            or receipt.observed_at > response["updated_at"]
        ):
            raise ContractError("package reconciliation receipt drifted from its attempt")
        if receipt.postcondition_state is PackagePostconditionState.DESIRED:
            installed = {(item.name, item.architecture): item.version for item in receipt.installed}
            if any(
                installed.get((item.name, item.architecture or "")) != item.version
                for item in plan.transaction.requested
            ):
                raise ContractError("package reconciliation postcondition is incomplete")
        return receipt

    async def status(self, *, actor: ActorContext, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id, user_id=actor.user_id, actor_own_id=actor.own_id)
        if job is None:
            raise ContractError("host action job was not found")
        if job.get("adapter_id") == "package.apt":
            return await self._package_status(actor=actor, job=job)
        local_status = str(job["status"])
        if local_status in {"completed", "partial", "failed", "cancelled", "reconciled"}:
            return self._status_projection(job, agent=None)
        if local_status == "unknown":
            try:
                job = self._jobs.transition(
                    job_id,
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                    expected_status="unknown",
                    status="reconciling",
                    stage="reconciliation",
                    outcome_code="status_requested",
                )
            except HostJobTransitionError:
                refreshed = self._jobs.get(
                    job_id,
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                )
                if refreshed is None:
                    raise ContractError("host action job was not found") from None
                return self._status_projection(refreshed, agent=None)
            local_status = "reconciling"
        plan = HostActionPlan.from_payload(job["plan"])
        agent: dict[str, Any] | None
        try:
            agent = await self._client.call(
                "JobReconcile" if local_status == "reconciling" else "JobStatus",
                {},
                job_id=job_id,
                actor_id=actor.user_id,
                own_id=actor.own_id,
                idempotency_key=plan.idempotency_key,
                plan_digest=plan.digest,
                effectful=False,
            )
        except HostControlClientError:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "reconciliation_unavailable")
                refreshed = self._jobs.get(
                    job_id,
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                )
                assert refreshed is not None
                job = refreshed
            agent = None
        else:
            assert agent is not None
            if local_status in {"running", "reconciling"}:
                try:
                    settled = self._settle_status_response(
                        actor=actor,
                        job=job,
                        plan=plan,
                        response=agent,
                        expected_job_status=local_status,
                    )
                except HostActionUnknown:
                    if local_status == "reconciling":
                        self._restore_unknown(job, actor, "reconciliation_response_invalid")
                    raise
                except (ContractError, HostJobTransitionError, KeyError, TypeError, ValueError) as exc:
                    if local_status == "reconciling":
                        self._restore_unknown(job, actor, "reconciliation_response_invalid")
                    else:
                        self._mark_unknown(job, actor, "status_response_invalid")
                    raise HostActionUnknown("host status response failed validation") from exc
                if settled is not None:
                    return {**settled, "agent": agent}
                refreshed = self._jobs.get(
                    job_id,
                    user_id=actor.user_id,
                    actor_own_id=actor.own_id,
                )
                assert refreshed is not None
                job = refreshed
        return self._status_projection(job, agent=agent)

    @staticmethod
    def _status_projection(job: dict[str, Any], *, agent: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "agent": agent,
            "error_code": job.get("error_code"),
            "job_id": job["id"],
            "reconciliation_required": bool(job.get("reconciliation_required")),
            "result_ref": job.get("result_ref"),
            "status": job["status"],
        }

    async def _cancel_package(
        self,
        *,
        actor: ActorContext,
        job: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            plan = AptInstallPlan.from_payload(job["plan"])
        except (BrokerContractError, KeyError, TypeError) as exc:
            raise ContractError("durable package plan is invalid") from exc
        self._validate_install_continuation(job)
        local_status = str(job["status"])
        if local_status in {"completed", "partial", "failed", "cancelled", "reconciled"}:
            return self._status_projection(job, agent=None)
        if local_status == "unknown":
            job = self._jobs.transition(
                str(job["id"]),
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="unknown",
                status="reconciling",
                stage="package_cancellation",
                outcome_code="cancel_before_commit_requested",
            )
            local_status = "reconciling"
        try:
            response = await self._client.call(
                "PackageCancelBeforeCommit",
                {"plan_id": plan.plan_id},
                job_id=str(job["id"]),
                actor_id=actor.user_id,
                own_id=actor.own_id,
                idempotency_key=str(job["idempotency_key"]),
                plan_digest="0" * 64,
                effectful=True,
            )
        except (HostControlOutcomeUnknown, HostControlRejected, HostControlUnavailable) as exc:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "package_cancel_outcome_unknown")
                raise HostActionUnknown("package cancellation outcome is unknown") from exc
            if local_status == "running":
                self._mark_unknown(job, actor, "package_cancel_outcome_unknown")
                raise HostActionUnknown("package cancellation outcome is unknown") from exc
            raise HostCapabilityUnavailable("package cancellation is unavailable") from exc
        try:
            receipt = self._validate_package_status_record(response, plan=plan, job=job)
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "package_cancel_response_invalid")
            elif local_status == "running":
                self._mark_unknown(job, actor, "package_cancel_response_invalid")
            raise HostActionUnknown("package cancellation response failed validation") from exc
        if response.get("status") != "cancelled_before_commit" or receipt is not None:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "package_cancel_not_proven")
            elif local_status == "running":
                self._mark_unknown(job, actor, "package_cancel_not_proven")
            raise HostActionUnknown("package cancellation was not proven")
        terminal = self._jobs.transition(
            str(job["id"]),
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status=local_status,
            status="reconciled" if local_status == "reconciling" else "cancelled",
            stage="package_cancellation",
            outcome_code="cancelled_before_commit",
            error_code="cancelled_before_commit",
        )
        return {
            "agent": response,
            "cancelled": True,
            "job_id": terminal["id"],
            "status": terminal["status"],
            "terminal_outcome": "cancelled",
        }

    async def cancel(self, *, actor: ActorContext, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id, user_id=actor.user_id, actor_own_id=actor.own_id)
        if job is None:
            raise ContractError("host action job was not found")
        if job.get("adapter_id") == "package.apt":
            return await self._cancel_package(actor=actor, job=job)
        local_status = str(job["status"])
        if local_status in {"completed", "partial", "failed", "cancelled", "reconciled"}:
            return self._status_projection(job, agent=None)
        if local_status in {"planned", "awaiting_approval", "approved", "admitted"}:
            cancelled = self._jobs.transition(
                job_id,
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status=local_status,
                status="cancelled",
                stage="cancellation",
                outcome_code="cancelled_before_process",
                error_code="cancelled",
            )
            return {
                "cancelled": True,
                "job_id": job_id,
                "status": cancelled["status"],
                "terminal_outcome": "cancelled",
            }
        if local_status == "unknown":
            job = self._jobs.transition(
                job_id,
                user_id=actor.user_id,
                actor_own_id=actor.own_id,
                expected_status="unknown",
                status="reconciling",
                stage="cancellation",
                outcome_code="cancel_requested_during_reconciliation",
            )
            local_status = "reconciling"
        plan = HostActionPlan.from_payload(job["plan"])
        try:
            response = await self._client.call(
                "JobCancel",
                {},
                job_id=job_id,
                actor_id=actor.user_id,
                own_id=actor.own_id,
                idempotency_key=plan.idempotency_key,
                plan_digest=plan.digest,
                effectful=True,
            )
        except HostControlOutcomeUnknown as exc:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "cancel_outcome_unknown")
            else:
                self._mark_unknown(job, actor, "cancel_outcome_unknown")
            raise HostActionUnknown("host cancellation outcome is unknown") from exc
        except (HostControlRejected, HostControlUnavailable) as exc:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "cancel_rejected")
            else:
                self._mark_unknown(job, actor, "cancel_rejected")
            raise HostActionUnknown("host cancellation could not be reconciled") from exc
        try:
            settled = self._settle_status_response(
                actor=actor,
                job=job,
                plan=plan,
                response=response,
                expected_job_status=local_status,
            )
        except HostActionUnknown:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "cancel_response_invalid")
            raise
        except (ContractError, HostJobTransitionError, KeyError, TypeError, ValueError) as exc:
            if local_status == "reconciling":
                self._restore_unknown(job, actor, "cancel_response_invalid")
            else:
                self._mark_unknown(job, actor, "cancel_response_invalid")
            raise HostActionUnknown("host cancellation response failed validation") from exc
        if settled is not None:
            return {**settled, "cancelled": settled.get("terminal_outcome") == "cancelled"}
        refreshed = self._jobs.get(job_id, user_id=actor.user_id, actor_own_id=actor.own_id)
        assert refreshed is not None
        return {**self._status_projection(refreshed, agent=response), "cancelled": False}

    def _require_action_capability(self, actor: ActorContext, security_id: str) -> None:
        if self._authorization is None:
            raise AuthorizationError("host action authorization is unavailable")
        self._authorization.require(actor, security_id)

    def _require_fresh_action_capability(
        self,
        actor: ActorContext,
        security_id: str,
    ) -> ActorContext:
        """Re-read account state immediately before a native host effect."""

        if self._authorization is None:
            raise AuthorizationError("host action authorization is unavailable")
        try:
            user = self._storage.get_user(str(actor.own_id or ""))
        except Exception as exc:  # noqa: BLE001 - unavailable identity proof denies the effect
            raise AuthorizationError("host action principal is unavailable") from exc
        if not user or str(user.get("status") or "") != "active":
            raise AuthorizationError("host action account is unavailable")
        fresh_actor = replace(actor, preset_key=str(user.get("preset_key") or "user"))
        if not fresh_actor.is_owner:
            raise AuthorizationError("host actions are reserved for the installation owner")
        self._authorization.require(fresh_actor, security_id)
        return fresh_actor

    def _verify_evidence_files(
        self,
        job_id: str,
        receipt: HostActionReceipt,
        paths: dict[str, Any],
        receipt_ref: str,
    ) -> dict[str, bytes]:
        root = self._settings.host_job_root
        job_root = root / job_id
        if (
            root.resolve(strict=True) != root
            or job_root.is_symlink()
            or job_root.resolve(strict=True).parent != root
        ):
            raise ValueError("host evidence job root is invalid")
        verified: dict[str, bytes] = {}
        for evidence in receipt.evidence:
            relative = paths.get(evidence.evidence_id)
            if not isinstance(relative, str) or _SAFE_REF.fullmatch(relative) is None:
                raise ValueError("host evidence path is absent")
            payload = self._read_evidence(job_root, relative, maximum=evidence.size_bytes)
            if len(payload) != evidence.size_bytes or not hmac.compare_digest(
                hashlib.sha256(payload).hexdigest(), evidence.sha256
            ):
                raise ValueError("host evidence identity changed")
            verified[evidence.evidence_id] = payload
        receipt_bytes = self._read_evidence(job_root, receipt_ref, maximum=512 * 1024)
        if receipt_bytes != canonical_json_bytes(receipt.to_payload()):
            raise ValueError("host receipt file does not match the signed response")
        return verified

    @staticmethod
    def _read_evidence(job_root: Any, relative: str, *, maximum: int) -> bytes:
        selected = job_root / relative
        if selected.is_symlink() or selected.parent.resolve(strict=True).parent != job_root:
            raise ValueError("host evidence path escapes the job root")
        descriptor = os.open(
            selected,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
                or observed.st_size > maximum
                or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError("host evidence file metadata is unsafe")
            payload = bytearray()
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                payload.extend(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if len(payload) > maximum or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
            ):
                raise ValueError("host evidence file changed while reading")
            return bytes(payload)
        finally:
            os.close(descriptor)


def _stage_exact_job_input(
    root: Path,
    *,
    job_id: str,
    content: bytes,
    content_sha256: str,
) -> str:
    """Create or verify one immutable job input beneath an already private root."""

    if re.fullmatch(r"hjob_[0-9a-f]{32}", job_id) is None:
        raise ContractError("host file job id is invalid")
    if len(content) > MAX_JQ_INPUT_BYTES or not hmac.compare_digest(
        hashlib.sha256(content).hexdigest(), content_sha256
    ):
        raise ContractError("host file input identity is invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise ContractError("host job root is unavailable") from exc
    try:
        _validate_private_directory(root_fd, label="host job root")
        workspace_fd = _open_private_child_directory(root_fd, job_id)
        try:
            input_fd = _open_private_child_directory(workspace_fd, "input")
            try:
                name = f"source-{content_sha256[:16]}.json"
                _create_or_verify_private_file(
                    input_fd,
                    name=name,
                    content=content,
                    content_sha256=content_sha256,
                )
            finally:
                os.close(input_fd)
        finally:
            os.close(workspace_fd)
    finally:
        os.close(root_fd)
    return f"input/{name}"


def _validate_private_directory(descriptor: int, *, label: str) -> None:
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise ContractError(f"{label} has unsafe ownership or permissions")


def _open_private_child_directory(parent_fd: int, name: str) -> int:
    if re.fullmatch(r"(?:hjob_[0-9a-f]{32}|input)", name) is None:
        raise ContractError("host workspace directory name is invalid")
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ContractError("host workspace directory could not be created") from exc
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ContractError("host workspace directory is unsafe") from exc
    try:
        _validate_private_directory(descriptor, label="host workspace directory")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _create_or_verify_private_file(
    directory_fd: int,
    *,
    name: str,
    content: bytes,
    content_sha256: str,
) -> None:
    if re.fullmatch(r"source-[0-9a-f]{16}\.json", name) is None:
        raise ContractError("host workspace input name is invalid")
    common_flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | common_flags,
            0o600,
            dir_fd=directory_fd,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(name, os.O_RDONLY | common_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ContractError("host workspace input is unsafe") from exc
    except OSError as exc:
        raise ContractError("host workspace input could not be created") from exc
    try:
        if created:
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while staging host input")
                remaining = remaining[written:]
            os.fsync(descriptor)
        else:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or observed.st_nlink != 1
                or observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
                or observed.st_size != len(content)
            ):
                raise ContractError("existing host workspace input metadata is unsafe")
            payload = bytearray()
            remaining_bytes = len(content) + 1
            while remaining_bytes:
                chunk = os.read(descriptor, min(65_536, remaining_bytes))
                if not chunk:
                    break
                payload.extend(chunk)
                remaining_bytes -= len(chunk)
            after = os.fstat(descriptor)
            if (
                len(payload) != len(content)
                or (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), content_sha256)
            ):
                raise ContractError("existing host workspace input identity changed")
    except Exception:
        if created:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=directory_fd)
        raise
    finally:
        os.close(descriptor)
    if created:
        os.fsync(directory_fd)


__all__ = [
    "HostActionUnknown",
    "HostCapabilityUnavailable",
    "HostControlService",
    "PreparedHostAction",
]
