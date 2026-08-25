from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from friday.orchestration.effect_outcome import (
    ACCEPTED_EFFECT_OUTCOME_METADATA_KEY,
    EFFECT_OUTCOME_RECEIPT_SCHEMA,
    EFFECT_OUTCOME_SCHEMA,
    AcceptedEffectOutcomeReceipt,
    EffectAction,
    EffectCapability,
    EffectCompensationState,
    EffectObservationState,
    EffectObservationsV1,
    EffectOutcomeError,
    EffectOutcomeV1,
    EffectPublishability,
    EffectReconciliationState,
    EffectStatus,
    attach_accepted_effect_outcome_receipt,
    load_accepted_effect_outcome_receipt,
)

_EFFECT = "a" * 64
_WORK_ITEM = "b" * 64
_REQUEST = "c" * 64
_AUTHORIZATION = "d" * 64
_IDEMPOTENCY = "e" * 64
_SIDE_EFFECT_RECEIPT = "f" * 64
_COMPENSATION_RECEIPT = "1" * 64
_EVIDENCE = "2" * 64


def _observations(state: EffectObservationState) -> EffectObservationsV1:
    return EffectObservationsV1(
        server_sync=state,
        reingest=state,
        physical_device=state,
    )


def _outcome(
    status: EffectStatus,
    *,
    action: EffectAction = EffectAction.CREATE,
    authority_rechecked: bool = True,
) -> EffectOutcomeV1:
    accepted = status in {EffectStatus.SUCCEEDED, EffectStatus.PARTIAL, EffectStatus.COMPENSATED}
    if status is EffectStatus.SUCCEEDED:
        reconciliation = EffectReconciliationState.NOT_REQUIRED
        compensation = EffectCompensationState.NOT_REQUIRED
        compensation_receipt = None
        publishability = EffectPublishability.ACCEPTED_FACTS
    elif status is EffectStatus.PARTIAL:
        reconciliation = EffectReconciliationState.BLOCKED
        compensation = EffectCompensationState.NOT_REQUIRED
        compensation_receipt = None
        publishability = EffectPublishability.ACCEPTED_FACTS
    elif status is EffectStatus.UNCERTAIN:
        reconciliation = EffectReconciliationState.REQUIRED
        compensation = EffectCompensationState.NOT_REQUIRED
        compensation_receipt = None
        publishability = EffectPublishability.UNCERTAINTY_ONLY
    elif status is EffectStatus.COMPENSATED:
        reconciliation = EffectReconciliationState.SETTLED
        compensation = EffectCompensationState.SUCCEEDED
        compensation_receipt = _COMPENSATION_RECEIPT
        publishability = EffectPublishability.ACCEPTED_FACTS
    else:
        reconciliation = EffectReconciliationState.NOT_REQUIRED
        compensation = EffectCompensationState.NOT_REQUIRED
        compensation_receipt = None
        publishability = EffectPublishability.NEGATIVE_ONLY
    if not authority_rechecked:
        publishability = EffectPublishability.SUPPRESSED
    return EffectOutcomeV1(
        effect_id_sha256=_EFFECT,
        work_item_sha256=_WORK_ITEM,
        capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
        action=action,
        request_sha256=_REQUEST,
        authorization_basis_sha256=_AUTHORIZATION,
        idempotency_key_sha256=_IDEMPOTENCY,
        status=status,
        reconciliation=reconciliation,
        compensation=compensation,
        side_effect_receipt_sha256=_SIDE_EFFECT_RECEIPT if accepted else None,
        compensation_receipt_sha256=compensation_receipt,
        evidence_sha256=_EVIDENCE,
        observations=_observations(
            EffectObservationState.PENDING
            if accepted or status is EffectStatus.UNCERTAIN
            else EffectObservationState.UNAVAILABLE
        ),
        publishability=publishability,
        authority_rechecked=authority_rechecked,
    )


