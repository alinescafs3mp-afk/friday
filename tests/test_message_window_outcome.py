from __future__ import annotations

import copy
import hashlib
import json
import pickle
import re
from dataclasses import asdict, is_dataclass, replace

import pytest

import friday.orchestration.capability_outcome as capability_outcome_module
import friday.orchestration.message_window_outcome as contract
from friday.orchestration.capability_outcome import (
    CapabilityOutcome,
    CapabilityOutcomeError,
    CapabilityOutcomeStatus,
    attach_accepted_capability_outcome_receipt,
    load_accepted_capability_outcome_receipt,
)
from friday.orchestration.contracts import RouteClass
from friday.orchestration.message_window_outcome import (
    MESSAGE_WINDOW_DENIED_RESPONSE,
    MESSAGE_WINDOW_EMPTY_RESPONSE,
    MESSAGE_WINDOW_MAX_MESSAGES,
    MESSAGE_WINDOW_UNAVAILABLE_RESPONSE,
    LegacyMessageWindowPlan,
    MessageWindowCompletionDecision,
    MessageWindowEvidence,
    MessageWindowOutcomeError,
    MessageWindowResult,
    MessageWindowSelectionToken,
    MessageWindowStorageAuthority,
    MessageWindowStorageSnapshot,
    accept_message_window_capability_outcome,
    attest_message_window_storage_projection,
    evaluate_message_window_completion,
    message_window_selection_token_is_process_owned,
    message_window_storage_snapshot_is_process_owned,
    prepare_message_window_selection,
    render_message_window_result,
)

REQUEST = "Покажи сообщения в этой переписке за вчера"
TENANT = "owner"
PERSON = "alice"
CONVERSATION = "conv_0000000000000001"
BOUNDARY_ID = "msg_ffffffffffffffff"
TIMEZONE = "Europe/Moscow"
SINCE = "2026-08-21T21:00:00+00:00"
UNTIL = "2026-08-22T21:00:00+00:00"

ROWS = [
    {
        "id": "msg_0000000000000001",
        "conversation_id": CONVERSATION,
        "user_id": PERSON,
        "role": "user",
        "content": "PRIVATE-BODY-ONE",
        "created_at": "2026-08-22T08:22:00+00:00",
    },
    {
        "id": "msg_0000000000000002",
        "conversation_id": CONVERSATION,
        "user_id": PERSON,
        "role": "assistant",
        "content": "PRIVATE-BODY-TWO",
        "created_at": "2026-08-22T08:23:00+00:00",
    },
]

BOUNDARY = {
    "id": BOUNDARY_ID,
    "conversation_id": CONVERSATION,
    "user_id": PERSON,
    "role": "user",
    "content": REQUEST,
    "created_at": "2026-08-22T21:01:00+00:00",
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plan(**changes: object) -> LegacyMessageWindowPlan:
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "person_id": PERSON,
        "conversation_id": CONVERSATION,
        "timezone_name": TIMEZONE,
        "since_utc": SINCE,
        "until_utc": UNTIL,
        "boundary_message_id": BOUNDARY_ID,
        "role": None,
    }
    values.update(changes)
    return LegacyMessageWindowPlan.from_request(REQUEST, **values)  # type: ignore[arg-type]


def _many_rows() -> list[dict[str, object]]:
    return [
        {
            "id": f"msg_{index:016x}",
            "conversation_id": CONVERSATION,
            "user_id": PERSON,
            "role": "user" if index % 2 else "assistant",
            "content": f"body-{index}",
            "created_at": f"2026-08-22T08:{index:02d}:00+00:00",
        }
        for index in range(1, MESSAGE_WINDOW_MAX_MESSAGES + 1)
    ]


def _projection(
    *,
    rows: list[dict[str, object]] | None = None,
    boundary: dict[str, object] | None = None,
    total: int | None = None,
    **changes: object,
) -> dict[str, object]:
    selected_rows = [dict(row) for row in (ROWS if rows is None else rows)]
    selected_total = len(selected_rows) if total is None else total
    value: dict[str, object] = {
        "results": selected_rows,
        "boundary": dict(BOUNDARY if boundary is None else boundary),
        "total": selected_total,
        "shown": len(selected_rows),
        "complete": len(selected_rows) == selected_total,
        "since": SINCE,
        "until": UNTIL,
        "role": None,
        "limit": MESSAGE_WINDOW_MAX_MESSAGES,
    }
    value.update(changes)
    return value


