"""Server-origin proof for one representative semantic-supervisor window.

The offline baseline evaluator deliberately emits only a candidate.  This
module is the independent trust root which can elevate that candidate into a
representative-window fact: the running Friday server recomputes the report
from its own database, binds it to the sealed live release and current
supervisor registry, signs a short-lived body-free attestation with the
database audit-privacy key, and persists a one-use lookup token.  The consumer
burns that token atomically before a release operator may use the witness.

There is intentionally no public signing helper and no path which accepts a
synthetic/offline authority label.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from friday import __version__, semantic_supervisor_policy
from friday.audit_privacy import decode_audit_privacy_key
from friday.orchestration import supervisor_production_baseline as production_baseline_module
from friday.orchestration.capability_binding import operational_capability_snapshot
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
)
from friday.orchestration.supervisor_contracts import SupervisorMode, canonical_dumps
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PRODUCTION_BASELINE_KIND,
    SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
    build_production_baseline,
)
from friday.orchestration.supervisor_promoted_product_event import (
    PromotedProductEventError,
    load_accepted_supervisor_latency_budget,
)
from friday.secondary_brain.document_map_evidence import _live_release_identity  # noqa: PLC2701
from friday.secondary_product_witness import secondary_product_process_epoch_sha256

REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA = "friday.semantic-supervisor-representative-window-attestation.v1"
REPRESENTATIVE_WINDOW_ISSUE_REQUEST_SCHEMA = (
    "friday.semantic-supervisor-representative-window-issue-request.v1"
)
REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA = (
    "friday.semantic-supervisor-representative-window-issue-response.v1"
)
REPRESENTATIVE_WINDOW_CONSUME_REQUEST_SCHEMA = (
    "friday.semantic-supervisor-representative-window-consume-request.v1"
)
REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_SCHEMA = (
    "friday.semantic-supervisor-representative-window-consume-response.v1"
)
REPRESENTATIVE_WINDOW_AUTHORITY = "server_recomputed_live_production"
REPRESENTATIVE_WINDOW_ATTESTATION_TTL_SEC = 570
REPRESENTATIVE_WINDOW_ATTESTATION_SKEW_SEC = 30
REPRESENTATIVE_WINDOW_REQUEST_KEY_PREFIX = "semantic-supervisor-representative-window:"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_ATTESTATION_ID_RE = re.compile(r"sswindow_[0-9a-f]{32}\Z")
_SIGNATURE_DOMAIN = b"friday.semantic-supervisor-representative-window.v1\0"
_CONSUME_DOMAIN = b"friday.semantic-supervisor-representative-window-consume.v1\0"
_MAX_BASELINE_BYTES = 1_048_576
_MAX_BUDGET_BYTES = 4_096
_MAX_STORED_WITNESSES = 4
_PROCESS_ACCEPTANCE_AUTHORITY = object()
_PROCESS_ACCEPTANCE_KEY = secrets.token_bytes(32)

_SCHEDULER_PUBLIC_KEYS = frozenset(
    {"schema", "role", "enabled", "configured", "mode", "state", "available", "semantic_supervisor"}
)
_SCHEDULER_SUPERVISOR_KEYS = frozenset(
    {
        "workload",
        "requested_mode",
        "effective_mode",
        "policy_id",
        "policy_sha256",
        "workload_available",
        "runtime_available",
        "closed_reason",
    }
)

_STABLE_SERVER_IDENTITY_KEYS = frozenset(
    {
        "primary_backend_version",
        "supervisor_policy_id",
        "supervisor_policy_sha256",
        "runtime_profile_id",
        "runtime_profile_manifest_sha256",
    }
)

REPRESENTATIVE_WINDOW_SERVER_IDENTITY_KEYS = frozenset(
    {
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_backend_version",
        "requested_mode",
        "observed_release_commit",
        "observed_release_metadata_sha256",
        "observed_release_tree_sha256",
        "observed_registry_binding_sha256",
        "supervisor_policy_id",
        "supervisor_policy_sha256",
        "runtime_profile_id",
        "runtime_profile_manifest_sha256",
    }
)

REPRESENTATIVE_WINDOW_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "attestation_id",
        "authority",
        "target_mode",
        "observed_mode",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "latency_budget_target_mode",
        "latency_budget_source_revision_sha256",
        "maximum_user_visible_latency_ms",
        "precursor_assist_promotion_evidence_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_backend_version",
        "requested_mode",
        "observed_release_commit",
        "observed_release_metadata_sha256",
        "observed_release_tree_sha256",
        "observed_registry_binding_sha256",
        "supervisor_policy_id",
        "supervisor_policy_sha256",
        "runtime_profile_id",
        "runtime_profile_manifest_sha256",
        "observer_runner_sha256",
        "sample_limit",
        "turn_trace_count",
        "joined_trace_count",
        "representative_window_sha256",
        "server_recomputed",
        "representative_window_attested",
        "synthetic_authority",
        "lookup_token_sha256",
        "state_version",
        "issued_at",
        "expires_at",
        "signature",
    }
)

REPRESENTATIVE_WINDOW_ISSUE_REQUEST_KEYS = frozenset(
    {
        "schema",
        "target_mode",
        "baseline_file_sha256",
        "baseline",
        "registry_binding_sha256",
        "latency_budget_file_sha256",
        "latency_budget",
        "precursor_assist_promotion_evidence_sha256",
    }
)

REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_KEYS = frozenset(
    {
        "schema",
        "status",
        "server_attestation",
        "server_attestation_sha256",
        "attestation_lookup_token",
        "lookup_token_sha256",
        "state_version",
    }
)

REPRESENTATIVE_WINDOW_CONSUME_REQUEST_KEYS = frozenset(
    {
        "schema",
        "attestation_lookup_token",
        "server_attestation_sha256",
        "target_mode",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "observer_runner_sha256",
        "precursor_assist_promotion_evidence_sha256",
    }
)

REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_KEYS = frozenset(
    {
        "schema",
        "status",
        "attestation_id",
        "target_mode",
        "observed_mode",
        "server_attestation_sha256",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "observer_runner_sha256",
        "representative_window_sha256",
        "precursor_assist_promotion_evidence_sha256",
        "lookup_token_sha256",
        "consume_request_sha256",
        "consumed_at",
        "state_version",
        "consume_binding_sha256",
        "server_attestation",
    }
)


class RepresentativeWindowAttestationError(ValueError):
    """A candidate or server witness is outside the closed trust contract."""


@dataclass(frozen=True, slots=True)
class AcceptedRepresentativeWindowAttestation:
    """Process-sealed result of rechecking one persisted consumed witness."""

    attestation_id: str
    target_mode: SupervisorMode
    observed_mode: SupervisorMode
    baseline_file_sha256: str
    baseline_report_sha256: str
    latency_budget_file_sha256: str
    latency_budget_document_sha256: str
    latency_budget_maximum_ms: int
    source_revision_sha256: str
    registry_binding_sha256: str
    observer_runner_sha256: str
    representative_window_sha256: str
    precursor_assist_promotion_evidence_sha256: str | None
    server_attestation_sha256: str
    consume_binding_sha256: str
    consumed_response_sha256: str
    consumed_at: int
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def payload(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            "target_mode": self.target_mode.value,
            "observed_mode": self.observed_mode.value,
            "baseline_file_sha256": self.baseline_file_sha256,
            "baseline_report_sha256": self.baseline_report_sha256,
            "latency_budget_file_sha256": self.latency_budget_file_sha256,
            "latency_budget_document_sha256": self.latency_budget_document_sha256,
            "latency_budget_maximum_ms": self.latency_budget_maximum_ms,
            "source_revision_sha256": self.source_revision_sha256,
            "registry_binding_sha256": self.registry_binding_sha256,
            "observer_runner_sha256": self.observer_runner_sha256,
            "representative_window_sha256": self.representative_window_sha256,
            "precursor_assist_promotion_evidence_sha256": (self.precursor_assist_promotion_evidence_sha256),
            "server_attestation_sha256": self.server_attestation_sha256,
            "consume_binding_sha256": self.consume_binding_sha256,
            "consumed_response_sha256": self.consumed_response_sha256,
            "consumed_at": self.consumed_at,
        }

    def __post_init__(self) -> None:
        if (
            self._process_authority is not _PROCESS_ACCEPTANCE_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(
                self._process_seal_sha256,
                _process_acceptance_seal(self.payload()),
            )
        ):
            raise RepresentativeWindowAttestationError(
                "representative-window witness was not accepted by this process"
            )


def _process_acceptance_seal(value: Mapping[str, Any]) -> str:
    return hmac.new(
        _PROCESS_ACCEPTANCE_KEY,
        b"friday.accepted-representative-window.v1\0" + representative_window_canonical(value),
        hashlib.sha256,
    ).hexdigest()


def is_accepted_representative_window_attestation(value: object) -> bool:
    if type(value) is not AcceptedRepresentativeWindowAttestation:
        return False
    item = cast(AcceptedRepresentativeWindowAttestation, value)
    return bool(
        item._process_authority is _PROCESS_ACCEPTANCE_AUTHORITY
        and type(item._process_seal_sha256) is str
        and hmac.compare_digest(
            item._process_seal_sha256,
            _process_acceptance_seal(item.payload()),
        )
    )


def representative_window_canonical(value: Mapping[str, Any]) -> bytes:
    if not isinstance(value, Mapping):
        raise TypeError("representative-window payload must be a mapping")
    return (canonical_dumps(dict(value)) + "\n").encode("utf-8")


def representative_window_sha256(value: Mapping[str, Any] | bytes | str) -> str:
    raw = (
        representative_window_canonical(value)
        if isinstance(value, Mapping)
        else value.encode("utf-8")
        if isinstance(value, str)
        else value
    )
    if type(raw) is not bytes:
        raise TypeError("representative-window digest input is invalid")
    return hashlib.sha256(raw).hexdigest()


def representative_window_observer_runner_sha256() -> str:
    """Digest the exact code-owned recomputer and issuer implementation."""

    module_path = getattr(production_baseline_module, "__file__", None)
    if type(module_path) is not str:
        raise RepresentativeWindowAttestationError("baseline runner is unavailable")
    paths = (Path(module_path).resolve(strict=True), Path(__file__).resolve(strict=True))
    digest = hashlib.sha256(b"friday.representative-window-observer-runner.v1\0")
    for path in paths:
        raw = path.read_bytes()
        if not raw or len(raw) > 1_048_576:
            raise RepresentativeWindowAttestationError("baseline runner is invalid")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _valid_digest(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _exact_mapping(
    value: object,
    keys: frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise RepresentativeWindowAttestationError(f"{label} keys do not match")
    return cast(dict[str, Any], value)


def _strict_json(
    value: object,
    *,
    maximum_bytes: int = _MAX_BASELINE_BYTES,
    label: str = "baseline",
) -> dict[str, Any]:
    if type(value) is dict:
        raw = representative_window_canonical(cast(dict[str, Any], value))
    elif type(value) is bytes:
        raw = value
    else:
        raise RepresentativeWindowAttestationError(f"{label} must be an object or bytes")
    if not 0 < len(raw) <= maximum_bytes:
        raise RepresentativeWindowAttestationError(f"{label} exceeds its byte bound")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise RepresentativeWindowAttestationError(f"{label} has duplicate keys")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise RepresentativeWindowAttestationError(f"{label} has a non-finite number")

    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except RepresentativeWindowAttestationError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise RepresentativeWindowAttestationError(f"{label} JSON is invalid") from exc
    if type(decoded) is not dict or raw != representative_window_canonical(decoded):
        raise RepresentativeWindowAttestationError(f"{label} is not canonical JSON")
    return cast(dict[str, Any], decoded)


def _signing_key_from_transaction(conn: Any) -> bytes:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
    try:
        return decode_audit_privacy_key(row[0] if row is not None else None)
    except Exception as exc:
        raise RepresentativeWindowAttestationError(
            "server representative-window signing key is unavailable"
        ) from exc


def _signature(key: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(
        key,
        _SIGNATURE_DOMAIN + representative_window_canonical(payload),
        hashlib.sha256,
    ).hexdigest()


def _consume_signature(key: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(
        key,
        _CONSUME_DOMAIN + representative_window_canonical(payload),
        hashlib.sha256,
    ).hexdigest()


def _unsigned_attestation(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("signature", None)
    return result


def _validate_server_identity(value: object) -> dict[str, Any]:
    identity = _exact_mapping(
        value,
        REPRESENTATIVE_WINDOW_SERVER_IDENTITY_KEYS,
        label="representative-window server identity",
    )
    if (
        type(identity["primary_pid"]) is not int
        or identity["primary_pid"] <= 0
        or identity["primary_backend_version"] != __version__
        or _COMMIT_RE.fullmatch(str(identity["observed_release_commit"])) is None
        or identity["requested_mode"]
        not in {
            SupervisorMode.ASSIST.value,
            SupervisorMode.CANARY.value,
        }
        or any(
            not _valid_digest(identity[name])
            for name in (
                "primary_process_epoch_sha256",
                "observed_release_metadata_sha256",
                "observed_release_tree_sha256",
                "observed_registry_binding_sha256",
                "supervisor_policy_sha256",
                "runtime_profile_manifest_sha256",
            )
        )
        or identity["supervisor_policy_id"] != semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
        or identity["supervisor_policy_sha256"]
        != semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
        or identity["runtime_profile_id"] != semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID
        or identity["runtime_profile_manifest_sha256"]
        != semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
    ):
        raise RepresentativeWindowAttestationError("representative-window server identity is invalid")
    return identity


def representative_window_current_server_identity(
    settings: object,
    secondary: object,
    *,
    target_mode: SupervisorMode,
) -> dict[str, Any]:
    """Re-attest the sealed live release and healthy shadow scheduler."""

    if target_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}:
        raise RepresentativeWindowAttestationError("representative target mode is invalid")

    public_method = getattr(secondary, "public_status", None)
    diagnostics_method = getattr(secondary, "diagnostics_status", None)
    if not callable(public_method) or not callable(diagnostics_method):
        raise RepresentativeWindowAttestationError("semantic scheduler is unavailable")
    try:
        public = public_method()
        diagnostics = diagnostics_method()
        if type(public) is not dict or type(diagnostics) is not dict:
            raise ValueError("scheduler status is not closed")
        scheduler = public.get("semantic_supervisor")
        diagnostic_scheduler = diagnostics.get("semantic_supervisor")
        requested_mode = scheduler.get("requested_mode") if type(scheduler) is dict else None
        if (
            set(public) != _SCHEDULER_PUBLIC_KEYS
            or type(scheduler) is not dict
            or set(scheduler) != _SCHEDULER_SUPERVISOR_KEYS
            or diagnostic_scheduler != scheduler
            or public.get("schema") != "friday.optional-secondary-health.v1"
            or public.get("role") != "optional_advisory"
            or public.get("enabled") is not True
            or public.get("configured") is not True
            or public.get("mode") != "assist"
            or public.get("state") != "healthy"
            or public.get("available") is not True
            or scheduler.get("workload") != "plan_candidate"
            or requested_mode not in {SupervisorMode.ASSIST.value, SupervisorMode.CANARY.value}
            or getattr(settings, "semantic_supervisor_mode", None) != requested_mode
            or scheduler.get("effective_mode") != SupervisorMode.SHADOW.value
            or scheduler.get("policy_id") != semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
            or scheduler.get("policy_sha256")
            != semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
            or scheduler.get("workload_available") is not True
            or scheduler.get("runtime_available") is not True
            or scheduler.get("closed_reason") != "admitted"
            or diagnostics.get("profile") != semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID
            or diagnostics.get("profile_admission") != "accepted"
            or diagnostics.get("profile_manifest_match") is not True
            or diagnostics.get("served_model_match") is not True
        ):
            raise ValueError("scheduler identity is not admitted")
        live = _live_release_identity(verify_tree=True)
        registry = operational_capability_snapshot().digest_hex()
    except Exception as exc:
        raise RepresentativeWindowAttestationError(
            "live representative-window identity is unavailable"
        ) from exc
    identity = {
        "primary_pid": os.getpid(),
        "primary_process_epoch_sha256": secondary_product_process_epoch_sha256(os.getpid()),
        "primary_backend_version": __version__,
        "observed_release_commit": live["predecessor_release_commit"],
        "observed_release_metadata_sha256": live["predecessor_release_metadata_sha256"],
        "observed_release_tree_sha256": live["predecessor_release_tree_manifest_sha256"],
        "observed_registry_binding_sha256": registry,
        "requested_mode": requested_mode,
        "supervisor_policy_id": scheduler["policy_id"],
        "supervisor_policy_sha256": scheduler["policy_sha256"],
        "runtime_profile_id": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
    }
    return _validate_server_identity(identity)


def _server_identity_matches(
    attestation: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    after_restart: bool,
) -> bool:
    try:
        identity = _validate_server_identity(current)
    except RepresentativeWindowAttestationError:
        return False
    if any(attestation.get(name) != identity.get(name) for name in _STABLE_SERVER_IDENTITY_KEYS):
        return False
    if not after_restart:
        return all(
            attestation.get(name) == identity.get(name) for name in REPRESENTATIVE_WINDOW_SERVER_IDENTITY_KEYS
        )
    return bool(
        attestation.get("source_revision_sha256") == identity.get("observed_release_tree_sha256")
        and attestation.get("registry_binding_sha256") == identity.get("observed_registry_binding_sha256")
        and identity.get("requested_mode") == attestation.get("target_mode")
    )


def _representative_window_facts(
    report: Mapping[str, Any],
    *,
    target_mode: SupervisorMode,
    precursor_assist_promotion_evidence_sha256: str | None,
) -> dict[str, Any]:
    if report.get("schema") != SUPERVISOR_PRODUCTION_BASELINE_SCHEMA:
        raise RepresentativeWindowAttestationError("baseline schema is not accepted")
    evidence = report.get("evidence")
    sample = report.get("sample")
    windows = report.get("product_windows")
    if type(evidence) is not dict or type(sample) is not dict or type(windows) is not dict:
        raise RepresentativeWindowAttestationError("baseline sections are invalid")
    expected_evidence = {
        "kind": SUPERVISOR_PRODUCTION_BASELINE_KIND,
        "body_free": True,
        "production_acceptance": False,
        "acceptance_authority": "operator_review_required",
        "representative_window_attested": False,
        "promotion_authority": False,
    }
    anomaly_keys = (
        "malformed_turn_traces",
        "malformed_joined_events",
        "malformed_promoted_product_events",
        "duplicate_turn_trace_digests",
        "duplicate_shadow_product_events",
        "duplicate_promoted_product_events",
        "unmatched_shadow_product_events",
        "unmatched_promoted_product_events",
    )
    if evidence != expected_evidence or any(sample.get(key) != 0 for key in anomaly_keys):
        raise RepresentativeWindowAttestationError("baseline is not anomaly-free")
    limit = sample.get("limit")
    traces = sample.get("turn_traces")
    joins = sample.get("joined_supervisor_events")
    promoted = sample.get("promoted_product_events")
    minimum = SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
    if (
        type(limit) is not int
        or type(traces) is not int
        or type(joins) is not int
        or type(promoted) is not int
        or limit < 1
        or traces < minimum
        or traces >= limit
        or joins >= limit
        or promoted >= limit
        or traces < max(joins, promoted)
    ):
        raise RepresentativeWindowAttestationError("baseline does not contain one complete population")
    shadow = windows.get("shadow_readiness")
    if type(shadow) is not dict:
        raise RepresentativeWindowAttestationError("shadow readiness window is invalid")
    baseline = shadow.get("baseline")
    window_sha256 = shadow.get("readiness_witness_sha256")
    if (
        shadow.get("mode") != SupervisorMode.SHADOW.value
        or shadow.get("production_joined") is not True
        or shadow.get("actual_promoted_execution") is not False
        or shadow.get("quality_claim") != "documented_baseline_failure_only"
        or shadow.get("observation_count") != joins
        or shadow.get("joined_trace_count") != joins
        or joins < minimum
        or type(shadow.get("readiness_observation_count")) is not int
        or shadow["readiness_observation_count"] < minimum
        or shadow.get("call_rate_observation_count") != joins
        or shadow.get("user_visible_observation_count") != shadow.get("readiness_observation_count")
        or not _valid_digest(window_sha256)
        or type(baseline) is not dict
        or baseline.get("observation_count") != shadow.get("readiness_observation_count")
        or baseline.get("latency_observation_count") != shadow.get("readiness_observation_count")
        or not _valid_digest(baseline.get("window_sha256"))
    ):
        raise RepresentativeWindowAttestationError("shadow readiness window is not representative")
    if target_mode is SupervisorMode.ASSIST:
        if precursor_assist_promotion_evidence_sha256 is not None or promoted != 0:
            raise RepresentativeWindowAttestationError(
                "assist readiness cannot claim promoted or precursor evidence"
            )
        return {
            "observed_mode": SupervisorMode.SHADOW.value,
            "sample_limit": limit,
            "turn_trace_count": traces,
            "joined_trace_count": joins,
            "representative_window_sha256": window_sha256,
            "latency_observation_count": baseline["latency_observation_count"],
            "latency_total_ms": baseline.get("latency_total_ms"),
            "latency_max_ms": baseline.get("latency_max_ms"),
        }
    if target_mode is not SupervisorMode.CANARY or not _valid_digest(
        precursor_assist_promotion_evidence_sha256
    ):
        raise RepresentativeWindowAttestationError("canary precursor is invalid")
    promoted_windows = windows.get("promoted_execution")
    if type(promoted_windows) is not dict:
        raise RepresentativeWindowAttestationError("promoted windows are invalid")
    assist = promoted_windows.get(SupervisorMode.ASSIST.value)
    canary = promoted_windows.get(SupervisorMode.CANARY.value)
    if type(assist) is not dict or type(canary) is not dict:
        raise RepresentativeWindowAttestationError("promoted window is invalid")
    metrics = assist.get("promoted")
    assist_observations = assist.get("observation_count")
    assist_window_sha256 = assist.get("product_window_sha256")
    if (
        assist.get("mode") != SupervisorMode.ASSIST.value
        or assist.get("production_joined") is not True
        or assist.get("actual_promoted_execution") is not True
        or type(assist_observations) is not int
        or assist_observations < minimum
        or assist.get("joined_trace_count") != assist_observations
        or assist.get("promotion_evidence_count") != 1
        or assist.get("promotion_evidence_sha256") != precursor_assist_promotion_evidence_sha256
        or assist.get("call_rate_observation_count") != assist_observations
        or assist.get("user_visible_observation_count") != assist_observations
        or assist.get("user_visible_regression_count") != 0
        or not _valid_digest(assist_window_sha256)
        or type(metrics) is not dict
        or metrics.get("stage") != SupervisorMode.ASSIST.value
        or metrics.get("observation_count") != assist_observations
        or metrics.get("latency_observation_count") != assist_observations
        or not _valid_digest(metrics.get("window_sha256"))
        or canary.get("observation_count") != 0
        or promoted != assist_observations
    ):
        raise RepresentativeWindowAttestationError("assist outcome window is not representative")
    return {
        "observed_mode": SupervisorMode.ASSIST.value,
        "sample_limit": limit,
        "turn_trace_count": traces,
        "joined_trace_count": assist_observations,
        "representative_window_sha256": assist_window_sha256,
        "latency_observation_count": metrics["latency_observation_count"],
        "latency_total_ms": metrics.get("latency_total_ms"),
        "latency_max_ms": metrics.get("latency_max_ms"),
    }


def validate_representative_window_attestation(
    signing_key: bytes,
    value: object,
    *,
    now: int,
    current_server_identity: Mapping[str, Any],
    consumed_at: int | None = None,
    after_restart: bool = False,
) -> bool:
    """Verify one signed witness for issue/consume or a persisted restart."""

    try:
        item = _exact_mapping(
            value,
            REPRESENTATIVE_WINDOW_ATTESTATION_KEYS,
            label="representative-window attestation",
        )
        _validate_server_identity(current_server_identity)
        _validate_server_identity({name: item[name] for name in REPRESENTATIVE_WINDOW_SERVER_IDENTITY_KEYS})
        signature = item["signature"]
        try:
            target_mode = SupervisorMode(item["target_mode"])
        except (TypeError, ValueError):
            return False
        expected_observed = (
            SupervisorMode.SHADOW if target_mode is SupervisorMode.ASSIST else SupervisorMode.ASSIST
        )
        precursor = item["precursor_assist_promotion_evidence_sha256"]
        valid_time = (
            item["expires_at"] > now
            if consumed_at is None
            else type(consumed_at) is int
            and item["issued_at"] <= consumed_at <= item["expires_at"]
            and consumed_at <= now + REPRESENTATIVE_WINDOW_ATTESTATION_SKEW_SEC
        )
        if (
            item["schema"] != REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA
            or _ATTESTATION_ID_RE.fullmatch(str(item["attestation_id"])) is None
            or item["authority"] != REPRESENTATIVE_WINDOW_AUTHORITY
            or target_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}
            or item["observed_mode"] != expected_observed.value
            or item["requested_mode"] != SupervisorMode.ASSIST.value
            or item["latency_budget_target_mode"] != target_mode.value
            or item["latency_budget_source_revision_sha256"] != item["source_revision_sha256"]
            or item["latency_budget_file_sha256"] != item["latency_budget_document_sha256"]
            or item["registry_binding_sha256"] != item["observed_registry_binding_sha256"]
            or (target_mode is SupervisorMode.ASSIST and precursor is not None)
            or (target_mode is SupervisorMode.CANARY and not _valid_digest(precursor))
            or item["server_recomputed"] is not True
            or item["representative_window_attested"] is not True
            or item["synthetic_authority"] is not False
            or item["state_version"] != 1
            or type(item["issued_at"]) is not int
            or type(item["expires_at"]) is not int
            or not item["issued_at"]
            < item["expires_at"]
            <= item["issued_at"] + REPRESENTATIVE_WINDOW_ATTESTATION_TTL_SEC
            or item["issued_at"] > now + REPRESENTATIVE_WINDOW_ATTESTATION_SKEW_SEC
            or not valid_time
            or not _valid_digest(signature)
            or not _server_identity_matches(
                item,
                current_server_identity,
                after_restart=after_restart,
            )
            or any(
                not _valid_digest(item[name])
                for name in (
                    "baseline_file_sha256",
                    "baseline_report_sha256",
                    "latency_budget_file_sha256",
                    "latency_budget_document_sha256",
                    "source_revision_sha256",
                    "registry_binding_sha256",
                    "observer_runner_sha256",
                    "representative_window_sha256",
                    "lookup_token_sha256",
                )
            )
            or type(item["sample_limit"]) is not int
            or type(item["turn_trace_count"]) is not int
            or type(item["joined_trace_count"]) is not int
            or type(item["maximum_user_visible_latency_ms"]) is not int
            or item["maximum_user_visible_latency_ms"] <= 0
            or not hmac.compare_digest(
                signature,
                _signature(signing_key, _unsigned_attestation(item)),
            )
        ):
            return False
    except Exception:
        return False
    return True


def validate_representative_window_issue_request(value: object) -> bool:
    try:
        item = _exact_mapping(
            value,
            REPRESENTATIVE_WINDOW_ISSUE_REQUEST_KEYS,
            label="representative-window issue request",
        )
        mode = item["target_mode"]
        precursor = item["precursor_assist_promotion_evidence_sha256"]
        return bool(
            item["schema"] == REPRESENTATIVE_WINDOW_ISSUE_REQUEST_SCHEMA
            and mode in {SupervisorMode.ASSIST.value, SupervisorMode.CANARY.value}
            and _valid_digest(item["baseline_file_sha256"])
            and _valid_digest(item["latency_budget_file_sha256"])
            and _valid_digest(item["registry_binding_sha256"])
            and type(item["baseline"]) is dict
            and type(item["latency_budget"]) is dict
            and (precursor is None if mode == SupervisorMode.ASSIST.value else _valid_digest(precursor))
        )
    except Exception:
        return False


def validate_representative_window_consume_request(value: object) -> bool:
    try:
        item = _exact_mapping(
            value,
            REPRESENTATIVE_WINDOW_CONSUME_REQUEST_KEYS,
            label="representative-window consume request",
        )
        token = item["attestation_lookup_token"]
        mode = item["target_mode"]
        precursor = item["precursor_assist_promotion_evidence_sha256"]
        return bool(
            item["schema"] == REPRESENTATIVE_WINDOW_CONSUME_REQUEST_SCHEMA
            and mode in {SupervisorMode.ASSIST.value, SupervisorMode.CANARY.value}
            and type(token) is str
            and re.fullmatch(r"[0-9a-f]{64}", token) is not None
            and all(
                _valid_digest(item[name])
                for name in (
                    "server_attestation_sha256",
                    "baseline_file_sha256",
                    "baseline_report_sha256",
                    "latency_budget_file_sha256",
                    "latency_budget_document_sha256",
                    "source_revision_sha256",
                    "registry_binding_sha256",
                    "observer_runner_sha256",
                )
            )
            and (precursor is None if mode == SupervisorMode.ASSIST.value else _valid_digest(precursor))
        )
    except Exception:
        return False


def validate_representative_window_issue_response(value: object) -> bool:
    try:
        item = _exact_mapping(
            value,
            REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_KEYS,
            label="representative-window issue response",
        )
        attestation = item["server_attestation"]
        token = item["attestation_lookup_token"]
        return bool(
            item["schema"] == REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA
            and item["status"] == "unused"
            and item["state_version"] == 1
            and type(attestation) is dict
            and type(token) is str
            and re.fullmatch(r"[0-9a-f]{64}", token) is not None
            and _valid_digest(item["server_attestation_sha256"])
            and _valid_digest(item["lookup_token_sha256"])
            and representative_window_sha256(attestation) == item["server_attestation_sha256"]
            and hashlib.sha256(token.encode("ascii")).hexdigest() == item["lookup_token_sha256"]
            and attestation.get("lookup_token_sha256") == item["lookup_token_sha256"]
        )
    except Exception:
        return False


def issue_representative_window_attestation(
    storage: Any,
    *,
    user_id: str,
    request_value: Mapping[str, Any],
    current_server_identity: Mapping[str, Any],
    now: int | None = None,
) -> dict[str, Any]:
    """Recompute, sign and persist one unused representative-window witness."""

    if not validate_representative_window_issue_request(request_value):
        raise RepresentativeWindowAttestationError("issue request is invalid")
    identity = _validate_server_identity(current_server_identity)
    if identity["requested_mode"] != SupervisorMode.ASSIST.value:
        raise RepresentativeWindowAttestationError(
            "representative witness must be issued by the assist predecessor"
        )
    target_mode = SupervisorMode(request_value["target_mode"])
    report = _strict_json(request_value["baseline"])
    baseline_raw = representative_window_canonical(report)
    if not hmac.compare_digest(
        representative_window_sha256(baseline_raw),
        str(request_value["baseline_file_sha256"]),
    ):
        raise RepresentativeWindowAttestationError("baseline file digest does not match")
    budget = _strict_json(
        request_value["latency_budget"],
        maximum_bytes=_MAX_BUDGET_BYTES,
        label="latency budget",
    )
    budget_raw = representative_window_canonical(budget)
    try:
        accepted_budget = load_accepted_supervisor_latency_budget(
            budget_raw,
            expected_document_sha256=str(request_value["latency_budget_file_sha256"]),
        )
    except (TypeError, PromotedProductEventError) as exc:
        raise RepresentativeWindowAttestationError("latency budget is not accepted") from exc
    budget_document = accepted_budget.document
    if (
        budget_document.target_mode is not target_mode
        or request_value["registry_binding_sha256"] != identity["observed_registry_binding_sha256"]
    ):
        raise RepresentativeWindowAttestationError("target budget or registry binding does not match")
    issued_at = int(time.time()) if now is None else now
    if type(issued_at) is not int or issued_at <= 0:
        raise RepresentativeWindowAttestationError("issue time is invalid")

    with storage.transaction() as conn:
        sample = report.get("sample")
        if type(sample) is not dict or type(sample.get("limit")) is not int:
            raise RepresentativeWindowAttestationError("baseline sample is invalid")
        recomputed = build_production_baseline(conn, limit=sample["limit"])
        if report != recomputed:
            raise RepresentativeWindowAttestationError(
                "baseline was not recomputed from this live database snapshot"
            )
        facts = _representative_window_facts(
            report,
            target_mode=target_mode,
            precursor_assist_promotion_evidence_sha256=request_value[
                "precursor_assist_promotion_evidence_sha256"
            ],
        )
        observed_mode = facts.pop("observed_mode")
        latency_observation_count = facts.pop("latency_observation_count")
        latency_total_ms = facts.pop("latency_total_ms")
        latency_max_ms = facts.pop("latency_max_ms")
        maximum_latency_ms = budget_document.maximum_user_visible_latency_ms
        if (
            type(latency_observation_count) is not int
            or latency_observation_count < SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
            or type(latency_total_ms) is not int
            or type(latency_max_ms) is not int
            or latency_total_ms < 0
            or latency_max_ms < 0
            or latency_max_ms > maximum_latency_ms
            or latency_total_ms > maximum_latency_ms * latency_observation_count
        ):
            raise RepresentativeWindowAttestationError(
                "representative window exceeds the exact latency budget"
            )
        report_sha256 = report.get("report_sha256")
        if not _valid_digest(report_sha256):
            raise RepresentativeWindowAttestationError("baseline report digest is invalid")
        key = _signing_key_from_transaction(conn)
        lookup_token = secrets.token_hex(32)
        attestation_id = f"sswindow_{secrets.token_hex(16)}"
        unsigned: dict[str, Any] = {
            "schema": REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA,
            "attestation_id": attestation_id,
            "authority": REPRESENTATIVE_WINDOW_AUTHORITY,
            "target_mode": target_mode.value,
            "observed_mode": observed_mode,
            "baseline_file_sha256": request_value["baseline_file_sha256"],
            "baseline_report_sha256": report_sha256,
            "latency_budget_file_sha256": request_value["latency_budget_file_sha256"],
            "latency_budget_document_sha256": accepted_budget.document_sha256,
            "latency_budget_target_mode": budget_document.target_mode.value,
            "latency_budget_source_revision_sha256": (budget_document.source_revision_sha256),
            "maximum_user_visible_latency_ms": maximum_latency_ms,
            "precursor_assist_promotion_evidence_sha256": request_value[
                "precursor_assist_promotion_evidence_sha256"
            ],
            "source_revision_sha256": budget_document.source_revision_sha256,
            "registry_binding_sha256": request_value["registry_binding_sha256"],
            **identity,
            "observer_runner_sha256": representative_window_observer_runner_sha256(),
            **facts,
            "server_recomputed": True,
            "representative_window_attested": True,
            "synthetic_authority": False,
            "lookup_token_sha256": hashlib.sha256(lookup_token.encode("ascii")).hexdigest(),
            "state_version": 1,
            "issued_at": issued_at,
            "expires_at": issued_at + REPRESENTATIVE_WINDOW_ATTESTATION_TTL_SEC,
        }
        attestation = {**unsigned, "signature": _signature(key, unsigned)}
        if not validate_representative_window_attestation(
            key,
            attestation,
            now=issued_at,
            current_server_identity=identity,
        ):
            raise RepresentativeWindowAttestationError("issued attestation is invalid")
        attestation_sha256 = representative_window_sha256(attestation)
        response: dict[str, Any] = {
            "schema": REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA,
            "status": "unused",
            "server_attestation": attestation,
            "server_attestation_sha256": attestation_sha256,
            "attestation_lookup_token": lookup_token,
            "lookup_token_sha256": attestation["lookup_token_sha256"],
            "state_version": 1,
        }
        request_key = REPRESENTATIVE_WINDOW_REQUEST_KEY_PREFIX + attestation_id
        request_hash = representative_window_sha256(dict(request_value))
        stored = {
            **response,
            "attestation_lookup_token": None,
            "consume_state": "unused",
            "consumed_at": None,
            "consume_request_sha256": None,
            "consume_binding_sha256": None,
            "consumed_response_sha256": None,
            "consumed_response": None,
        }
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(issued_at))
        conn.execute(
            """INSERT INTO request_idempotency(
                   user_id,request_key,request_hash,response_json,state,lease_token,
                   created_at,updated_at
               ) VALUES(?,?,?,?,'complete','',?,?)""",
            (
                user_id,
                request_key,
                request_hash,
                json.dumps(stored, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        rows = conn.execute(
            """SELECT request_key FROM request_idempotency
                 WHERE user_id=? AND request_key LIKE ? AND request_key<>?
                   AND json_extract(response_json,'$.consume_state')='unused'
                 ORDER BY rowid DESC""",
            (user_id, REPRESENTATIVE_WINDOW_REQUEST_KEY_PREFIX + "%", request_key),
        ).fetchall()
        for stale in rows[_MAX_STORED_WITNESSES - 1 :]:
            stale_key = stale["request_key"] if hasattr(stale, "keys") else stale[0]
            conn.execute(
                "DELETE FROM request_idempotency WHERE user_id=? AND request_key=?",
                (user_id, stale_key),
            )
    return response


def consume_representative_window_attestation(
    storage: Any,
    *,
    user_id: str,
    request_value: Mapping[str, Any],
    current_server_identity: Mapping[str, Any],
    now: int | None = None,
) -> dict[str, Any]:
    """Atomically burn one exact server-origin representative-window witness."""

    if not validate_representative_window_consume_request(request_value):
        raise RepresentativeWindowAttestationError("consume request is invalid")
    identity = _validate_server_identity(current_server_identity)
    consumed_at = int(time.time()) if now is None else now
    if type(consumed_at) is not int or consumed_at <= 0:
        raise RepresentativeWindowAttestationError("consume time is invalid")
    lookup_sha256 = hashlib.sha256(str(request_value["attestation_lookup_token"]).encode("ascii")).hexdigest()

    with storage.transaction() as conn:
        key = _signing_key_from_transaction(conn)
        rows = conn.execute(
            """SELECT request_key,response_json FROM request_idempotency
                 WHERE user_id=? AND request_key LIKE ? AND state='complete'
                 ORDER BY rowid DESC LIMIT ?""",
            (
                user_id,
                REPRESENTATIVE_WINDOW_REQUEST_KEY_PREFIX + "%",
                _MAX_STORED_WITNESSES + 1,
            ),
        ).fetchall()
        matches: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            row_json = row["response_json"] if hasattr(row, "keys") else row[1]
            row_key = row["request_key"] if hasattr(row, "keys") else row[0]
            try:
                stored = json.loads(str(row_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            attestation = stored.get("server_attestation") if type(stored) is dict else None
            if type(attestation) is dict and hmac.compare_digest(
                str(attestation.get("lookup_token_sha256") or ""),
                lookup_sha256,
            ):
                matches.append((str(row_key), str(row_json), stored, attestation))
        if len(matches) != 1:
            raise RepresentativeWindowAttestationError("attestation was not found")
        request_key, old_json, stored, attestation = matches[0]
        if stored.get("consume_state") != "unused" or stored.get("state_version") != 1:
            request_sha256 = representative_window_sha256(dict(request_value))
            persisted = stored.get("consumed_response")
            issuer_epoch = attestation.get("primary_process_epoch_sha256")
            current_epoch = identity.get("primary_process_epoch_sha256")
            after_restart = not (
                _valid_digest(issuer_epoch)
                and _valid_digest(current_epoch)
                and hmac.compare_digest(str(issuer_epoch), str(current_epoch))
            )
            if (
                stored.get("consume_state") == "consumed"
                and stored.get("state_version") == 2
                and stored.get("consume_request_sha256") == request_sha256
                and type(persisted) is dict
                and representative_window_sha256(persisted) == stored.get("consumed_response_sha256")
            ):
                return _validated_consumed_response(
                    key,
                    persisted,
                    current_server_identity=identity,
                    now=consumed_at,
                    after_restart=after_restart,
                )
            raise RuntimeError("representative-window attestation was already consumed")
        if not validate_representative_window_attestation(
            key,
            attestation,
            now=consumed_at,
            current_server_identity=identity,
        ):
            raise RepresentativeWindowAttestationError("attestation is invalid or stale")
        if (
            not hmac.compare_digest(
                representative_window_sha256(attestation),
                str(request_value["server_attestation_sha256"]),
            )
            or not hmac.compare_digest(str(attestation["lookup_token_sha256"]), lookup_sha256)
            or any(
                request_value[name] != attestation[name]
                for name in (
                    "target_mode",
                    "baseline_file_sha256",
                    "baseline_report_sha256",
                    "latency_budget_file_sha256",
                    "latency_budget_document_sha256",
                    "source_revision_sha256",
                    "registry_binding_sha256",
                    "observer_runner_sha256",
                    "precursor_assist_promotion_evidence_sha256",
                )
            )
        ):
            raise RepresentativeWindowAttestationError("consume binding does not match")
        unsigned_response: dict[str, Any] = {
            "schema": REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_SCHEMA,
            "status": "consumed",
            "attestation_id": attestation["attestation_id"],
            "target_mode": attestation["target_mode"],
            "observed_mode": attestation["observed_mode"],
            "server_attestation_sha256": request_value["server_attestation_sha256"],
            "baseline_file_sha256": attestation["baseline_file_sha256"],
            "baseline_report_sha256": attestation["baseline_report_sha256"],
            "latency_budget_file_sha256": attestation["latency_budget_file_sha256"],
            "latency_budget_document_sha256": attestation["latency_budget_document_sha256"],
            "source_revision_sha256": attestation["source_revision_sha256"],
            "registry_binding_sha256": attestation["registry_binding_sha256"],
            "observer_runner_sha256": attestation["observer_runner_sha256"],
            "representative_window_sha256": attestation["representative_window_sha256"],
            "precursor_assist_promotion_evidence_sha256": attestation[
                "precursor_assist_promotion_evidence_sha256"
            ],
            "lookup_token_sha256": lookup_sha256,
            "consume_request_sha256": representative_window_sha256(dict(request_value)),
            "consumed_at": consumed_at,
            "state_version": 2,
        }
        response = {
            **unsigned_response,
            "consume_binding_sha256": _consume_signature(key, unsigned_response),
            "server_attestation": attestation,
        }
        consumed_response_sha256 = representative_window_sha256(response)
        updated = {
            **stored,
            "status": "consumed",
            "consume_state": "consumed",
            "consumed_at": consumed_at,
            "consume_request_sha256": unsigned_response["consume_request_sha256"],
            "consume_binding_sha256": response["consume_binding_sha256"],
            "consumed_response_sha256": consumed_response_sha256,
            "consumed_response": response,
            "state_version": 2,
        }
        changed = conn.execute(
            """UPDATE request_idempotency SET response_json=?,updated_at=?
                 WHERE user_id=? AND request_key=? AND response_json=? AND state='complete'""",
            (
                json.dumps(updated, ensure_ascii=False, sort_keys=True),
                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(consumed_at)),
                user_id,
                request_key,
                old_json,
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("representative-window consume CAS failed")
    return response


def _validated_consumed_response(
    key: bytes,
    value: object,
    *,
    current_server_identity: Mapping[str, Any],
    now: int,
    after_restart: bool,
) -> dict[str, Any]:
    item = _exact_mapping(
        value,
        REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_KEYS,
        label="representative-window consume response",
    )
    attestation = item["server_attestation"]
    consumed_at = item["consumed_at"]
    if (
        item["schema"] != REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_SCHEMA
        or item["status"] != "consumed"
        or item["state_version"] != 2
        or type(consumed_at) is not int
        or consumed_at <= 0
        or not validate_representative_window_attestation(
            key,
            attestation,
            now=now,
            current_server_identity=current_server_identity,
            consumed_at=consumed_at,
            after_restart=after_restart,
        )
        or not _valid_digest(item["consume_binding_sha256"])
    ):
        raise RepresentativeWindowAttestationError("consumed representative-window response is invalid")
    assert type(attestation) is dict
    unsigned = dict(item)
    unsigned.pop("consume_binding_sha256")
    unsigned.pop("server_attestation")
    if (
        not hmac.compare_digest(
            str(item["consume_binding_sha256"]),
            _consume_signature(key, unsigned),
        )
        or not hmac.compare_digest(
            representative_window_sha256(attestation),
            str(item["server_attestation_sha256"]),
        )
        or any(
            item.get(name) != attestation.get(name)
            for name in (
                "attestation_id",
                "target_mode",
                "observed_mode",
                "baseline_file_sha256",
                "baseline_report_sha256",
                "latency_budget_file_sha256",
                "latency_budget_document_sha256",
                "source_revision_sha256",
                "registry_binding_sha256",
                "observer_runner_sha256",
                "representative_window_sha256",
                "precursor_assist_promotion_evidence_sha256",
            )
        )
        or not all(
            _valid_digest(item[name])
            for name in (
                "server_attestation_sha256",
                "baseline_file_sha256",
                "baseline_report_sha256",
                "latency_budget_file_sha256",
                "latency_budget_document_sha256",
                "source_revision_sha256",
                "registry_binding_sha256",
                "observer_runner_sha256",
                "representative_window_sha256",
                "lookup_token_sha256",
                "consume_request_sha256",
            )
        )
    ):
        raise RepresentativeWindowAttestationError("consumed representative-window binding does not match")
    return item


def verify_consumed_representative_window_attestation(
    storage: Any,
    *,
    user_id: str,
    consumed_value: Mapping[str, Any],
    current_server_identity: Mapping[str, Any],
    now: int | None = None,
) -> AcceptedRepresentativeWindowAttestation:
    """Recheck a sidecar against the HMAC key and exact durable consumed row.

    Expiry is enforced at consume time.  A later restart instead re-attests the
    target release, registry and scheduler mode, so a valid consumed witness
    remains usable after its short issuance window without becoming replayable.
    """

    verified_at = int(time.time()) if now is None else now
    if type(verified_at) is not int or verified_at <= 0:
        raise RepresentativeWindowAttestationError("verification time is invalid")
    with storage.transaction() as conn:
        key = _signing_key_from_transaction(conn)
        raw_attestation = consumed_value.get("server_attestation")
        if type(raw_attestation) is not dict:
            raise RepresentativeWindowAttestationError("consumed response lacks its server attestation")
        issuer_epoch = raw_attestation.get("primary_process_epoch_sha256")
        current_epoch = current_server_identity.get("primary_process_epoch_sha256")
        after_restart = not (
            _valid_digest(issuer_epoch)
            and _valid_digest(current_epoch)
            and hmac.compare_digest(str(issuer_epoch), str(current_epoch))
        )
        item = _validated_consumed_response(
            key,
            consumed_value,
            current_server_identity=current_server_identity,
            now=verified_at,
            after_restart=after_restart,
        )
        request_key = REPRESENTATIVE_WINDOW_REQUEST_KEY_PREFIX + str(item["attestation_id"])
        row = conn.execute(
            """SELECT response_json,state FROM request_idempotency
                 WHERE user_id=? AND request_key=?""",
            (user_id, request_key),
        ).fetchone()
        if row is None:
            raise RepresentativeWindowAttestationError("consumed representative-window state is unavailable")
        row_json = row["response_json"] if hasattr(row, "keys") else row[0]
        row_state = row["state"] if hasattr(row, "keys") else row[1]
        try:
            stored = json.loads(str(row_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RepresentativeWindowAttestationError(
                "consumed representative-window state is malformed"
            ) from exc
        consumed_response_sha256 = representative_window_sha256(item)
        if (
            row_state != "complete"
            or type(stored) is not dict
            or stored.get("consume_state") != "consumed"
            or stored.get("state_version") != 2
            or stored.get("server_attestation") != item["server_attestation"]
            or stored.get("server_attestation_sha256") != item["server_attestation_sha256"]
            or stored.get("consumed_at") != item["consumed_at"]
            or stored.get("consume_request_sha256") != item["consume_request_sha256"]
            or stored.get("consume_binding_sha256") != item["consume_binding_sha256"]
            or stored.get("consumed_response_sha256") != consumed_response_sha256
            or stored.get("consumed_response") != item
        ):
            raise RepresentativeWindowAttestationError(
                "consumed representative-window durable state does not match"
            )
    attestation = cast(dict[str, Any], item["server_attestation"])
    try:
        target_mode = SupervisorMode(item["target_mode"])
        observed_mode = SupervisorMode(item["observed_mode"])
    except (TypeError, ValueError) as exc:  # pragma: no cover - closed above
        raise RepresentativeWindowAttestationError(
            "consumed representative-window modes are invalid"
        ) from exc
    fields: dict[str, Any] = {
        "attestation_id": item["attestation_id"],
        "target_mode": target_mode,
        "observed_mode": observed_mode,
        "baseline_file_sha256": item["baseline_file_sha256"],
        "baseline_report_sha256": item["baseline_report_sha256"],
        "latency_budget_file_sha256": item["latency_budget_file_sha256"],
        "latency_budget_document_sha256": item["latency_budget_document_sha256"],
        "latency_budget_maximum_ms": attestation["maximum_user_visible_latency_ms"],
        "source_revision_sha256": item["source_revision_sha256"],
        "registry_binding_sha256": item["registry_binding_sha256"],
        "observer_runner_sha256": item["observer_runner_sha256"],
        "representative_window_sha256": item["representative_window_sha256"],
        "precursor_assist_promotion_evidence_sha256": item["precursor_assist_promotion_evidence_sha256"],
        "server_attestation_sha256": item["server_attestation_sha256"],
        "consume_binding_sha256": item["consume_binding_sha256"],
        "consumed_response_sha256": consumed_response_sha256,
        "consumed_at": item["consumed_at"],
    }
    seal_payload = {
        **fields,
        "target_mode": target_mode.value,
        "observed_mode": observed_mode.value,
    }
    return AcceptedRepresentativeWindowAttestation(
        **fields,
        _process_authority=_PROCESS_ACCEPTANCE_AUTHORITY,
        _process_seal_sha256=_process_acceptance_seal(seal_payload),
    )


def verify_persisted_consumed_representative_window_issue(
    storage: Any,
    *,
    user_id: str,
    issue_value: Mapping[str, Any],
    current_server_identity: Mapping[str, Any],
    now: int | None = None,
) -> AcceptedRepresentativeWindowAttestation:
    """Load the exact consumed receipt named by a bundle's issue envelope."""

    if not validate_representative_window_issue_response(issue_value):
        raise RepresentativeWindowAttestationError("representative-window issue envelope is invalid")
    attestation = cast(dict[str, Any], issue_value["server_attestation"])
    request_key = REPRESENTATIVE_WINDOW_REQUEST_KEY_PREFIX + str(attestation["attestation_id"])
    with storage.transaction() as conn:
        row = conn.execute(
            """SELECT response_json,state FROM request_idempotency
                 WHERE user_id=? AND request_key=?""",
            (user_id, request_key),
        ).fetchone()
        if row is None:
            raise RepresentativeWindowAttestationError("representative-window durable state is unavailable")
        row_json = row["response_json"] if hasattr(row, "keys") else row[0]
        row_state = row["state"] if hasattr(row, "keys") else row[1]
        try:
            stored = json.loads(str(row_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RepresentativeWindowAttestationError(
                "representative-window durable state is malformed"
            ) from exc
        consumed = stored.get("consumed_response") if type(stored) is dict else None
        if (
            row_state != "complete"
            or type(stored) is not dict
            or stored.get("consume_state") != "consumed"
            or stored.get("state_version") != 2
            or stored.get("server_attestation") != attestation
            or stored.get("server_attestation_sha256") != issue_value["server_attestation_sha256"]
            or stored.get("lookup_token_sha256") != issue_value["lookup_token_sha256"]
            or type(consumed) is not dict
            or representative_window_sha256(consumed) != stored.get("consumed_response_sha256")
        ):
            raise RepresentativeWindowAttestationError(
                "representative-window issue envelope is not durably consumed"
            )
    return verify_consumed_representative_window_attestation(
        storage,
        user_id=user_id,
        consumed_value=consumed,
        current_server_identity=current_server_identity,
        now=now,
    )


__all__ = [
    "AcceptedRepresentativeWindowAttestation",
    "REPRESENTATIVE_WINDOW_ATTESTATION_KEYS",
    "REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA",
    "REPRESENTATIVE_WINDOW_AUTHORITY",
    "REPRESENTATIVE_WINDOW_CONSUME_REQUEST_KEYS",
    "REPRESENTATIVE_WINDOW_CONSUME_REQUEST_SCHEMA",
    "REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_KEYS",
    "REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_SCHEMA",
    "REPRESENTATIVE_WINDOW_ISSUE_REQUEST_KEYS",
    "REPRESENTATIVE_WINDOW_ISSUE_REQUEST_SCHEMA",
    "REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_KEYS",
    "REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA",
    "REPRESENTATIVE_WINDOW_SERVER_IDENTITY_KEYS",
    "RepresentativeWindowAttestationError",
    "consume_representative_window_attestation",
    "is_accepted_representative_window_attestation",
    "issue_representative_window_attestation",
    "representative_window_canonical",
    "representative_window_current_server_identity",
    "representative_window_observer_runner_sha256",
    "representative_window_sha256",
    "validate_representative_window_attestation",
    "validate_representative_window_consume_request",
    "validate_representative_window_issue_request",
    "validate_representative_window_issue_response",
    "verify_consumed_representative_window_attestation",
    "verify_persisted_consumed_representative_window_issue",
]