def test_effect_outcome_is_immutable_canonical_closed_and_round_trips() -> None:
    outcome = _outcome(EffectStatus.SUCCEEDED)

    assert EffectOutcomeV1.parse(outcome.to_json()) == outcome
    assert EffectOutcomeV1.parse(outcome.to_payload()) == outcome
    assert outcome.to_payload()["schema"] == EFFECT_OUTCOME_SCHEMA
    assert len(outcome.canonical_sha256()) == 64
    assert (
        outcome.canonical_sha256()
        == EffectOutcomeV1.parse(
            json.dumps(outcome.to_payload(), indent=2, sort_keys=False)
        ).canonical_sha256()
    )
    with pytest.raises(FrozenInstanceError):
        outcome.status = EffectStatus.UNCERTAIN  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        outcome.observations.server_sync = EffectObservationState.OBSERVED  # type: ignore[misc]


def test_contract_is_explicitly_narrow_and_all_declared_statuses_round_trip() -> None:
    assert set(EffectCapability) == {EffectCapability.OBSIDIAN_NOTE_MUTATION}
    assert set(EffectAction) == {EffectAction.CREATE, EffectAction.APPEND}
    assert set(EffectStatus) == {
        EffectStatus.SUCCEEDED,
        EffectStatus.PARTIAL,
        EffectStatus.REFUSED,
        EffectStatus.UNAVAILABLE,
        EffectStatus.UNCERTAIN,
        EffectStatus.COMPENSATED,
    }

    for status in EffectStatus:
        for action in EffectAction:
            outcome = _outcome(status, action=action)
            assert EffectOutcomeV1.parse(outcome.to_json()) == outcome


@pytest.mark.parametrize(
    "field",
    (
        "effect_id_sha256",
        "work_item_sha256",
        "request_sha256",
        "authorization_basis_sha256",
        "idempotency_key_sha256",
        "side_effect_receipt_sha256",
        "evidence_sha256",
    ),
)
@pytest.mark.parametrize("invalid", ("", "A" * 64, "0" * 63, "g" * 64, True))
def test_every_identity_field_requires_an_exact_lowercase_sha256(field: str, invalid: object) -> None:
    payload = _outcome(EffectStatus.SUCCEEDED).to_payload()
    payload[field] = invalid
    with pytest.raises(EffectOutcomeError, match="lowercase SHA-256"):
        EffectOutcomeV1.parse(payload)


def test_optional_digest_fields_accept_only_none_or_digest() -> None:
    outcome = replace(
        _outcome(EffectStatus.SUCCEEDED),
        work_item_sha256=None,
        evidence_sha256=None,
    )
    assert EffectOutcomeV1.parse(outcome.to_payload()) == outcome

    compensated = _outcome(EffectStatus.COMPENSATED)
    with pytest.raises(EffectOutcomeError, match="lowercase SHA-256"):
        replace(compensated, compensation_receipt_sha256="raw-receipt")


@pytest.mark.parametrize(
    "status",
    (EffectStatus.SUCCEEDED, EffectStatus.PARTIAL, EffectStatus.COMPENSATED),
)
def test_accepted_status_requires_side_effect_receipt(status: EffectStatus) -> None:
    with pytest.raises(EffectOutcomeError, match="present together"):
        replace(_outcome(status), side_effect_receipt_sha256=None)


@pytest.mark.parametrize(
    "status",
    (EffectStatus.REFUSED, EffectStatus.UNAVAILABLE, EffectStatus.UNCERTAIN),
)
def test_unaccepted_status_forbids_side_effect_receipt(status: EffectStatus) -> None:
    with pytest.raises(EffectOutcomeError, match="present together"):
        replace(_outcome(status), side_effect_receipt_sha256=_SIDE_EFFECT_RECEIPT)


def test_compensation_status_and_receipt_are_bound_bidirectionally() -> None:
    compensated = _outcome(EffectStatus.COMPENSATED)
    with pytest.raises(EffectOutcomeError, match="requires its receipt"):
        replace(compensated, compensation_receipt_sha256=None)
    with pytest.raises(EffectOutcomeError, match="requires successful compensation"):
        replace(
            _outcome(EffectStatus.PARTIAL),
            compensation_receipt_sha256=_COMPENSATION_RECEIPT,
        )
    with pytest.raises(EffectOutcomeError, match="settled successful compensation"):
        replace(compensated, reconciliation=EffectReconciliationState.BLOCKED)
    with pytest.raises(EffectOutcomeError, match="settled successful compensation"):
        replace(
            compensated,
            compensation=EffectCompensationState.NOT_REQUIRED,
            compensation_receipt_sha256=None,
        )