def _snapshot(
    *,
    plan: LegacyMessageWindowPlan | None = None,
    projection: object | None = None,
    **scope: object,
) -> MessageWindowStorageSnapshot:
    selected_plan = plan or _plan()
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "person_id": PERSON,
        "conversation_id": CONVERSATION,
        "timezone_name": TIMEZONE,
        "projection": _projection() if projection is None else projection,
    }
    values.update(scope)
    return attest_message_window_storage_projection(
        contract._trusted_message_window_storage_authority(),
        selected_plan,
        **values,  # type: ignore[arg-type]
    )


def _prepared(
    *,
    plan: LegacyMessageWindowPlan | None = None,
    projection: object | None = None,
) -> tuple[
    LegacyMessageWindowPlan,
    MessageWindowStorageSnapshot,
    MessageWindowSelectionToken,
    MessageWindowEvidence,
]:
    selected_plan = plan or _plan()
    snapshot = _snapshot(plan=selected_plan, projection=projection)
    token = prepare_message_window_selection(selected_plan, snapshot)
    evidence = MessageWindowEvidence.from_selection(selected_plan, token)
    return selected_plan, snapshot, token, evidence


def _case(
    status: CapabilityOutcomeStatus,
) -> tuple[
    LegacyMessageWindowPlan,
    MessageWindowEvidence,
    MessageWindowSelectionToken | None,
    MessageWindowStorageSnapshot | None,
    str,
    MessageWindowResult,
    bool,
]:
    if status is CapabilityOutcomeStatus.PARTIAL:
        plan, snapshot, token, evidence = _prepared(projection=_projection(rows=_many_rows(), total=21))
    elif status is CapabilityOutcomeStatus.EMPTY:
        plan, snapshot, token, evidence = _prepared(projection=_projection(rows=[]))
    elif status is CapabilityOutcomeStatus.COMPLETE:
        plan, snapshot, token, evidence = _prepared()
    else:
        plan = _plan()
        snapshot = None
        token = None
        evidence = MessageWindowEvidence.source_free(plan, status)
    authority_allowed = status is not CapabilityOutcomeStatus.DENIED
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=authority_allowed,
    )
    return plan, evidence, token, snapshot, answer, result, authority_allowed


@pytest.mark.parametrize(
    ("status", "decision"),
    [
        (
            CapabilityOutcomeStatus.COMPLETE,
            MessageWindowCompletionDecision.READY_TO_PUBLISH,
        ),
        (
            CapabilityOutcomeStatus.PARTIAL,
            MessageWindowCompletionDecision.RETURN_PARTIAL,
        ),
        (
            CapabilityOutcomeStatus.EMPTY,
            MessageWindowCompletionDecision.RETURN_EMPTY,
        ),
        (
            CapabilityOutcomeStatus.UNAVAILABLE,
            MessageWindowCompletionDecision.RETURN_UNAVAILABLE,
        ),
        (CapabilityOutcomeStatus.DENIED, MessageWindowCompletionDecision.DENY),
    ],
)
def test_closed_status_matrix_and_unavailable_never_auto_retries(
    status: CapabilityOutcomeStatus,
    decision: MessageWindowCompletionDecision,
) -> None:
    plan, evidence, token, snapshot, answer, result, authority_allowed = _case(status)

    assert result.status is status
    assert (
        evaluate_message_window_completion(
            plan=plan,
            evidence=evidence,
            result=result,
            answer=answer,
            prepared_selection=token,
            current_snapshot=snapshot if authority_allowed else None,
            authority_rechecked=True,
            authority_allowed=authority_allowed,
        )
        is decision
    )


def test_renderer_is_byte_exact_to_attested_rows_and_rejects_fabricated_transcript() -> None:
    plan, snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )
    expected = (
        '[A1] 2026-08-22T11:22:00+03:00 user: "PRIVATE-BODY-ONE"\n'
        '[A2] 2026-08-22T11:23:00+03:00 assistant: "PRIVATE-BODY-TWO"\n\n'
        "Показано сообщений: 2 из 2. Окно полное."
    )

    assert answer.encode("utf-8") == expected.encode("utf-8")
    assert result.content_sha256 == token.visible_content_sha256 == _sha256(expected)
    fabricated = "[A1] invented\n[A2] invented\n\nПоказано сообщений: 2 из 2. Окно полное."
    forged_result = replace(result, content_sha256=_sha256(fabricated))
    with pytest.raises(MessageWindowOutcomeError, match="deterministic visible projection"):
        evaluate_message_window_completion(
            plan=plan,
            evidence=evidence,
            result=forged_result,
            answer=fabricated,
            prepared_selection=token,
            current_snapshot=snapshot,
            authority_rechecked=True,
            authority_allowed=True,
        )


