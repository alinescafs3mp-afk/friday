from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration.effect_outcome import (
    AcceptedEffectOutcomeReceipt,
    EffectAction,
    EffectCapability,
    EffectCompensationState,
    EffectObservationState,
    EffectObservationsV1,
    EffectOutcomeV1,
    EffectPublishability,
    EffectReconciliationState,
    EffectStatus,
    attach_accepted_effect_outcome_receipt,
    load_accepted_effect_outcome_receipt,
)
from friday.orchestration.supervisor_effect_intent import (
    SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
    EffectIntentActionSelection,
    EffectIntentCapabilitySelection,
    EffectIntentSelectionV2,
    prepare_effect_intent_projection_v2,
)
from friday.orchestration.supervisor_effect_intent_runtime import (
    SUPERVISOR_EFFECT_SHADOW_HEALTH_SCHEMA,
    SupervisorEffectIntentShadowRuntime,
    build_supervisor_effect_intent_runtime,
    supervisor_effect_shadow_health_status,
)
from friday.secondary_brain import ModelWorkload


def _outcome() -> EffectOutcomeV1:
    return EffectOutcomeV1(
        effect_id_sha256="a" * 64,
        work_item_sha256="b" * 64,
        capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
        action=EffectAction.CREATE,
        request_sha256="c" * 64,
        authorization_basis_sha256="d" * 64,
        idempotency_key_sha256="e" * 64,
        status=EffectStatus.SUCCEEDED,
        reconciliation=EffectReconciliationState.NOT_REQUIRED,
        compensation=EffectCompensationState.NOT_REQUIRED,
        side_effect_receipt_sha256="f" * 64,
        compensation_receipt_sha256=None,
        evidence_sha256="1" * 64,
        observations=EffectObservationsV1(
            server_sync=EffectObservationState.PENDING,
            reingest=EffectObservationState.PENDING,
            physical_device=EffectObservationState.PENDING,
        ),
        publishability=EffectPublishability.ACCEPTED_FACTS,
        authority_rechecked=True,
    )


class _Primary:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0
        self.args: list[tuple[str, str, dict[str, Any]]] = []

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> Any:
        self.calls += 1
        self.args.append((user_id, message, kwargs))
        return self.result


class _Scheduler:
    def workload_mode(self, workload: ModelWorkload) -> str:
        assert workload is ModelWorkload.EFFECT_PLANNING
        return "shadow"


class _Storage:
    def __init__(self, *, receipt: bool = True) -> None:
        metadata: dict[str, Any] = {}
        if receipt:
            attach_accepted_effect_outcome_receipt(metadata, _outcome())
        self.row = {
            "id": "message-1",
            "user_id": "user-1",
            "conversation_id": "conversation-1",
            "role": "assistant",
            "metadata_json": json.dumps(metadata),
        }
        self.events: list[tuple[str, dict[str, Any]]] = []

    def get_message(self, message_id: str, user_id: str) -> dict[str, Any] | None:
        return dict(self.row) if (message_id, user_id) == ("message-1", "user-1") else None

    def record_event(self, event_type: str, payload: dict[str, Any] | None = None) -> str:
        assert payload is not None
        self.events.append((event_type, dict(payload)))
        return "event-1"


def _settings(mode: str = "shadow", *, evidence_sha256: str = "1" * 64) -> SimpleNamespace:
    return SimpleNamespace(
        semantic_supervisor_effect_mode=mode,
        semantic_supervisor_effect_evidence_sha256=evidence_sha256,
    )


def _witness() -> SimpleNamespace:
    return SimpleNamespace(
        artifact_file_sha256="1" * 64,
        maturity_facts_sha256="2" * 64,
        source_revision_sha256="3" * 64,
        registry_binding_sha256="4" * 64,
        effect_registry_binding_sha256="5" * 64,
    )


@pytest.fixture(autouse=True)
def _isolate_process_dedupe() -> Any:
    import friday.orchestration.supervisor_effect_intent_runtime as runtime

    with runtime._PROCESS_DEDUPE_LOCK:  # noqa: SLF001
        runtime._PROCESS_DEDUPE_BLOOM[:] = bytes(  # noqa: SLF001
            len(runtime._PROCESS_DEDUPE_BLOOM)  # noqa: SLF001
        )
        runtime._PROCESS_DEDUPE_INSERT_TOTAL = 0  # noqa: SLF001
    yield
    with runtime._PROCESS_DEDUPE_LOCK:  # noqa: SLF001
        runtime._PROCESS_DEDUPE_BLOOM[:] = bytes(  # noqa: SLF001
            len(runtime._PROCESS_DEDUPE_BLOOM)  # noqa: SLF001
        )
        runtime._PROCESS_DEDUPE_INSERT_TOTAL = 0  # noqa: SLF001