@pytest.mark.parametrize(
    ("status", "reconciliation", "compensation", "message"),
    (
        (
            EffectStatus.SUCCEEDED,
            EffectReconciliationState.REQUIRED,
            EffectCompensationState.NOT_REQUIRED,
            "succeeded effect",
        ),
        (
            EffectStatus.PARTIAL,
            EffectReconciliationState.REQUIRED,
            EffectCompensationState.NOT_REQUIRED,
            "partial effect",
        ),
        (
            EffectStatus.REFUSED,
            EffectReconciliationState.BLOCKED,
            EffectCompensationState.NOT_REQUIRED,
            "known negative",
        ),
        (
            EffectStatus.UNAVAILABLE,
            EffectReconciliationState.NOT_REQUIRED,
            EffectCompensationState.REQUIRED,
            "known negative",
        ),
        (
            EffectStatus.UNCERTAIN,
            EffectReconciliationState.NOT_REQUIRED,
            EffectCompensationState.NOT_REQUIRED,
            "must be reconciled",
        ),
    ),
)
def test_status_reconciliation_and_compensation_matrix_is_closed(
    status: EffectStatus,
    reconciliation: EffectReconciliationState,
    compensation: EffectCompensationState,
    message: str,
) -> None:
    with pytest.raises(EffectOutcomeError, match=message):
        replace(
            _outcome(status),
            reconciliation=reconciliation,
            compensation=compensation,
        )


@pytest.mark.parametrize(
    "status",
    (EffectStatus.REFUSED, EffectStatus.UNAVAILABLE, EffectStatus.UNCERTAIN),
)
@pytest.mark.parametrize(
    "observed",
    (EffectObservationState.OBSERVED, EffectObservationState.CONFLICT),
)
def test_unaccepted_status_cannot_claim_downstream_observation(
    status: EffectStatus,
    observed: EffectObservationState,
) -> None:
    with pytest.raises(EffectOutcomeError, match="unaccepted effect"):
        replace(
            _outcome(status),
            observations=EffectObservationsV1(
                server_sync=EffectObservationState.PENDING,
                reingest=observed,
                physical_device=EffectObservationState.UNAVAILABLE,
            ),
        )


def test_accepted_downstream_observations_remain_independent() -> None:
    observations = EffectObservationsV1(
        server_sync=EffectObservationState.UNAVAILABLE,
        reingest=EffectObservationState.OBSERVED,
        physical_device=EffectObservationState.CONFLICT,
    )
    outcome = replace(_outcome(EffectStatus.SUCCEEDED), observations=observations)

    assert outcome.observations == observations
    assert EffectOutcomeV1.parse(outcome.to_payload()) == outcome


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (EffectStatus.SUCCEEDED, EffectPublishability.ACCEPTED_FACTS),
        (EffectStatus.PARTIAL, EffectPublishability.ACCEPTED_FACTS),
        (EffectStatus.COMPENSATED, EffectPublishability.ACCEPTED_FACTS),
        (EffectStatus.UNCERTAIN, EffectPublishability.UNCERTAINTY_ONLY),
        (EffectStatus.REFUSED, EffectPublishability.NEGATIVE_ONLY),
        (EffectStatus.UNAVAILABLE, EffectPublishability.NEGATIVE_ONLY),
    ),
)
def test_publication_class_is_code_owned_by_status(
    status: EffectStatus,
    expected: EffectPublishability,
) -> None:
    outcome = _outcome(status)
    assert outcome.publishability is expected
    for contradictory in set(EffectPublishability) - {expected, EffectPublishability.SUPPRESSED}:
        with pytest.raises(EffectOutcomeError, match="contradicts"):
            replace(outcome, publishability=contradictory)


@pytest.mark.parametrize("status", tuple(EffectStatus))
def test_missing_late_authority_recheck_suppresses_all_effect_facts(status: EffectStatus) -> None:
    outcome = _outcome(status, authority_rechecked=False)
    assert outcome.publishability is EffectPublishability.SUPPRESSED
    with pytest.raises(EffectOutcomeError, match="authority recheck"):
        replace(
            outcome,
            publishability=(
                EffectPublishability.UNCERTAINTY_ONLY
                if status is EffectStatus.UNCERTAIN
                else EffectPublishability.NEGATIVE_ONLY
            ),
        )