def test_message_body_cannot_inject_structural_citation_labels() -> None:
    injected = _projection(
        rows=[
            dict(
                ROWS[0],
                content="body claims [A2] and [A999]",
            )
        ]
    )
    plan, snapshot, token, evidence = _prepared(projection=injected)
    answer, _result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )

    assert re.findall(r"\[A[1-9][0-9]{0,2}\]", answer) == ["[A1]"]
    assert "\\u005bA2\\u005d" in answer
    assert "\\u005bA999\\u005d" in answer


def test_result_cannot_change_while_reusing_the_real_visible_answer() -> None:
    plan, evidence, token, snapshot, answer, result, _allowed = _case(CapabilityOutcomeStatus.COMPLETE)
    assert token is not None and snapshot is not None
    with pytest.raises(MessageWindowOutcomeError, match="fully bound"):
        evaluate_message_window_completion(
            plan=plan,
            evidence=evidence,
            result=replace(result, content_sha256="f" * 64),
            answer=answer,
            prepared_selection=token,
            current_snapshot=snapshot,
            authority_rechecked=True,
            authority_allowed=True,
        )


def test_late_authority_denial_discards_prepared_evidence_and_visible_rows() -> None:
    plan, _snapshot_value, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=None,
        authority_allowed=False,
    )

    assert answer == MESSAGE_WINDOW_DENIED_RESPONSE
    assert result.status is CapabilityOutcomeStatus.DENIED
    assert result.evidence_identity_sha256 is None
    assert (
        evaluate_message_window_completion(
            plan=plan,
            evidence=evidence,
            result=result,
            answer=answer,
            prepared_selection=token,
            current_snapshot=None,
            authority_rechecked=True,
            authority_allowed=False,
        )
        is MessageWindowCompletionDecision.DENY
    )


def test_storage_mapping_without_opaque_authority_cannot_mint_a_snapshot() -> None:
    plan = _plan()
    projection = _projection()
    with pytest.raises(MessageWindowOutcomeError, match="process-private"):
        MessageWindowStorageAuthority()
    with pytest.raises(MessageWindowOutcomeError, match="authority"):
        attest_message_window_storage_projection(  # type: ignore[arg-type]
            projection,
            plan,
            tenant_id=TENANT,
            person_id=PERSON,
            conversation_id=CONVERSATION,
            timezone_name=TIMEZONE,
            projection=projection,
        )
    with pytest.raises(MessageWindowOutcomeError, match="sealed storage input"):
        prepare_message_window_selection(plan, projection)  # type: ignore[arg-type]
    fake = object.__new__(MessageWindowStorageAuthority)
    object.__setattr__(fake, "_authority", object())
    with pytest.raises(MessageWindowOutcomeError, match="authority"):
        attest_message_window_storage_projection(
            fake,
            plan,
            tenant_id=TENANT,
            person_id=PERSON,
            conversation_id=CONVERSATION,
            timezone_name=TIMEZONE,
            projection=projection,
        )


def test_token_is_sealed_digest_only_and_not_a_plaintext_dataclass() -> None:
    plan, snapshot, token, _evidence_value = _prepared()
    secrets_to_hide = (
        REQUEST,
        TENANT,
        PERSON,
        CONVERSATION,
        TIMEZONE,
        SINCE,
        UNTIL,
        "PRIVATE-BODY-ONE",
        "PRIVATE-BODY-TWO",
    )
    token_projection = repr(token) + json.dumps(
        [getattr(token, name) for name in token.__slots__],
        default=str,
    )

    assert message_window_selection_token_is_process_owned(token, plan=plan)
    assert not is_dataclass(token)
    assert not hasattr(token, "__dict__")
    for secret in secrets_to_hide:
        assert secret not in token_projection
    with pytest.raises(TypeError):
        asdict(token)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replace(token)  # type: ignore[type-var]
    with pytest.raises(TypeError, match="process-private"):
        pickle.dumps(token)
    with pytest.raises(TypeError, match="process-private"):
        copy.copy(token)
    with pytest.raises(TypeError, match="process-private"):
        copy.deepcopy(token)
    with pytest.raises(TypeError, match="immutable"):
        token._shown = 1  # type: ignore[misc]
    assert message_window_storage_snapshot_is_process_owned(snapshot)