def _admit_witness(monkeypatch: pytest.MonkeyPatch, witness: object) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "accepted_read_only_maturity_witness_is_current",
        lambda value: value is witness,
    )


async def _drain(wrapper: SupervisorEffectIntentShadowRuntime) -> None:
    for _ in range(10):
        if not wrapper.semantic_supervisor_effect_status()["pending"]:
            return
        await asyncio.sleep(0)
    raise AssertionError("effect shadow task did not drain")


def test_builder_preserves_primary_identity_for_every_closed_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _Primary({})
    witness = _witness()
    _admit_witness(monkeypatch, witness)

    assert (
        build_supervisor_effect_intent_runtime(
            _settings("off"),
            primary,
            _Scheduler(),
            _Storage(),
            witness,  # type: ignore[arg-type]
        )
        is primary
    )
    assert (
        build_supervisor_effect_intent_runtime(
            _settings(evidence_sha256="9" * 64),
            primary,
            _Scheduler(),
            _Storage(),
            witness,  # type: ignore[arg-type]
        )
        is primary
    )
    assert (
        build_supervisor_effect_intent_runtime(_settings(), primary, _Scheduler(), _Storage(), None)
        is primary
    )
    assert (
        build_supervisor_effect_intent_runtime(
            _settings(),
            primary,
            object(),
            _Storage(),
            witness,  # type: ignore[arg-type]
        )
        is primary
    )