def test_parser_rejects_unknown_duplicate_nested_and_derived_widening() -> None:
    outcome = _outcome(EffectStatus.SUCCEEDED)

    widened = outcome.to_payload()
    widened["note_body"] = "secret"
    with pytest.raises(EffectOutcomeError, match="closed contract"):
        EffectOutcomeV1.parse(widened)

    nested = outcome.to_payload()
    assert isinstance(nested["observations"], dict)
    nested["observations"]["path"] = "/private/vault/note.md"
    with pytest.raises(EffectOutcomeError, match="observation keys"):
        EffectOutcomeV1.parse(nested)

    duplicate_top = outcome.to_json().replace(
        '"schema":',
        '"schema":"duplicate","schema":',
        1,
    )
    with pytest.raises(EffectOutcomeError, match="duplicate"):
        EffectOutcomeV1.parse(duplicate_top)

    duplicate_nested = outcome.to_json().replace(
        '"physical_device":',
        '"physical_device":"pending","physical_device":',
        1,
    )
    with pytest.raises(EffectOutcomeError, match="duplicate"):
        EffectOutcomeV1.parse(duplicate_nested)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema", "friday.effect-outcome.v2"),
        ("capability", "generic_connector"),
        ("action", "delete"),
        ("status", "failed"),
        ("reconciliation", "retry_now"),
        ("compensation", "rolled_back"),
        ("publishability", "full_prose"),
    ),
)
def test_parser_rejects_unknown_schema_and_enum_values(field: str, value: str) -> None:
    payload = _outcome(EffectStatus.SUCCEEDED).to_payload()
    payload[field] = value
    with pytest.raises(EffectOutcomeError):
        EffectOutcomeV1.parse(payload)


def test_parser_rejects_unknown_observation_enum_and_nonobjects() -> None:
    payload = _outcome(EffectStatus.SUCCEEDED).to_payload()
    assert isinstance(payload["observations"], dict)
    payload["observations"]["server_sync"] = "probably"
    with pytest.raises(EffectOutcomeError, match="unknown enum"):
        EffectOutcomeV1.parse(payload)

    invalid_values: tuple[object, ...] = ([], "text", 1, None)
    for invalid in invalid_values:
        with pytest.raises(EffectOutcomeError, match="object"):
            EffectOutcomeV1.parse(invalid)  # type: ignore[arg-type]

    payload = _outcome(EffectStatus.SUCCEEDED).to_payload()
    payload["observations"] = []
    with pytest.raises(EffectOutcomeError, match="observations must be an object"):
        EffectOutcomeV1.parse(payload)


def test_constructor_rejects_raw_enum_strings_and_non_boolean_authority() -> None:
    outcome = _outcome(EffectStatus.SUCCEEDED)
    with pytest.raises(EffectOutcomeError, match="EffectStatus"):
        replace(outcome, status="succeeded")  # type: ignore[arg-type]
    with pytest.raises(EffectOutcomeError, match="EffectObservationsV1"):
        replace(outcome, observations=outcome.observations.to_payload())  # type: ignore[arg-type]
    with pytest.raises(EffectOutcomeError, match="must be a boolean"):
        replace(outcome, authority_rechecked=1)  # type: ignore[arg-type]


def test_parser_enforces_utf8_and_serialized_budget() -> None:
    with pytest.raises(EffectOutcomeError, match="valid UTF-8"):
        EffectOutcomeV1.parse("\ud800")

    oversized = _outcome(EffectStatus.SUCCEEDED).to_json() + (" " * 4_096)
    with pytest.raises(EffectOutcomeError, match="too large"):
        EffectOutcomeV1.parse(oversized)