def test_direct_token_construction_and_mutation_do_not_pass_the_seal() -> None:
    plan, _snapshot_value, token, _evidence_value = _prepared()
    with pytest.raises(MessageWindowOutcomeError, match="sealed storage input"):
        MessageWindowSelectionToken(
            plan_sha256=token.plan_sha256,
            snapshot_identity_sha256=token.snapshot_identity_sha256,
            row_ledger_sha256=token.row_ledger_sha256,
            boundary_identity_sha256=token.boundary_identity_sha256,
            visible_content_sha256=token.visible_content_sha256,
            citation_labels=token.citation_labels,
            shown=token.shown,
            total=token.total,
            complete=token.complete,
            identity_sha256=token.identity_sha256,
            seal_sha256="f" * 64,
        )
    object.__setattr__(token, "_shown", 1)
    assert not message_window_selection_token_is_process_owned(token, plan=plan)


def test_snapshot_is_opaque_immutable_and_non_exportable() -> None:
    snapshot = _snapshot()
    projection = repr(snapshot)

    assert not is_dataclass(snapshot)
    assert not hasattr(snapshot, "__dict__")
    assert REQUEST not in projection
    assert "PRIVATE-BODY-ONE" not in projection
    with pytest.raises(TypeError):
        asdict(snapshot)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replace(snapshot)  # type: ignore[type-var]
    with pytest.raises(TypeError, match="process-private"):
        pickle.dumps(snapshot)
    with pytest.raises(TypeError, match="immutable"):
        snapshot._total = 10  # type: ignore[misc]


def test_direct_snapshot_constructor_cannot_reuse_plain_storage_values() -> None:
    snapshot = _snapshot()
    with pytest.raises(MessageWindowOutcomeError, match="storage attestation"):
        MessageWindowStorageSnapshot(
            plan_sha256=snapshot._plan_sha256,
            tenant_id=snapshot._tenant_id,
            person_id=snapshot._person_id,
            timezone_name=snapshot._timezone_name,
            rows=snapshot._rows,
            boundary=snapshot._boundary,
            total=snapshot._total,
            visible_content=snapshot._visible_content,
            identity_sha256=snapshot.identity_sha256,
            seal_sha256="f" * 64,
        )


@pytest.mark.parametrize(
    ("boundary_change", "message"),
    [
        ({"id": "msg_eeeeeeeeeeeeeeee"}, "bound to its plan"),
        ({"user_id": "bob"}, "request or authority"),
        ({"conversation_id": "conv_0000000000000002"}, "request or authority"),
        ({"role": "assistant"}, "request or authority"),
        ({"content": "DIFFERENT REQUEST"}, "request or authority"),
        ({"created_at": "2026-08-21T20:59:59+00:00"}, "precedes"),
    ],
)
def test_boundary_must_attest_exact_request_owner_conversation_role_and_order(
    boundary_change: dict[str, object],
    message: str,
) -> None:
    boundary = dict(BOUNDARY)
    boundary.update(boundary_change)
    with pytest.raises(MessageWindowOutcomeError, match=message):
        _snapshot(projection=_projection(boundary=boundary))


@pytest.mark.parametrize("shape", ["missing", "open"])
def test_boundary_shape_is_closed(shape: str) -> None:
    boundary = dict(BOUNDARY)
    if shape == "missing":
        boundary.pop("content")
    else:
        boundary["metadata"] = {}
    with pytest.raises(MessageWindowOutcomeError, match="open shape"):
        _snapshot(projection=_projection(boundary=boundary))


def _mutated_rows(index: int = 0, **changes: object) -> list[dict[str, object]]:
    rows = [dict(row) for row in ROWS]
    rows[index].update(changes)
    return rows