@pytest.mark.asyncio
async def test_post_commit_shadow_returns_exact_primary_and_persists_body_free_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as runtime

    result = {
        "conversation_id": "conversation-1",
        "message_id": "message-1",
        "message": "private primary answer",
    }
    primary = _Primary(result)
    scheduler = _Scheduler()
    storage = _Storage()
    witness = _witness()
    _admit_witness(monkeypatch, witness)
    calls = 0
    selections: list[EffectIntentSelectionV2] = []

    async def select(_scheduler: object, **kwargs: Any) -> EffectIntentSelectionV2:
        nonlocal calls
        calls += 1
        projection = kwargs["projection"]
        selection = EffectIntentSelectionV2(
            capability=EffectIntentCapabilitySelection.OBSIDIAN_NOTE_MUTATION,
            action=EffectIntentActionSelection.CREATE,
            manifest_digest=SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
            projection_digest=projection.projection_digest,
        )
        selections.append(selection)
        return selection

    monkeypatch.setattr(runtime, "select_supervisor_effect_intent", select)
    wrapper = build_supervisor_effect_intent_runtime(
        _settings(),
        primary,
        scheduler,
        storage,
        witness,  # type: ignore[arg-type]
    )
    assert isinstance(wrapper, SupervisorEffectIntentShadowRuntime)

    returned = await wrapper.chat("user-1", "Создай заметку о встрече", marker=object())
    assert returned is result
    assert primary.calls == 1
    await _drain(wrapper)

    assert calls == 1
    assert len(storage.events) == 1
    event_type, payload = storage.events[0]
    assert event_type == "semantic_supervisor.effect_shadow"
    assert payload["agreement"] == "matched_actual_effect"
    assert payload["actual_capability"] == "obsidian_note_mutation"
    assert payload["actual_action"] == "create"
    assert payload["execution_authorized"] is False
    assert payload["publication_authorized"] is False
    encoded = json.dumps(payload, ensure_ascii=False)
    raw_projection_digest = prepare_effect_intent_projection_v2("Создай заметку о встрече").projection_digest
    assert payload["projection_identity_sha256"] != raw_projection_digest
    assert raw_projection_digest not in encoded
    assert selections[0].canonical_sha256() not in encoded
    assert "projection_digest" not in payload
    assert "selection_sha256" not in payload
    for private in ("user-1", "message-1", "conversation-1", "Создай", "private primary"):
        assert private not in encoded

    # Adding more accepted receipts than the former LRU bound neither
    # reallocates the fixed Bloom filter nor forgets the first receipt.
    bloom_object = runtime._PROCESS_DEDUPE_BLOOM  # noqa: SLF001
    bloom_bytes = len(bloom_object)
    for index in range(5_000):
        receipt = AcceptedEffectOutcomeReceipt.from_outcome(
            replace(_outcome(), effect_id_sha256=f"{index:064x}")
        )
        assert wrapper._remember_accepted_receipt_once(receipt)  # noqa: SLF001
    first_receipt = AcceptedEffectOutcomeReceipt.from_outcome(_outcome())
    assert wrapper._remember_accepted_receipt_once(first_receipt) is False  # noqa: SLF001
    assert runtime._PROCESS_DEDUPE_BLOOM is bloom_object  # noqa: SLF001
    assert len(runtime._PROCESS_DEDUPE_BLOOM) == bloom_bytes  # noqa: SLF001
    assert await wrapper.chat("user-1", "Создай заметку о встрече") is result
    await _drain(wrapper)
    assert calls == 1
    assert primary.calls == 2
    runtime_status = wrapper.semantic_supervisor_effect_status()
    assert runtime_status["dedupe_retention"] == "process_lifetime"
    assert runtime_status["dedupe_algorithm"] == "fixed_hmac_sha256_bloom_v1"
    assert runtime_status["dedupe_identity"] == "accepted_effect_id_and_outcome_sha256_v1"
    assert runtime_status["dedupe_identity_count"] == 2
    assert runtime_status["dedupe_memory_bounded"] is True
    assert runtime_status["dedupe_memory_bytes"] == 512 * 1_024
    assert runtime_status["dedupe_bit_capacity"] == 512 * 1_024 * 8
    assert runtime_status["dedupe_hash_count"] == 7
    assert runtime_status["dedupe_bit_probes_per_receipt"] == 14
    assert runtime_status["dedupe_insert_total"] == 5_001
    await wrapper.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_outcome",
    [
        pytest.param(None, id="same-receipt"),
        pytest.param(
            replace(_outcome(), evidence_sha256="2" * 64),
            id="same-effect-new-outcome",
        ),
    ],
)
async def test_accepted_effect_replay_cannot_dispatch_under_different_message_scope(
    monkeypatch: pytest.MonkeyPatch,
    second_outcome: EffectOutcomeV1 | None,
) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as runtime

    class _ReplayStorage(_Storage):
        def __init__(self) -> None:
            super().__init__()
            second_metadata_json = self.row["metadata_json"]
            if second_outcome is not None:
                second_metadata: dict[str, Any] = {}
                attach_accepted_effect_outcome_receipt(second_metadata, second_outcome)
                second_metadata_json = json.dumps(second_metadata)
            second = {
                **self.row,
                "id": "message-2",
                "user_id": "user-2",
                "conversation_id": "conversation-2",
                "metadata_json": second_metadata_json,
            }
            self.rows = {
                ("message-1", "user-1"): dict(self.row),
                ("message-2", "user-2"): second,
            }

        def get_message(self, message_id: str, user_id: str) -> dict[str, Any] | None:
            row = self.rows.get((message_id, user_id))
            return dict(row) if row is not None else None

    first_result = {
        "conversation_id": "conversation-1",
        "message_id": "message-1",
        "message": "first private primary answer",
    }
    second_result = {
        "conversation_id": "conversation-2",
        "message_id": "message-2",
        "message": "second private primary answer",
    }
    primary = _Primary(first_result)
    storage = _ReplayStorage()
    first_receipt = load_accepted_effect_outcome_receipt(
        storage.rows[("message-1", "user-1")]["metadata_json"]
    )
    second_receipt = load_accepted_effect_outcome_receipt(
        storage.rows[("message-2", "user-2")]["metadata_json"]
    )
    assert first_receipt.outcome.effect_id_sha256 == second_receipt.outcome.effect_id_sha256
    assert (first_receipt.outcome_sha256 == second_receipt.outcome_sha256) is (second_outcome is None)
    witness = _witness()
    _admit_witness(monkeypatch, witness)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def select(_scheduler: object, **kwargs: Any) -> EffectIntentSelectionV2:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        projection = kwargs["projection"]
        return EffectIntentSelectionV2(
            capability=EffectIntentCapabilitySelection.OBSIDIAN_NOTE_MUTATION,
            action=EffectIntentActionSelection.CREATE,
            manifest_digest=SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
            projection_digest=projection.projection_digest,
        )

    monkeypatch.setattr(runtime, "select_supervisor_effect_intent", select)
    wrapper = build_supervisor_effect_intent_runtime(
        _settings(),
        primary,
        _Scheduler(),
        storage,
        witness,  # type: ignore[arg-type]
    )
    assert isinstance(wrapper, SupervisorEffectIntentShadowRuntime)

    assert await wrapper.chat("user-1", "Создай первую заметку") is first_result
    await asyncio.wait_for(started.wait(), timeout=1)
    primary.result = second_result
    assert await wrapper.chat("user-2", "Создай вторую заметку") is second_result
    for _ in range(10):
        skip_reasons = wrapper.semantic_supervisor_effect_status()["skip_reasons"]
        if isinstance(skip_reasons, dict) and skip_reasons.get("already_observed"):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("cross-scope receipt replay was not rejected")

    assert calls == 1
    assert primary.calls == 2
    assert wrapper.semantic_supervisor_effect_status()["dedupe_insert_total"] == 1
    release.set()
    await _drain(wrapper)
    assert len(storage.events) == 1
    assert wrapper.semantic_supervisor_effect_status()["dispatch_total"] == 1
    await wrapper.close()