def test_hash_binds_every_effect_identity_and_structural_observation() -> None:
    outcome = _outcome(EffectStatus.SUCCEEDED)
    mutations = (
        replace(outcome, effect_id_sha256="3" * 64),
        replace(outcome, work_item_sha256=None),
        replace(outcome, action=EffectAction.APPEND),
        replace(outcome, request_sha256="4" * 64),
        replace(outcome, authorization_basis_sha256="5" * 64),
        replace(outcome, idempotency_key_sha256="6" * 64),
        replace(outcome, side_effect_receipt_sha256="7" * 64),
        replace(outcome, evidence_sha256=None),
        replace(
            outcome,
            observations=replace(
                outcome.observations,
                server_sync=EffectObservationState.OBSERVED,
            ),
        ),
        replace(outcome, publishability=EffectPublishability.SUPPRESSED),
    )
    assert all(item.canonical_sha256() != outcome.canonical_sha256() for item in mutations)
    assert len({item.canonical_sha256() for item in mutations}) == len(mutations)


def test_envelope_never_retains_private_effect_material() -> None:
    private_values = (
        "Projects/Friday Test.md",
        "Тест интеграции Friday",
        "note-body-sentinel",
        "append-text-sentinel",
        "raw-owner-291",
        "telegram-chat-991",
        "provider-stacktrace-sentinel",
    )
    serialized = _outcome(EffectStatus.SUCCEEDED).to_json()

    assert all(value not in serialized for value in private_values)
    assert all(
        forbidden not in serialized.casefold()
        for forbidden in ("note_body", "append_text", "path", "owner", "chat_id", "error", "prose")
    )
    for private_key, private_value in (
        ("path", private_values[0]),
        ("body", private_values[2]),
        ("owner_id", private_values[4]),
        ("provider_error", private_values[6]),
    ):
        payload = _outcome(EffectStatus.SUCCEEDED).to_payload()
        payload[private_key] = private_value
        with pytest.raises(EffectOutcomeError, match="closed contract"):
            EffectOutcomeV1.parse(payload)


def test_accepted_effect_receipt_is_immutable_canonical_closed_and_round_trips() -> None:
    outcome = _outcome(EffectStatus.SUCCEEDED)
    receipt = AcceptedEffectOutcomeReceipt.from_outcome(outcome)

    assert AcceptedEffectOutcomeReceipt.parse(receipt.to_json()) == receipt
    assert AcceptedEffectOutcomeReceipt.parse(receipt.to_payload()) == receipt
    assert receipt.to_payload()["schema"] == EFFECT_OUTCOME_RECEIPT_SCHEMA
    assert receipt.outcome_sha256 == outcome.canonical_sha256()
    assert len(receipt.canonical_sha256()) == 64
    with pytest.raises(FrozenInstanceError):
        receipt.outcome_sha256 = "3" * 64  # type: ignore[misc]


def test_accepted_effect_receipt_attaches_and_loads_from_mapping_and_json() -> None:
    outcome = _outcome(EffectStatus.UNCERTAIN)
    metadata: dict[str, object] = {"answer_mode": "obsidian_mutation"}

    receipt = attach_accepted_effect_outcome_receipt(metadata, outcome)

    assert metadata[ACCEPTED_EFFECT_OUTCOME_METADATA_KEY] == receipt.to_payload()
    assert load_accepted_effect_outcome_receipt(metadata, expected_outcome=outcome) == receipt
    assert (
        load_accepted_effect_outcome_receipt(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            expected_outcome=outcome,
        )
        == receipt
    )


def test_accepted_effect_receipt_rejects_tamper_widening_and_wrong_expected_outcome() -> None:
    outcome = _outcome(EffectStatus.SUCCEEDED)
    receipt = AcceptedEffectOutcomeReceipt.from_outcome(outcome)

    wrong_digest = receipt.to_payload()
    wrong_digest["outcome_sha256"] = "3" * 64
    with pytest.raises(EffectOutcomeError, match="digest"):
        AcceptedEffectOutcomeReceipt.parse(wrong_digest)

    widened = receipt.to_payload()
    widened["private_body"] = "secret"
    with pytest.raises(EffectOutcomeError, match="closed contract"):
        AcceptedEffectOutcomeReceipt.parse(widened)

    metadata = {ACCEPTED_EFFECT_OUTCOME_METADATA_KEY: receipt.to_payload()}
    expected = replace(outcome, request_sha256="4" * 64)
    with pytest.raises(EffectOutcomeError, match="expected outcome"):
        load_accepted_effect_outcome_receipt(metadata, expected_outcome=expected)