@pytest.mark.parametrize(
    "rows",
    [
        [dict(ROWS[0], extra="open")],
        _mutated_rows(id="invalid"),
        _mutated_rows(id=BOUNDARY_ID),
        [ROWS[0], ROWS[0]],
        _mutated_rows(user_id="bob"),
        _mutated_rows(conversation_id="conv_0000000000000002"),
        _mutated_rows(role="tool"),
        _mutated_rows(created_at="2026-08-21T20:59:59+00:00"),
        [ROWS[1], ROWS[0]],
        _mutated_rows(content="x" * 65_537),
    ],
)
def test_rows_reject_open_duplicate_foreign_out_of_window_or_oversize_input(
    rows: list[dict[str, object]],
) -> None:
    with pytest.raises(MessageWindowOutcomeError):
        _snapshot(projection=_projection(rows=rows))


def test_exact_list_and_dict_types_are_required() -> None:
    class RowList(list[dict[str, object]]):
        pass

    class RowDict(dict[str, object]):
        pass

    projection = _projection()
    projection["results"] = RowList([dict(ROWS[0])])
    projection["shown"] = 1
    projection["total"] = 1
    with pytest.raises(MessageWindowOutcomeError, match="list shape"):
        _snapshot(projection=projection)

    projection = _projection(rows=[dict(ROWS[0])])
    projection["results"] = [RowDict(ROWS[0])]
    with pytest.raises(MessageWindowOutcomeError, match="open shape"):
        _snapshot(projection=projection)


def test_same_timestamp_keeps_storage_rowid_order_instead_of_sorting_by_message_id() -> None:
    same_stamp = "2026-08-22T08:22:00+00:00"
    rows = [
        dict(ROWS[1], created_at=same_stamp),
        dict(ROWS[0], created_at=same_stamp),
    ]
    plan, snapshot, token, evidence = _prepared(projection=_projection(rows=rows))
    answer, _result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )

    assert answer.index("PRIVATE-BODY-TWO") < answer.index("PRIVATE-BODY-ONE")


def test_current_minute_window_may_end_after_the_owned_rowid_boundary() -> None:
    plan = _plan(
        since_utc="2026-08-22T21:00:00+00:00",
        until_utc="2026-08-22T21:01:00+00:00",
    )
    boundary = dict(BOUNDARY, created_at="2026-08-22T21:00:30+00:00")
    rows = [
        dict(
            ROWS[0],
            created_at="2026-08-22T21:00:30+00:00",
        )
    ]
    projection = _projection(
        rows=rows,
        boundary=boundary,
        since="2026-08-22T21:00:00+00:00",
        until="2026-08-22T21:01:00+00:00",
    )
    snapshot = _snapshot(plan=plan, projection=projection)

    assert message_window_storage_snapshot_is_process_owned(snapshot)
    assert prepare_message_window_selection(plan, snapshot).shown == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"shown": 1},
        {"total": 3},
        {"complete": False},
        {"limit": 19},
        {"since": "2026-08-21T20:59:59+00:00"},
        {"until": "2026-08-22T21:00:01+00:00"},
        {"role": "user"},
    ],
)
def test_storage_coverage_and_scope_claims_are_recomputed(changes: dict[str, object]) -> None:
    with pytest.raises(MessageWindowOutcomeError):
        _snapshot(projection=_projection(**changes))


def test_partial_requires_the_exact_closed_page_and_empty_is_attested() -> None:
    plan, _partial_snapshot, partial_token, partial = _prepared(
        projection=_projection(rows=_many_rows(), total=21)
    )
    assert partial.status is CapabilityOutcomeStatus.PARTIAL
    assert partial_token.shown == MESSAGE_WINDOW_MAX_MESSAGES
    assert partial_token.total == 21

    empty_snapshot = _snapshot(plan=plan, projection=_projection(rows=[]))
    empty_token = prepare_message_window_selection(plan, empty_snapshot)
    empty = MessageWindowEvidence.from_selection(plan, empty_token)
    assert empty.status is CapabilityOutcomeStatus.EMPTY
    assert empty.identity_sha256 is not None
    answer, _result = render_message_window_result(
        plan,
        empty,
        selection=empty_token,
        snapshot=empty_snapshot,
        authority_allowed=True,
    )
    assert answer == MESSAGE_WINDOW_EMPTY_RESPONSE

    with pytest.raises(MessageWindowOutcomeError, match="coverage"):
        _snapshot(projection=_projection(rows=ROWS, total=3))


