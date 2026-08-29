"""Maturity-gated, advisory-only shadow observation of completed effects.

The wrapper is deliberately outside the primary runtime.  It calls the
primary exactly once and returns that exact object.  Only after the primary
has durably stored an accepted effect receipt may one bounded background
request ask the optional secondary model which closed effect symbol it would
have selected.  The answer is compared and discarded; it cannot execute,
publish, replay, compensate, or alter the primary result.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import CapabilityBindingSnapshot
from friday.orchestration.effect_outcome import (
    AcceptedEffectOutcomeReceipt,
    EffectOutcomeError,
    load_accepted_effect_outcome_receipt,
)
from friday.orchestration.supervisor_assist_activation import (
    derive_installed_release_tree_sha256,
)
from friday.orchestration.supervisor_effect_intent import (
    SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
    EffectIntentActionSelection,
    EffectIntentCapabilitySelection,
    EffectIntentError,
    EffectIntentProjectionV2,
    EffectIntentSelectionV2,
    derive_persisted_effect_intent_projection_identity,
    prepare_effect_intent_projection_v2,
)
from friday.orchestration.supervisor_effect_intent_transport import (
    SupervisorEffectIntentTransportError,
    select_supervisor_effect_intent,
)
from friday.orchestration.supervisor_effect_maturity import (
    AcceptedReadOnlyMaturityWitness,
    SupervisorEffectMaturityError,
    accepted_read_only_maturity_witness_is_current,
    load_accepted_read_only_maturity_witness,
)
from friday.orchestration.supervisor_trace_join import load_primary_trace_projection
from friday.orchestration.turn_context import AuthenticatedTurnContext
from friday.orchestration.turn_context_advisory import suspend_authenticated_advisory_authority
from friday.orchestration.turn_context_call_scope import (
    UNSPECIFIED_CHAT_ADJUNCT,
    require_authenticated_chat_call_scope,
)
from friday.orchestration.turn_context_runtime import (
    current_primary_authenticated_turn_context,
    reserve_authenticated_advisory_call,
)
from friday.permissions import ActorContext
from friday.secondary_brain import ModelRequest, ModelWorkload, SecondaryAttempt, SecondaryResult

SUPERVISOR_EFFECT_SHADOW_HEALTH_SCHEMA = "friday.semantic-supervisor-effect-shadow-health.v1"
SUPERVISOR_EFFECT_SHADOW_RUNTIME_SCHEMA = "friday.semantic-supervisor-effect-shadow-runtime.v1"
SUPERVISOR_EFFECT_SHADOW_OBSERVATION_SCHEMA = "friday.semantic-supervisor-effect-shadow-observation.v1"

_MAX_PENDING = 4
_MAX_EVIDENCE_BYTES = 4_194_304
_MAX_EVENT_BYTES = 8_192
_MODEL_BUDGET_SEC = 5.0
_CLOSE_DRAIN_SEC = 1.0
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_DEDUPE_KEY = secrets.token_bytes(32)
_PROCESS_PERSISTED_IDENTITY_KEY = secrets.token_bytes(32)
_PERSISTED_SELECTION_IDENTITY_CONTEXT = (
    b"friday.semantic-supervisor-effect-shadow.persisted-selection-identity.v1\x00"
)
_DEDUPE_BLOOM_ID = "fixed_hmac_sha256_bloom_v1"
_DEDUPE_BLOOM_CONTEXT = b"friday.semantic-supervisor-effect-shadow.dedupe-bloom.v1\x00"
_DEDUPE_EFFECT_ID_CONTEXT = b"accepted-effect-id\x00"
_DEDUPE_OUTCOME_ID_CONTEXT = b"accepted-outcome\x00"
_DEDUPE_IDENTITY = "accepted_effect_id_and_outcome_sha256_v1"
_DEDUPE_IDENTITY_COUNT = 2
_DEDUPE_BLOOM_BYTES = 512 * 1_024
_DEDUPE_BLOOM_BITS = _DEDUPE_BLOOM_BYTES * 8
_DEDUPE_BLOOM_HASH_COUNT = 7
_DEDUPE_BIT_PROBES_PER_RECEIPT = _DEDUPE_IDENTITY_COUNT * _DEDUPE_BLOOM_HASH_COUNT
# At-most-once is a process-lifetime invariant.  This bitset is never cleared
# or rotated in production, so it has no false negatives and an accepted
# effect/outcome cannot become dispatchable again under a different message
# scope.  Saturation can create false positives, but those only skip this
# optional observer.  Memory remains exactly 512 KiB while concurrent work is
# independently bounded by _MAX_PENDING.
_PROCESS_DEDUPE_BLOOM = bytearray(_DEDUPE_BLOOM_BYTES)
_PROCESS_DEDUPE_INSERT_TOTAL = 0
_PROCESS_DEDUPE_LOCK = threading.Lock()

_RUNTIME_STATUS_KEYS = frozenset(
    {
        "schema",
        "installed",
        "requested_mode",
        "effective_mode",
        "maturity_accepted",
        "evidence_sha256",
        "maturity_facts_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "effect_registry_binding_sha256",
        "policy_id",
        "policy_sha256",
        "workload",
        "runtime_owner",
        "publication_owner",
        "primary_result_unchanged",
        "tools_allowed",
        "effects_allowed",
        "execution_authorized",
        "publication_authorized",
        "max_pending",
        "pending",
        "dedupe_retention",
        "dedupe_algorithm",
        "dedupe_identity",
        "dedupe_identity_count",
        "dedupe_memory_bounded",
        "dedupe_memory_bytes",
        "dedupe_bit_capacity",
        "dedupe_hash_count",
        "dedupe_bit_probes_per_receipt",
        "dedupe_insert_total",
        "dispatch_total",
        "observation_total",
        "agreements",
        "skip_reasons",
        "body_free",
    }
)
_RUNTIME_AGREEMENT_KEYS = frozenset(
    {
        "selection_unavailable",
        "missed_actual_effect",
        "matched_actual_effect",
        "different_supported_effect",
    }
)
_RUNTIME_SKIP_KEYS = frozenset(
    {
        "persistence_unavailable",
        "durable_message_unavailable",
        "accepted_effect_receipt_unavailable",
        "admission_became_stale",
        "already_observed",
        "input_unavailable",
        "capacity",
        "projection_rejected",
        "primary_result_unavailable",
    }
)

RuntimeT = TypeVar("RuntimeT")


class _PrimaryRuntime(Protocol):
    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]: ...


class _EffectScheduler(Protocol):
    @property
    def served_model_alias(self) -> str: ...

    def product_attestation_identity(self) -> Mapping[str, object]: ...

    def diagnostics_status(self) -> Mapping[str, object]: ...

    def workload_mode(self, workload: ModelWorkload) -> object: ...

    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Callable[[], bool] | None = None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt: ...


class _EffectStorage(Protocol):
    def get_message(self, message_id: str, user_id: str) -> dict[str, Any] | None: ...

    def record_event(self, event_type: str, payload: dict[str, Any] | None = None) -> str: ...


def _requested_mode(settings: object) -> str:
    raw = getattr(settings, "semantic_supervisor_effect_mode", "off")
    return raw if type(raw) is str and raw in {"off", "shadow"} else "invalid"


def _closed_health(*, requested_mode: str, reason: str) -> dict[str, object]:
    return {
        "schema": SUPERVISOR_EFFECT_SHADOW_HEALTH_SCHEMA,
        "installed": False,
        "requested_mode": requested_mode,
        "effective_mode": "off",
        "maturity_accepted": False,
        "policy_id": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
        "policy_sha256": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
        "evidence_sha256": "",
        "maturity_facts_sha256": "",
        "source_revision_sha256": "",
        "registry_binding_sha256": "",
        "effect_registry_binding_sha256": "",
        "execution_authorized": False,
        "publication_authorized": False,
        "_reason": reason,
    }


def _positive_counter_projection(value: object, allowed_keys: frozenset[str]) -> bool:
    return bool(
        type(value) is dict
        and set(value).issubset(allowed_keys)
        and all(type(count) is int and count > 0 for count in value.values())
    )


def _runtime_status_contract_is_exact(source: object) -> bool:
    """Validate one complete, body-free status emitted by the installed wrapper."""

    if type(source) is not dict or set(source) != _RUNTIME_STATUS_KEYS:
        return False
    identity = tuple(
        source[key]
        for key in (
            "evidence_sha256",
            "maturity_facts_sha256",
            "source_revision_sha256",
            "registry_binding_sha256",
            "effect_registry_binding_sha256",
        )
    )
    active_identity = all(
        type(value) is str and _DIGEST_RE.fullmatch(value) is not None for value in identity
    )
    closed_identity = all(value == "" for value in identity)
    pending = source["pending"]
    dedupe_insert_total = source["dedupe_insert_total"]
    dispatch_total = source["dispatch_total"]
    observation_total = source["observation_total"]
    agreements = source["agreements"]
    skip_reasons = source["skip_reasons"]
    counters_are_exact = bool(
        type(pending) is int
        and 0 <= pending <= _MAX_PENDING
        and type(dedupe_insert_total) is int
        and dedupe_insert_total >= 0
        and type(dispatch_total) is int
        and dispatch_total >= 0
        and type(observation_total) is int
        and observation_total >= 0
        and _positive_counter_projection(agreements, _RUNTIME_AGREEMENT_KEYS)
        and _positive_counter_projection(skip_reasons, _RUNTIME_SKIP_KEYS)
    )
    active_lifecycle = bool(
        source["requested_mode"] == "shadow"
        and source["effective_mode"] == "shadow"
        and source["maturity_accepted"] is True
        and active_identity
    )
    closed_lifecycle = bool(
        source["effective_mode"] == "off" and source["maturity_accepted"] is False and closed_identity
    )
    lifecycle_is_exact = bool(
        type(source["requested_mode"]) is str
        and source["requested_mode"] in {"off", "shadow", "invalid"}
        and (active_lifecycle or closed_lifecycle)
    )
    return bool(
        source["schema"] == SUPERVISOR_EFFECT_SHADOW_RUNTIME_SCHEMA
        and source["installed"] is True
        and lifecycle_is_exact
        and source["policy_id"] == semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID
        and source["policy_sha256"] == semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256
        and source["workload"] == ModelWorkload.EFFECT_PLANNING.value
        and source["runtime_owner"] == "unchanged"
        and source["publication_owner"] == "primary"
        and source["primary_result_unchanged"] is True
        and source["tools_allowed"] is False
        and source["effects_allowed"] is False
        and source["execution_authorized"] is False
        and source["publication_authorized"] is False
        and source["max_pending"] == _MAX_PENDING
        and source["dedupe_retention"] == "process_lifetime"
        and source["dedupe_algorithm"] == _DEDUPE_BLOOM_ID
        and source["dedupe_identity"] == _DEDUPE_IDENTITY
        and source["dedupe_identity_count"] == _DEDUPE_IDENTITY_COUNT
        and source["dedupe_memory_bounded"] is True
        and source["dedupe_memory_bytes"] == _DEDUPE_BLOOM_BYTES
        and source["dedupe_bit_capacity"] == _DEDUPE_BLOOM_BITS
        and source["dedupe_hash_count"] == _DEDUPE_BLOOM_HASH_COUNT
        and source["dedupe_bit_probes_per_receipt"] == _DEDUPE_BIT_PROBES_PER_RECEIPT
        and source["body_free"] is True
        and counters_are_exact
    )


def supervisor_effect_shadow_health_status(
    runtime: object | None,
    activation_status: Mapping[str, object] | None,
    settings: object,
) -> dict[str, object]:
    """Return the exact public health contract expected by the release gate."""

    raw: dict[str, object] | None = None
    runtime_attested = False
    try:
        method = getattr(runtime, "semantic_supervisor_effect_status", None)
    except Exception:
        method = None
    if callable(method):
        try:
            candidate = method()
        except Exception:
            candidate = None
        if type(candidate) is dict:
            raw = cast(dict[str, object], candidate)
            runtime_attested = True
    if raw is None and type(activation_status) is dict:
        raw = cast(dict[str, object], activation_status)
    closed = _closed_health(
        requested_mode=_requested_mode(settings),
        reason="runtime_unavailable",
    )
    source = raw if raw is not None else closed
    raw_requested_mode = source.get("requested_mode")
    requested_mode = (
        raw_requested_mode
        if type(raw_requested_mode) is str and raw_requested_mode in {"off", "shadow", "invalid"}
        else "invalid"
    )
    configured_evidence_sha256 = getattr(
        settings,
        "semantic_supervisor_effect_evidence_sha256",
        "",
    )
    identity = tuple(
        source.get(key)
        for key in (
            "evidence_sha256",
            "maturity_facts_sha256",
            "source_revision_sha256",
            "registry_binding_sha256",
            "effect_registry_binding_sha256",
        )
    )
    identity_is_exact = bool(
        all(type(value) is str and _DIGEST_RE.fullmatch(value) is not None for value in identity)
        and type(configured_evidence_sha256) is str
        and hmac.compare_digest(cast(str, identity[0]), configured_evidence_sha256)
    )
    try:
        runtime_contract_is_exact = bool(runtime_attested and _runtime_status_contract_is_exact(source))
    except Exception:
        runtime_contract_is_exact = False
    active = bool(
        runtime_contract_is_exact
        and requested_mode == "shadow"
        and source.get("effective_mode") == "shadow"
        and source.get("maturity_accepted") is True
        and identity_is_exact
    )
    return {
        "schema": SUPERVISOR_EFFECT_SHADOW_HEALTH_SCHEMA,
        "installed": runtime_contract_is_exact,
        "requested_mode": requested_mode,
        "effective_mode": "shadow" if active else "off",
        "maturity_accepted": active,
        "policy_id": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
        "policy_sha256": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
        "evidence_sha256": cast(str, identity[0]) if active else "",
        "maturity_facts_sha256": cast(str, identity[1]) if active else "",
        "source_revision_sha256": cast(str, identity[2]) if active else "",
        "registry_binding_sha256": cast(str, identity[3]) if active else "",
        "effect_registry_binding_sha256": cast(str, identity[4]) if active else "",
        "execution_authorized": False,
        "publication_authorized": False,
    }


def _stable_private_file(path: Path) -> bytes:
    """Read one same-owner, no-link, immutable-looking evidence file."""

    if not isinstance(path, Path) or not path.is_absolute() or len(str(path)) > 4_096:
        raise SupervisorEffectMaturityError("maturity evidence path is invalid")
    lexical = Path(os.path.abspath(path))
    if lexical != path:
        raise SupervisorEffectMaturityError("maturity evidence path is invalid")
    parent_fd = -1
    file_fd = -1
    try:
        if lexical.resolve(strict=True) != lexical:
            raise SupervisorEffectMaturityError("maturity evidence path is not canonical")
        parent_fd = os.open(
            lexical.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_before = os.fstat(parent_fd)
        file_fd = os.open(
            lexical.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) not in {0o400, 0o600}
            or not 0 < before.st_size <= _MAX_EVIDENCE_BYTES
        ):
            raise SupervisorEffectMaturityError("maturity evidence file is not admissible")
        chunks: list[bytes] = []
        size = 0
        while size <= _MAX_EVIDENCE_BYTES:
            chunk = os.read(file_fd, min(1 << 20, _MAX_EVIDENCE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(file_fd)
        parent_after = os.fstat(parent_fd)
        stable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != before.st_size
            or len(raw) > _MAX_EVIDENCE_BYTES
            or any(getattr(before, field) != getattr(after, field) for field in stable)
            or any(
                getattr(parent_before, field) != getattr(parent_after, field)
                for field in ("st_dev", "st_ino", "st_mode", "st_uid", "st_mtime_ns", "st_ctime_ns")
            )
            or lexical.resolve(strict=True) != lexical
        ):
            raise SupervisorEffectMaturityError("maturity evidence changed while loading")
        return raw
    except SupervisorEffectMaturityError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SupervisorEffectMaturityError("maturity evidence is unavailable") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def load_configured_supervisor_effect_maturity(
    settings: object,
    *,
    installed_release_root: Path,
    binding_snapshot: object,
    effect_binding_snapshot: object,
) -> tuple[AcceptedReadOnlyMaturityWitness | None, dict[str, object]]:
    """Accept one exact current-release witness or return an inert status."""

    requested = _requested_mode(settings)
    closed = _closed_health(requested_mode=requested, reason="default_off")
    if requested != "shadow":
        return None, closed
    evidence_file = getattr(settings, "semantic_supervisor_effect_evidence_file", "")
    evidence_sha256 = getattr(settings, "semantic_supervisor_effect_evidence_sha256", "")
    if (
        type(evidence_file) is not str
        or not evidence_file
        or type(evidence_sha256) is not str
        or _DIGEST_RE.fullmatch(evidence_sha256) is None
        or type(binding_snapshot) is not CapabilityBindingSnapshot
        or type(effect_binding_snapshot) is not CapabilityBindingSnapshot
    ):
        closed["_reason"] = "raw_settings_invalid"
        return None, closed
    try:
        source_revision_sha256 = derive_installed_release_tree_sha256(installed_release_root)
        registry_binding_sha256 = binding_snapshot.digest_hex()
        effect_registry_binding_sha256 = effect_binding_snapshot.digest_hex()
        if (
            source_revision_sha256 is None
            or _DIGEST_RE.fullmatch(registry_binding_sha256) is None
            or _DIGEST_RE.fullmatch(effect_registry_binding_sha256) is None
        ):
            raise SupervisorEffectMaturityError("current release identity is unavailable")
        witness = load_accepted_read_only_maturity_witness(
            _stable_private_file(Path(evidence_file)),
            expected_file_sha256=evidence_sha256,
            expected_source_revision_sha256=source_revision_sha256,
            expected_registry_binding_sha256=registry_binding_sha256,
            expected_effect_registry_binding_sha256=effect_registry_binding_sha256,
        )
        if not accepted_read_only_maturity_witness_is_current(witness):
            raise SupervisorEffectMaturityError("maturity witness is stale")
    except (OSError, RuntimeError, TypeError, ValueError, SupervisorEffectMaturityError):
        closed["_reason"] = "maturity_evidence_rejected"
        return None, closed
    return witness, {
        **closed,
        "maturity_accepted": True,
        "_reason": "maturity_evidence_accepted",
        "evidence_sha256": evidence_sha256,
        "maturity_facts_sha256": witness.maturity_facts_sha256,
        "source_revision_sha256": witness.source_revision_sha256,
        "registry_binding_sha256": witness.registry_binding_sha256,
        "effect_registry_binding_sha256": witness.effect_registry_binding_sha256,
    }


def _effect_workload_admitted(scheduler: object | None) -> bool:
    method = getattr(scheduler, "workload_mode", None)
    if not callable(method):
        return False
    try:
        mode = method(ModelWorkload.EFFECT_PLANNING)
    except Exception:
        return False
    return str(getattr(mode, "value", mode) or "").strip().casefold() == "shadow"


def _configured_maturity_identity_is_current(
    settings: object,
    maturity_witness: object,
) -> bool:
    evidence_sha256 = getattr(settings, "semantic_supervisor_effect_evidence_sha256", "")
    try:
        witness_identity = (
            getattr(maturity_witness, "maturity_facts_sha256", None),
            getattr(maturity_witness, "source_revision_sha256", None),
            getattr(maturity_witness, "registry_binding_sha256", None),
            getattr(maturity_witness, "effect_registry_binding_sha256", None),
        )
        artifact_file_sha256 = getattr(maturity_witness, "artifact_file_sha256", None)
        return bool(
            type(evidence_sha256) is str
            and _DIGEST_RE.fullmatch(evidence_sha256) is not None
            and type(artifact_file_sha256) is str
            and hmac.compare_digest(evidence_sha256, artifact_file_sha256)
            and all(
                type(value) is str and _DIGEST_RE.fullmatch(value) is not None for value in witness_identity
            )
        )
    except (AttributeError, TypeError):
        return False


def build_supervisor_effect_intent_runtime(
    settings: object,
    primary: RuntimeT,
    scheduler: object | None,
    storage: object | None,
    maturity_witness: AcceptedReadOnlyMaturityWitness | None,
) -> RuntimeT | SupervisorEffectIntentShadowRuntime:
    """Install only with all independent maturity, scheduler, and storage gates."""

    if (
        _requested_mode(settings) != "shadow"
        or not accepted_read_only_maturity_witness_is_current(maturity_witness)
        or not _configured_maturity_identity_is_current(settings, maturity_witness)
        or not _effect_workload_admitted(scheduler)
        or not callable(getattr(storage, "get_message", None))
        or not callable(getattr(storage, "record_event", None))
    ):
        return primary
    return SupervisorEffectIntentShadowRuntime(
        settings=settings,
        primary=cast(_PrimaryRuntime, primary),
        scheduler=cast(_EffectScheduler, scheduler),
        storage=cast(_EffectStorage, storage),
        maturity_witness=cast(AcceptedReadOnlyMaturityWitness, maturity_witness),
    )


class SupervisorEffectIntentShadowRuntime:
    """Non-owning post-commit observer for one mature effect contour.

    Receipt membership is retained in a fixed process-lifetime Bloom filter to
    make the one-dispatch claim non-evicting.  False positives can skip this
    optional observer; false negatives and memory growth are forbidden.
    """

    def __init__(
        self,
        *,
        settings: object,
        primary: _PrimaryRuntime,
        scheduler: _EffectScheduler,
        storage: _EffectStorage,
        maturity_witness: AcceptedReadOnlyMaturityWitness,
    ) -> None:
        if not accepted_read_only_maturity_witness_is_current(maturity_witness):
            raise SupervisorEffectMaturityError("effect shadow requires a current witness")
        self._settings = settings
        self._primary = primary
        self._scheduler = scheduler
        self._storage = storage
        self._maturity = maturity_witness
        self._evidence_sha256 = cast(
            str,
            getattr(settings, "semantic_supervisor_effect_evidence_sha256", ""),
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._skip_counts: Counter[str] = Counter()
        self._agreement_counts: Counter[str] = Counter()
        self._observation_total = 0
        self._dispatch_total = 0
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)

    def semantic_supervisor_effect_status(self) -> dict[str, object]:
        current = bool(
            accepted_read_only_maturity_witness_is_current(self._maturity)
            and _configured_maturity_identity_is_current(self._settings, self._maturity)
        )
        active = bool(not self._closed and current and _effect_workload_admitted(self._scheduler))
        with _PROCESS_DEDUPE_LOCK:
            dedupe_insert_total = _PROCESS_DEDUPE_INSERT_TOTAL
        return {
            "schema": SUPERVISOR_EFFECT_SHADOW_RUNTIME_SCHEMA,
            "installed": True,
            "requested_mode": _requested_mode(self._settings),
            "effective_mode": "shadow" if active else "off",
            "maturity_accepted": active,
            "evidence_sha256": self._evidence_sha256 if active else "",
            "maturity_facts_sha256": self._maturity.maturity_facts_sha256 if active else "",
            "source_revision_sha256": self._maturity.source_revision_sha256 if active else "",
            "registry_binding_sha256": self._maturity.registry_binding_sha256 if active else "",
            "effect_registry_binding_sha256": (
                self._maturity.effect_registry_binding_sha256 if active else ""
            ),
            "policy_id": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
            "policy_sha256": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
            "workload": ModelWorkload.EFFECT_PLANNING.value,
            "runtime_owner": "unchanged",
            "publication_owner": "primary",
            "primary_result_unchanged": True,
            "tools_allowed": False,
            "effects_allowed": False,
            "execution_authorized": False,
            "publication_authorized": False,
            "max_pending": _MAX_PENDING,
            "pending": len(self._tasks),
            "dedupe_retention": "process_lifetime",
            "dedupe_algorithm": _DEDUPE_BLOOM_ID,
            "dedupe_identity": _DEDUPE_IDENTITY,
            "dedupe_identity_count": _DEDUPE_IDENTITY_COUNT,
            "dedupe_memory_bounded": True,
            "dedupe_memory_bytes": _DEDUPE_BLOOM_BYTES,
            "dedupe_bit_capacity": _DEDUPE_BLOOM_BITS,
            "dedupe_hash_count": _DEDUPE_BLOOM_HASH_COUNT,
            "dedupe_bit_probes_per_receipt": _DEDUPE_BIT_PROBES_PER_RECEIPT,
            "dedupe_insert_total": dedupe_insert_total,
            "dispatch_total": self._dispatch_total,
            "observation_total": self._observation_total,
            "agreements": dict(sorted(self._agreement_counts.items())),
            "skip_reasons": dict(sorted(self._skip_counts.items())),
            "body_free": True,
        }

    @staticmethod
    def _remember_accepted_receipt_once(receipt: object) -> bool:
        """Atomically retain one accepted operation/outcome without raw data."""

        global _PROCESS_DEDUPE_INSERT_TOTAL

        if type(receipt) is not AcceptedEffectOutcomeReceipt:
            return False
        identities = (
            (_DEDUPE_EFFECT_ID_CONTEXT, receipt.outcome.effect_id_sha256),
            (_DEDUPE_OUTCOME_ID_CONTEXT, receipt.outcome_sha256),
        )
        position_groups: list[tuple[int, ...]] = []
        for identity_context, identity_sha256 in identities:
            digest = hmac.new(
                _PROCESS_DEDUPE_KEY,
                _DEDUPE_BLOOM_CONTEXT + identity_context + identity_sha256.encode("ascii", errors="strict"),
                hashlib.sha256,
            ).digest()
            first = int.from_bytes(digest[:8], "big")
            # An odd stride visits the full power-of-two bit range before wrapping.
            stride = int.from_bytes(digest[8:16], "big") | 1
            position_groups.append(
                tuple(
                    (first + index * stride) & (_DEDUPE_BLOOM_BITS - 1)
                    for index in range(_DEDUPE_BLOOM_HASH_COUNT)
                )
            )
        # One lock makes both independent memberships a single at-most-once
        # boundary: an exact receipt replay or a newer outcome for the same
        # effect fails off before model dispatch.
        with _PROCESS_DEDUPE_LOCK:
            if any(
                all(_PROCESS_DEDUPE_BLOOM[position >> 3] & (1 << (position & 7)) for position in positions)
                for positions in position_groups
            ):
                return False
            for positions in position_groups:
                for position in positions:
                    _PROCESS_DEDUPE_BLOOM[position >> 3] |= 1 << (position & 7)
            _PROCESS_DEDUPE_INSERT_TOTAL += 1
        return True

    @staticmethod
    def _selection_identity(selection: EffectIntentSelectionV2) -> str:
        return hmac.new(
            _PROCESS_PERSISTED_IDENTITY_KEY,
            _PERSISTED_SELECTION_IDENTITY_CONTEXT + selection.canonical_sha256().encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _agreement(
        selection: EffectIntentSelectionV2 | None,
        receipt: AcceptedEffectOutcomeReceipt,
    ) -> str:
        if selection is None:
            return "selection_unavailable"
        if (
            selection.capability is EffectIntentCapabilitySelection.NONE
            and selection.action is EffectIntentActionSelection.NONE
        ):
            return "missed_actual_effect"
        if (
            selection.capability.value == receipt.outcome.capability.value
            and selection.action.value == receipt.outcome.action.value
        ):
            return "matched_actual_effect"
        return "different_supported_effect"

    def _persist(
        self,
        *,
        projection: EffectIntentProjectionV2,
        receipt: AcceptedEffectOutcomeReceipt,
        selection: EffectIntentSelectionV2 | None,
        selection_status: str,
        primary_trace_digest: str,
    ) -> None:
        agreement = self._agreement(selection, receipt)
        payload: dict[str, object] = {
            "schema": SUPERVISOR_EFFECT_SHADOW_OBSERVATION_SCHEMA,
            "body_free": True,
            "policy_id": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
            "policy_sha256": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
            "maturity_facts_sha256": self._maturity.maturity_facts_sha256,
            "projection_identity_sha256": derive_persisted_effect_intent_projection_identity(
                projection,
                namespace_key=_PROCESS_PERSISTED_IDENTITY_KEY,
            ),
            "symbol_manifest_sha256": SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
            "selection_status": selection_status,
            "selection_identity_sha256": (
                self._selection_identity(selection) if selection is not None else ""
            ),
            "selected_capability": selection.capability.value if selection is not None else "",
            "selected_action": selection.action.value if selection is not None else "",
            "actual_capability": receipt.outcome.capability.value,
            "actual_action": receipt.outcome.action.value,
            "actual_status": receipt.outcome.status.value,
            "actual_reconciliation": receipt.outcome.reconciliation.value,
            "actual_outcome_sha256": receipt.outcome_sha256,
            "authority_rechecked": receipt.outcome.authority_rechecked,
            "primary_trace_digest": primary_trace_digest,
            "agreement": agreement,
            "execution_authorized": False,
            "publication_authorized": False,
            "primary_result_unchanged": True,
        }
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            if len(encoded) > _MAX_EVENT_BYTES:
                raise ValueError("effect shadow observation exceeds its bound")
            self._storage.record_event("semantic_supervisor.effect_shadow", payload)
        except Exception:
            self._skip_counts["persistence_unavailable"] += 1
            return
        self._observation_total += 1
        self._agreement_counts[agreement] += 1

    async def _observe(
        self,
        *,
        user_id: str,
        message_id: str,
        conversation_id: str,
        projection: EffectIntentProjectionV2,
        absolute_deadline_monotonic: float | None,
    ) -> None:
        try:
            row = self._storage.get_message(message_id, user_id)
            if (
                type(row) is not dict
                or row.get("id") != message_id
                or row.get("user_id") != user_id
                or row.get("conversation_id") != conversation_id
                or row.get("role") != "assistant"
            ):
                self._skip_counts["durable_message_unavailable"] += 1
                return
            receipt = load_accepted_effect_outcome_receipt(row.get("metadata_json"))
            trace = load_primary_trace_projection(
                self._primary,
                {"message_id": message_id, "conversation_id": conversation_id},
            )
        except (EffectOutcomeError, TypeError, ValueError):
            self._skip_counts["accepted_effect_receipt_unavailable"] += 1
            return
        except Exception:
            self._skip_counts["durable_message_unavailable"] += 1
            return

        if (
            self._closed
            or _requested_mode(self._settings) != "shadow"
            or not accepted_read_only_maturity_witness_is_current(self._maturity)
            or not _configured_maturity_identity_is_current(self._settings, self._maturity)
            or not _effect_workload_admitted(self._scheduler)
        ):
            self._skip_counts["admission_became_stale"] += 1
            return
        if not self._remember_accepted_receipt_once(receipt):
            self._skip_counts["already_observed"] += 1
            return
        selection: EffectIntentSelectionV2 | None = None
        selection_status = "model_unavailable"
        try:
            now = time.monotonic()
            deadline = now + _MODEL_BUDGET_SEC
            if absolute_deadline_monotonic is not None:
                deadline = min(deadline, absolute_deadline_monotonic)
            if deadline <= now:
                self._skip_counts["admission_became_stale"] += 1
                return
            self._dispatch_total += 1
            selection = await select_supervisor_effect_intent(
                self._scheduler,
                projection=projection,
                absolute_deadline_monotonic=deadline,
            )
            selection_status = "accepted"
        except asyncio.CancelledError:
            raise
        except SupervisorEffectIntentTransportError as exc:
            selection_status = exc.failure.value
        except Exception:
            selection_status = "runtime_error"
        if (
            self._closed
            or _requested_mode(self._settings) != "shadow"
            or not accepted_read_only_maturity_witness_is_current(self._maturity)
            or not _configured_maturity_identity_is_current(self._settings, self._maturity)
            or not _effect_workload_admitted(self._scheduler)
        ):
            self._skip_counts["admission_became_stale"] += 1
            return
        self._persist(
            projection=projection,
            receipt=receipt,
            selection=selection,
            selection_status=selection_status,
            primary_trace_digest=trace.trace_digest if trace is not None else "",
        )

    def _schedule(
        self,
        *,
        user_id: object,
        message_id: object,
        conversation_id: object,
        projection: EffectIntentProjectionV2 | None,
        authenticated_context: AuthenticatedTurnContext | None,
        absolute_deadline_monotonic: float | None,
    ) -> None:
        if (
            self._closed
            or type(projection) is not EffectIntentProjectionV2
            or type(user_id) is not str
            or not 1 <= len(user_id) <= 200
            or type(message_id) is not str
            or not 1 <= len(message_id) <= 200
            or type(conversation_id) is not str
            or not 1 <= len(conversation_id) <= 200
        ):
            self._skip_counts["input_unavailable"] += 1
            return
        if len(self._tasks) >= _MAX_PENDING:
            self._skip_counts["capacity"] += 1
            return
        observation = self._observe(
            user_id=user_id,
            message_id=message_id,
            conversation_id=conversation_id,
            projection=projection,
            absolute_deadline_monotonic=(
                absolute_deadline_monotonic if authenticated_context is not None else None
            ),
        )
        task: asyncio.Task[None] | None = None
        try:
            if authenticated_context is not None:
                reserve_authenticated_advisory_call(authenticated_context)
            with suspend_authenticated_advisory_authority():
                task = asyncio.create_task(
                    observation,
                    name="semantic-supervisor-effect-shadow",
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception:
            # This observer is optional and runs only after a successful
            # primary response.  In particular, the authenticated deadline
            # can expire after the shared slot is reserved but before the
            # suspension seam admits task creation.  Scheduling failure must
            # therefore remain a closed observation skip, never replace the
            # already-successful primary response.
            if task is None:
                observation.close()
            else:
                task.cancel()
                self._tasks.discard(task)
                if task.done() and not task.cancelled():
                    with suppress(Exception):
                        task.exception()
            self._skip_counts["capacity"] += 1

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        """Call primary exactly once; any later observation is non-owning."""

        authenticated_context = current_primary_authenticated_turn_context(
            cast(AuthenticatedTurnContext | None, kwargs.get("_authenticated_turn_context"))
        )
        kg = kwargs.get("kg", UNSPECIFIED_CHAT_ADJUNCT)
        hybrid_searcher = kwargs.get("hybrid_searcher", UNSPECIFIED_CHAT_ADJUNCT)
        ingestion_result = kwargs.get("ingestion_result", UNSPECIFIED_CHAT_ADJUNCT)
        authenticated_scope = (
            require_authenticated_chat_call_scope(
                authenticated_context,
                user_id=user_id,
                message=message,
                actor=cast(ActorContext, kwargs.get("actor")),
                conversation_id=kwargs.get("conversation_id"),
                attachments=kwargs.get("attachments"),
                enable_tools=kwargs.get("enable_tools", True),
                synthetic_document_notice=kwargs.get("synthetic_document_notice", False),
                replay_source_message_id=kwargs.get("replay_source_message_id"),
                mode=kwargs.get("mode"),
                answer_with_voice=kwargs.get("answer_with_voice", False),
                reply_to=kwargs.get("reply_to"),
                quoted_attachment_reference=kwargs.get("quoted_attachment_reference", False),
                reply_assistant_reference=kwargs.get("reply_assistant_reference", False),
                reply_assistant_message_id=kwargs.get("reply_assistant_message_id"),
                turn_policy=kwargs.get("turn_policy"),
                telegram_update_id=kwargs.get("telegram_update_id"),
                turn_deadline=kwargs.get("turn_deadline"),
                pending_durable_admission=kwargs.get("_pending_durable_admission"),
                kg=kg,
                hybrid_searcher=hybrid_searcher,
                ingestion_result=ingestion_result,
            )
            if authenticated_context is not None
            else None
        )
        projection: EffectIntentProjectionV2 | None = None
        if not self._closed and accepted_read_only_maturity_witness_is_current(self._maturity):
            try:
                projection = prepare_effect_intent_projection_v2(
                    authenticated_scope.model_input.message if authenticated_scope is not None else message
                )
            except (EffectIntentError, TypeError, UnicodeError, ValueError):
                self._skip_counts["projection_rejected"] += 1
        primary_kwargs = kwargs
        if authenticated_context is not None:
            assert authenticated_scope is not None
            primary_kwargs = dict(kwargs)
            primary_kwargs["_authenticated_turn_context"] = authenticated_context
            primary_kwargs.update(authenticated_scope.exact_service_kwargs())
            revalidated_scope = require_authenticated_chat_call_scope(
                authenticated_context,
                user_id=user_id,
                message=message,
                actor=cast(ActorContext, kwargs.get("actor")),
                conversation_id=kwargs.get("conversation_id"),
                attachments=kwargs.get("attachments"),
                enable_tools=kwargs.get("enable_tools", True),
                synthetic_document_notice=kwargs.get("synthetic_document_notice", False),
                replay_source_message_id=kwargs.get("replay_source_message_id"),
                mode=kwargs.get("mode"),
                answer_with_voice=kwargs.get("answer_with_voice", False),
                reply_to=kwargs.get("reply_to"),
                quoted_attachment_reference=kwargs.get("quoted_attachment_reference", False),
                reply_assistant_reference=kwargs.get("reply_assistant_reference", False),
                reply_assistant_message_id=kwargs.get("reply_assistant_message_id"),
                turn_policy=kwargs.get("turn_policy"),
                telegram_update_id=kwargs.get("telegram_update_id"),
                turn_deadline=kwargs.get("turn_deadline"),
                pending_durable_admission=kwargs.get("_pending_durable_admission"),
                kg=kg,
                hybrid_searcher=hybrid_searcher,
                ingestion_result=ingestion_result,
            )
            primary_kwargs.update(revalidated_scope.exact_service_kwargs())
        result = await self._primary.chat(user_id, message, **primary_kwargs)
        if type(result) is dict:
            self._schedule(
                user_id=user_id,
                message_id=result.get("message_id"),
                conversation_id=result.get("conversation_id"),
                projection=projection,
                authenticated_context=authenticated_context,
                absolute_deadline_monotonic=(
                    authenticated_scope.deadline_monotonic if authenticated_scope is not None else None
                ),
            )
        else:
            self._skip_counts["primary_result_unavailable"] += 1
        return result

    async def close(self) -> None:
        """Boundedly cancel only work owned by this non-owning wrapper."""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(TimeoutError):
                async with asyncio.timeout(_CLOSE_DRAIN_SEC):
                    await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


__all__ = [
    "SUPERVISOR_EFFECT_SHADOW_HEALTH_SCHEMA",
    "SUPERVISOR_EFFECT_SHADOW_OBSERVATION_SCHEMA",
    "SUPERVISOR_EFFECT_SHADOW_RUNTIME_SCHEMA",
    "SupervisorEffectIntentShadowRuntime",
    "build_supervisor_effect_intent_runtime",
    "load_configured_supervisor_effect_maturity",
    "supervisor_effect_shadow_health_status",
]