def test_accepted_effect_receipt_parser_rejects_schema_shape_and_duplicate_keys() -> None:
    receipt = AcceptedEffectOutcomeReceipt.from_outcome(_outcome(EffectStatus.SUCCEEDED))

    wrong_schema = receipt.to_payload()
    wrong_schema["schema"] = "friday.accepted-effect-outcome-receipt.v2"
    with pytest.raises(EffectOutcomeError, match="schema"):
        AcceptedEffectOutcomeReceipt.parse(wrong_schema)

    no_outcome = receipt.to_payload()
    no_outcome["outcome"] = "not-an-object"
    with pytest.raises(EffectOutcomeError, match="no outcome object"):
        AcceptedEffectOutcomeReceipt.parse(no_outcome)

    duplicate = receipt.to_json().replace(
        '"outcome_sha256":',
        '"outcome_sha256":"duplicate","outcome_sha256":',
        1,
    )
    with pytest.raises(EffectOutcomeError, match="duplicate"):
        AcceptedEffectOutcomeReceipt.parse(duplicate)

    invalid_receipts: tuple[object, ...] = ([], 1, None)
    for invalid in invalid_receipts:
        with pytest.raises(EffectOutcomeError, match="must be an object"):
            AcceptedEffectOutcomeReceipt.parse(invalid)  # type: ignore[arg-type]


def test_accepted_effect_receipt_budget_single_attachment_and_atomic_failure() -> None:
    outcome = _outcome(EffectStatus.SUCCEEDED)
    metadata: dict[str, object] = {"answer_mode": "obsidian_mutation"}
    original = dict(metadata)

    with pytest.raises(EffectOutcomeError, match="bounded carrier"):
        attach_accepted_effect_outcome_receipt(metadata, outcome, max_serialized_bytes=1)
    assert metadata == original

    attach_accepted_effect_outcome_receipt(metadata, outcome)
    with pytest.raises(EffectOutcomeError, match="already attached"):
        attach_accepted_effect_outcome_receipt(metadata, outcome)

    for invalid_budget in (0, -1, True, 65_537):
        with pytest.raises(EffectOutcomeError, match="closed limit"):
            attach_accepted_effect_outcome_receipt(
                {},
                outcome,
                max_serialized_bytes=invalid_budget,
            )


def test_accepted_effect_receipt_loader_rejects_bad_carriers() -> None:
    receipt = AcceptedEffectOutcomeReceipt.from_outcome(_outcome(EffectStatus.SUCCEEDED))
    oversized = {
        ACCEPTED_EFFECT_OUTCOME_METADATA_KEY: receipt.to_payload(),
        "padding": "X" * 65_536,
    }
    with pytest.raises(EffectOutcomeError, match="bounded carrier"):
        load_accepted_effect_outcome_receipt(oversized)

    nonserializable = {
        ACCEPTED_EFFECT_OUTCOME_METADATA_KEY: receipt.to_payload(),
        "invalid": object(),
    }
    with pytest.raises(EffectOutcomeError, match="cannot be serialized"):
        load_accepted_effect_outcome_receipt(nonserializable)

    with pytest.raises(EffectOutcomeError, match="has no receipt"):
        load_accepted_effect_outcome_receipt({"answer_mode": "obsidian_mutation"})
    with pytest.raises(EffectOutcomeError, match="must be an object"):
        load_accepted_effect_outcome_receipt([])
    with pytest.raises(EffectOutcomeError, match="valid UTF-8"):
        load_accepted_effect_outcome_receipt("\ud800")


@pytest.mark.parametrize("status", tuple(EffectStatus))
def test_private_effect_receipt_never_reintroduces_raw_effect_material(status: EffectStatus) -> None:
    receipt = AcceptedEffectOutcomeReceipt.from_outcome(_outcome(status))
    serialized = receipt.to_json()

    for forbidden in (
        "Projects/Friday Test.md",
        "Тест интеграции Friday",
        "note-body-sentinel",
        "append-text-sentinel",
        "raw-owner-291",
        "telegram-chat-991",
        "provider-stacktrace-sentinel",
    ):
        assert forbidden not in serialized
    assert all(
        forbidden not in serialized.casefold()
        for forbidden in ("note_body", "append_text", "path", "owner", "chat_id", "error", "prose")
    )