@pytest.mark.parametrize(
    "mutation",
    ["content", "role", "created_at", "deleted", "inserted", "boundary_time"],
)
def test_final_reselection_detects_every_mutable_snapshot_dimension(mutation: str) -> None:
    plan, prepared_snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=prepared_snapshot,
        authority_allowed=True,
    )
    projection = _projection()
    if mutation == "content":
        projection = _projection(rows=_mutated_rows(content="CHANGED-CONTENT"))
    elif mutation == "role":
        projection = _projection(rows=_mutated_rows(role="assistant"))
    elif mutation == "created_at":
        projection = _projection(rows=_mutated_rows(created_at="2026-08-22T08:22:30+00:00"))
    elif mutation == "deleted":
        projection = _projection(rows=[dict(ROWS[0])])
    elif mutation == "inserted":
        projection = _projection(
            rows=[
                *[dict(row) for row in ROWS],
                {
                    "id": "msg_0000000000000003",
                    "conversation_id": CONVERSATION,
                    "user_id": PERSON,
                    "role": "user",
                    "content": "INSERTED",
                    "created_at": "2026-08-22T08:24:00+00:00",
                },
            ]
        )
    else:
        projection = _projection(boundary=dict(BOUNDARY, created_at="2026-08-22T21:02:00+00:00"))
    current = _snapshot(plan=plan, projection=projection)

    assert current.identity_sha256 != prepared_snapshot.identity_sha256
    with pytest.raises(MessageWindowOutcomeError, match="changed before publication"):
        evaluate_message_window_completion(
            plan=plan,
            evidence=evidence,
            result=result,
            answer=answer,
            prepared_selection=token,
            current_snapshot=current,
            authority_rechecked=True,
            authority_allowed=True,
        )


@pytest.mark.parametrize(
    "field",
    [
        "selection_sha256",
        "snapshot_identity_sha256",
        "row_ledger_sha256",
        "boundary_identity_sha256",
        "visible_content_sha256",
    ],
)
def test_gate_cross_binds_every_evidence_identity(field: str) -> None:
    plan, snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )
    mutated_evidence = replace(evidence, **{field: "f" * 64})

    with pytest.raises(MessageWindowOutcomeError):
        evaluate_message_window_completion(
            plan=plan,
            evidence=mutated_evidence,
            result=result,
            answer=answer,
            prepared_selection=token,
            current_snapshot=snapshot,
            authority_rechecked=True,
            authority_allowed=True,
        )


@pytest.mark.parametrize(
    "field",
    [
        "plan_sha256",
        "evidence_identity_sha256",
        "selection_sha256",
        "snapshot_identity_sha256",
        "row_ledger_sha256",
        "boundary_identity_sha256",
        "content_sha256",
    ],
)
def test_gate_cross_binds_every_result_identity(field: str) -> None:
    plan, snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )
    mutated_result = replace(result, **{field: "f" * 64})

    with pytest.raises(MessageWindowOutcomeError):
        evaluate_message_window_completion(
            plan=plan,
            evidence=evidence,
            result=mutated_result,
            answer=answer,
            prepared_selection=token,
            current_snapshot=snapshot,
            authority_rechecked=True,
            authority_allowed=True,
        )


def test_gate_cross_binds_status_counts_completeness_and_labels() -> None:
    plan, snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )
    evidence_mutations = [
        {"shown": 1, "total": 1, "citation_labels": ("A1",)},
        {"status": CapabilityOutcomeStatus.EMPTY, "shown": 0, "total": 0, "citation_labels": ()},
    ]
    result_mutations = [
        {"shown": 1, "total": 1, "citation_labels": ("A1",)},
        {"status": CapabilityOutcomeStatus.EMPTY, "shown": 0, "total": 0, "citation_labels": ()},
    ]
    for mutation in evidence_mutations:
        with pytest.raises(MessageWindowOutcomeError):
            evaluate_message_window_completion(
                plan=plan,
                evidence=replace(evidence, **mutation),
                result=result,
                answer=answer,
                prepared_selection=token,
                current_snapshot=snapshot,
                authority_rechecked=True,
                authority_allowed=True,
            )
    for mutation in result_mutations:
        with pytest.raises(MessageWindowOutcomeError):
            evaluate_message_window_completion(
                plan=plan,
                evidence=evidence,
                result=replace(result, **mutation),
                answer=answer,
                prepared_selection=token,
                current_snapshot=snapshot,
                authority_rechecked=True,
                authority_allowed=True,
            )