@pytest.mark.asyncio
async def test_missing_receipt_or_rejected_projection_never_calls_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as runtime

    result = {"conversation_id": "conversation-1", "message_id": "message-1"}
    primary = _Primary(result)
    storage = _Storage(receipt=False)
    witness = _witness()
    _admit_witness(monkeypatch, witness)

    async def forbidden(*_args: Any, **_kwargs: Any) -> EffectIntentSelectionV2:
        raise AssertionError("model must remain unavailable")

    monkeypatch.setattr(runtime, "select_supervisor_effect_intent", forbidden)
    wrapper = build_supervisor_effect_intent_runtime(
        _settings(),
        primary,
        _Scheduler(),
        storage,
        witness,  # type: ignore[arg-type]
    )
    assert isinstance(wrapper, SupervisorEffectIntentShadowRuntime)
    assert await wrapper.chat("user-1", "Создай /private/path") is result
    await _drain(wrapper)
    assert storage.events == []
    assert primary.calls == 1
    status = wrapper.semantic_supervisor_effect_status()
    assert status["dispatch_total"] == 0
    assert status["skip_reasons"] == {"input_unavailable": 1, "projection_rejected": 1}
    await wrapper.close()


@pytest.mark.asyncio
async def test_close_cancels_only_shadow_and_health_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as runtime

    result = {"conversation_id": "conversation-1", "message_id": "message-1"}
    primary = _Primary(result)
    witness = _witness()
    _admit_witness(monkeypatch, witness)
    started = asyncio.Event()

    async def blocked(*_args: Any, **_kwargs: Any) -> EffectIntentSelectionV2:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(runtime, "select_supervisor_effect_intent", blocked)
    wrapper = build_supervisor_effect_intent_runtime(
        _settings(),
        primary,
        _Scheduler(),
        _Storage(),
        witness,  # type: ignore[arg-type]
    )
    assert isinstance(wrapper, SupervisorEffectIntentShadowRuntime)
    assert await wrapper.chat("user-1", "Создай заметку") is result
    await started.wait()
    await wrapper.close()
    assert primary.calls == 1
    assert wrapper.semantic_supervisor_effect_status()["effective_mode"] == "off"
    await wrapper.close()