@pytest.mark.parametrize(
    "mutation",
    ["missing_prepared", "missing_current", "authority_not_rechecked", "authority_type"],
)
def test_gate_requires_exact_final_authority_and_snapshot_inputs(mutation: str) -> None:
    plan, snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )
    inputs: dict[str, object] = {
        "plan": plan,
        "evidence": evidence,
        "result": result,
        "answer": answer,
        "prepared_selection": token,
        "current_snapshot": snapshot,
        "authority_rechecked": True,
        "authority_allowed": True,
    }
    if mutation == "missing_prepared":
        inputs["prepared_selection"] = None
    elif mutation == "missing_current":
        inputs["current_snapshot"] = None
    elif mutation == "authority_not_rechecked":
        inputs["authority_rechecked"] = False
    else:
        inputs["authority_allowed"] = 1
    with pytest.raises(MessageWindowOutcomeError):
        evaluate_message_window_completion(**inputs)  # type: ignore[arg-type]


def test_public_plan_evidence_result_and_generic_receipt_are_privacy_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )
    monkeypatch.setattr(
        capability_outcome_module,
        "_OUTCOME_ROUTES",
        capability_outcome_module._OUTCOME_ROUTES | {RouteClass.ORDINARY_DIALOGUE},
    )
    decision, outcome = accept_message_window_capability_outcome(
        plan=plan,
        evidence=evidence,
        result=result,
        answer=answer,
        prepared_selection=token,
        current_snapshot=snapshot,
        authority_rechecked=True,
        authority_allowed=True,
    )
    metadata: dict[str, object] = {}
    receipt = attach_accepted_capability_outcome_receipt(metadata, outcome)
    loaded = load_accepted_capability_outcome_receipt(metadata, expected_outcome=outcome)
    projection = json.dumps(
        {
            "plan": plan.payload(),
            "evidence": asdict(evidence),
            "result": asdict(result),
            "outcome": outcome.to_payload(),
            "receipt": receipt.to_payload(),
        },
        ensure_ascii=False,
        default=str,
    )

    assert type(outcome) is CapabilityOutcome
    assert decision is MessageWindowCompletionDecision.READY_TO_PUBLISH
    assert outcome.route is RouteClass.ORDINARY_DIALOGUE
    assert loaded == receipt
    for secret in (
        REQUEST,
        TENANT,
        PERSON,
        CONVERSATION,
        BOUNDARY_ID,
        TIMEZONE,
        SINCE,
        UNTIL,
        "PRIVATE-BODY-ONE",
        "PRIVATE-BODY-TWO",
    ):
        assert secret not in projection


@pytest.mark.parametrize("status", tuple(CapabilityOutcomeStatus))
def test_accepted_gate_emits_only_the_existing_generic_outcome(
    status: CapabilityOutcomeStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capability_outcome_module,
        "_OUTCOME_ROUTES",
        capability_outcome_module._OUTCOME_ROUTES | {RouteClass.ORDINARY_DIALOGUE},
    )
    plan, evidence, token, snapshot, answer, result, authority_allowed = _case(status)
    decision, outcome = accept_message_window_capability_outcome(
        plan=plan,
        evidence=evidence,
        result=result,
        answer=answer,
        prepared_selection=token,
        current_snapshot=snapshot if authority_allowed else None,
        authority_rechecked=True,
        authority_allowed=authority_allowed,
    )

    assert isinstance(decision, MessageWindowCompletionDecision)
    assert type(outcome) is CapabilityOutcome
    assert outcome.route is RouteClass.ORDINARY_DIALOGUE
    assert outcome.status is status
    assert outcome.evidence_identity_sha256 == (
        evidence.identity_sha256
        if status
        in {
            CapabilityOutcomeStatus.COMPLETE,
            CapabilityOutcomeStatus.PARTIAL,
            CapabilityOutcomeStatus.EMPTY,
        }
        else None
    )


def test_generic_outcome_fails_closed_until_central_route_allowlist_lands() -> None:
    plan, snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=snapshot,
        authority_allowed=True,
    )
    if RouteClass.ORDINARY_DIALOGUE not in capability_outcome_module._OUTCOME_ROUTES:
        with pytest.raises(CapabilityOutcomeError, match="route is not admitted"):
            accept_message_window_capability_outcome(
                plan=plan,
                evidence=evidence,
                result=result,
                answer=answer,
                prepared_selection=token,
                current_snapshot=snapshot,
                authority_rechecked=True,
                authority_allowed=True,
            )
    else:
        assert (
            accept_message_window_capability_outcome(
                plan=plan,
                evidence=evidence,
                result=result,
                answer=answer,
                prepared_selection=token,
                current_snapshot=snapshot,
                authority_rechecked=True,
                authority_allowed=True,
            )[1].route
            is RouteClass.ORDINARY_DIALOGUE
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"conversation_id": "invalid"}, "conversation_id"),
        ({"boundary_message_id": "invalid"}, "boundary_message_id"),
        ({"timezone_name": "Not/AZone"}, "timezone_name"),
        ({"since_utc": "2026-08-22T00:00:00"}, "offset"),
        ({"since_utc": UNTIL}, "non-empty"),
        ({"until_utc": SINCE}, "non-empty"),
        ({"role": "system"}, "role"),
        ({"max_messages": 19}, "limit"),
        ({"tenant_id": " owner"}, "canonical"),
    ],
)
def test_plan_rejects_ambiguous_scope(change: dict[str, object], message: str) -> None:
    with pytest.raises(MessageWindowOutcomeError, match=message):
        _plan(**change)


def test_equivalent_offsets_have_one_plan_and_snapshot_identity() -> None:
    shifted = _plan(
        since_utc="2026-08-22T00:00:00+03:00",
        until_utc="2026-08-23T00:00:00+03:00",
    )
    first = _snapshot(plan=_plan())
    shifted_projection = _projection(
        since="2026-08-22T00:00:00+03:00",
        until="2026-08-23T00:00:00+03:00",
    )
    second = _snapshot(plan=shifted, projection=shifted_projection)

    assert shifted == _plan()
    assert first.identity_sha256 == second.identity_sha256


def test_source_free_fallbacks_are_exact_and_cannot_retain_snapshot() -> None:
    plan = _plan()
    unavailable = MessageWindowEvidence.source_free(
        plan,
        CapabilityOutcomeStatus.UNAVAILABLE,
    )
    answer, result = render_message_window_result(
        plan,
        unavailable,
        selection=None,
        snapshot=None,
        authority_allowed=True,
    )
    assert answer == MESSAGE_WINDOW_UNAVAILABLE_RESPONSE
    assert result.content_sha256 == _sha256(MESSAGE_WINDOW_UNAVAILABLE_RESPONSE)

    with pytest.raises(MessageWindowOutcomeError, match="retained a selection"):
        render_message_window_result(
            plan,
            unavailable,
            selection=prepare_message_window_selection(plan, _snapshot(plan=plan)),
            snapshot=None,
            authority_allowed=True,
        )


def test_denied_gate_rejects_retained_current_snapshot() -> None:
    plan, snapshot, token, evidence = _prepared()
    answer, result = render_message_window_result(
        plan,
        evidence,
        selection=token,
        snapshot=None,
        authority_allowed=False,
    )
    with pytest.raises(MessageWindowOutcomeError, match="retained current"):
        evaluate_message_window_completion(
            plan=plan,
            evidence=evidence,
            result=result,
            answer=answer,
            prepared_selection=token,
            current_snapshot=snapshot,
            authority_rechecked=True,
            authority_allowed=False,
        )


def test_boundary_timestamp_drift_changes_identity_even_when_rows_are_unchanged() -> None:
    plan = _plan()
    first = _snapshot(plan=plan)
    second = _snapshot(
        plan=plan,
        projection=_projection(boundary=dict(BOUNDARY, created_at="2026-08-22T21:02:00+00:00")),
    )
    first_token = prepare_message_window_selection(plan, first)
    second_token = prepare_message_window_selection(plan, second)

    assert first.identity_sha256 != second.identity_sha256
    assert first_token.boundary_identity_sha256 != second_token.boundary_identity_sha256
    assert first_token.identity_sha256 != second_token.identity_sha256


def test_boundary_content_drift_cannot_be_resealed_as_the_original_request() -> None:
    projection = _projection(boundary=dict(BOUNDARY, content="ALTERED"))
    with pytest.raises(MessageWindowOutcomeError, match="request or authority"):
        _snapshot(projection=projection)