def test_public_effect_health_is_exact_and_never_claims_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _Primary({})
    witness = _witness()
    _admit_witness(monkeypatch, witness)
    wrapper = build_supervisor_effect_intent_runtime(
        _settings(),
        primary,
        _Scheduler(),
        _Storage(),
        witness,  # type: ignore[arg-type]
    )
    status = supervisor_effect_shadow_health_status(wrapper, None, _settings())

    assert status == {
        "schema": SUPERVISOR_EFFECT_SHADOW_HEALTH_SCHEMA,
        "installed": True,
        "requested_mode": "shadow",
        "effective_mode": "shadow",
        "maturity_accepted": True,
        "policy_id": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
        "policy_sha256": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
        "evidence_sha256": "1" * 64,
        "maturity_facts_sha256": "2" * 64,
        "source_revision_sha256": "3" * 64,
        "registry_binding_sha256": "4" * 64,
        "effect_registry_binding_sha256": "5" * 64,
        "execution_authorized": False,
        "publication_authorized": False,
    }
    assert set(status) == {
        "schema",
        "installed",
        "requested_mode",
        "effective_mode",
        "maturity_accepted",
        "policy_id",
        "policy_sha256",
        "evidence_sha256",
        "maturity_facts_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "effect_registry_binding_sha256",
        "execution_authorized",
        "publication_authorized",
    }

    closed = supervisor_effect_shadow_health_status(None, None, _settings("off"))
    assert closed["effective_mode"] == "off"
    assert closed["maturity_accepted"] is False
    assert closed["evidence_sha256"] == ""
    assert closed["maturity_facts_sha256"] == ""
    assert closed["source_revision_sha256"] == ""
    assert closed["registry_binding_sha256"] == ""
    assert closed["effect_registry_binding_sha256"] == ""


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema", "friday.semantic-supervisor-effect-shadow-runtime.v0"),
        ("policy_id", "forged-policy"),
        ("policy_sha256", "9" * 64),
        ("workload", "plan_candidate"),
        ("runtime_owner", "secondary"),
        ("publication_owner", "secondary"),
        ("primary_result_unchanged", False),
        ("tools_allowed", True),
        ("effects_allowed", True),
        ("execution_authorized", True),
        ("publication_authorized", True),
        ("body_free", False),
        ("effect_registry_binding_sha256", ""),
    ],
)
def test_public_effect_health_rejects_mutated_runtime_attestation(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    primary = _Primary({})
    witness = _witness()
    _admit_witness(monkeypatch, witness)
    wrapper = build_supervisor_effect_intent_runtime(
        _settings(),
        primary,
        _Scheduler(),
        _Storage(),
        witness,  # type: ignore[arg-type]
    )
    assert isinstance(wrapper, SupervisorEffectIntentShadowRuntime)
    raw = wrapper.semantic_supervisor_effect_status()
    raw[field] = replacement
    forged_runtime = SimpleNamespace(semantic_supervisor_effect_status=lambda: raw)

    status = supervisor_effect_shadow_health_status(forged_runtime, None, _settings())

    assert status["installed"] is False
    assert status["effective_mode"] == "off"
    assert status["maturity_accepted"] is False
    assert status["evidence_sha256"] == ""
    assert status["maturity_facts_sha256"] == ""
    assert status["source_revision_sha256"] == ""
    assert status["registry_binding_sha256"] == ""
    assert status["effect_registry_binding_sha256"] == ""
    assert status["execution_authorized"] is False
    assert status["publication_authorized"] is False


def test_public_effect_health_requires_exact_runtime_keys_and_live_runtime_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _Primary({})
    witness = _witness()
    _admit_witness(monkeypatch, witness)
    wrapper = build_supervisor_effect_intent_runtime(
        _settings(),
        primary,
        _Scheduler(),
        _Storage(),
        witness,  # type: ignore[arg-type]
    )
    assert isinstance(wrapper, SupervisorEffectIntentShadowRuntime)
    valid = wrapper.semantic_supervisor_effect_status()
    candidates = [{key: value for key, value in valid.items() if key != missing} for missing in valid]
    candidates.append({**valid, "unknown": True})

    for candidate in candidates:
        runtime = SimpleNamespace(semantic_supervisor_effect_status=lambda value=candidate: value)
        status = supervisor_effect_shadow_health_status(runtime, None, _settings())
        assert status["installed"] is False
        assert status["effective_mode"] == "off"
        assert status["maturity_accepted"] is False

    activation_only = supervisor_effect_shadow_health_status(None, valid, _settings())
    assert activation_only["installed"] is False
    assert activation_only["effective_mode"] == "off"
    assert activation_only["maturity_accepted"] is False

    def unavailable() -> dict[str, object]:
        raise RuntimeError("private runtime failure")

    failed_runtime = SimpleNamespace(semantic_supervisor_effect_status=unavailable)
    failed = supervisor_effect_shadow_health_status(failed_runtime, valid, _settings())
    assert failed["installed"] is False
    assert failed["effective_mode"] == "off"
    assert failed["maturity_accepted"] is False

    class BrokenRuntime:
        @property
        def semantic_supervisor_effect_status(self) -> object:
            raise RuntimeError("private runtime attribute failure")

    broken = supervisor_effect_shadow_health_status(BrokenRuntime(), valid, _settings())
    assert broken["installed"] is False
    assert broken["effective_mode"] == "off"
    assert broken["maturity_accepted"] is False
