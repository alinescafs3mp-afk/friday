"""Frozen B03 speech and altered-data controls through the real runtime.

The single permitted model role is an intent arbiter; no model supplies counts.
Synthetic sources only. This profile supplies no configured-model/live credit.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime, _bounded_attachment_projection
from friday.agent_runtime._office_attachments import (
    OFFICE_STRUCTURE_KEY,
    code_owned_office_answer,
    office_arbiter_applies,
    office_exact_request_detected,
    office_request_kind,
    trusted_office_attachment,
)
from friday.documents import DocumentExtractor
from friday.permissions import AuthorizationService
from tools import synthetic_live_battery as battery

A_CASES = [
    case
    for case in battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS["A"]))
    if case.pass_index == 3
]
B_CASES = [
    case
    for case in battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS["B"]))
    if case.pass_index == 3
]
UNBOUND_B15 = next(case for case in B_CASES if case.id == "SYN-B03-15")
SOURCE_BOUND_CASES = [*A_CASES, *(case for case in B_CASES if case.id != UNBOUND_B15.id)]


class CountArbiter:
    enabled = True
    model = "synthetic-csv-intent"
    total_budget_sec = 30.0

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kwargs):
        del kwargs
        self.calls += 1
        system = next(m["content"] for m in messages if m["role"] == "system")
        assert "арбитр" in system.casefold(), "B03 escaped the owned evidence path into answer generation"
        return {"content": '{"kind": "count_records"}'}


async def _empty_context(user_id, message, conversation_id, **kwargs):
    del message, kwargs
    return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)


def _attachment(payload):
    extraction = DocumentExtractor(secret_values=()).extract(payload, "synthetic-register.csv")
    assert extraction.success
    return trusted_office_attachment(
        {
            "filename": "synthetic-register.csv",
            "transient_text": extraction.text,
            "extraction_success": True,
            "verification_eligible": True,
            OFFICE_STRUCTURE_KEY: extraction.office_structure_index,
        }
    )


async def _ask(case, payload, settings, storage, monkeypatch):
    attachment = _attachment(payload)
    storage.ensure_user("alice", preset_key="owner")
    model = CountArbiter()
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1), storage, llm=model
    )
    monkeypatch.setattr(runtime, "_prepare_context", _empty_context)
    result = await runtime.chat(
        "alice",
        case.question,
        actor=AuthorizationService(storage).actor_for_user("alice", source="test"),
        attachments=[attachment],
        enable_tools=True,
    )
    rows = storage.get_conversation_messages(result["conversation_id"], user_id="alice")
    metadata = json.loads(rows[-1]["metadata_json"])
    assert metadata["structural"]["verdict_kind"] == "office_exact"
    assert metadata["structural"]["model_spoke"] is False
    assert metadata["attachment_context_used"] is True
    assert result["verification_status"] == "passed"
    assert result["attachment_coverage_complete"] is True
    assert result["tools_used"] == [] and result["files"] == []
    # Independently classified prompt contracts for the prospective count
    # repair. Verify the real caller's model boundary, not just leaf routing.
    structural_cases = {
        "A": {3, 6, 8, 15, 19, 20},
        "B": {2, 5, 6, 8, 9, 10, 11, 12, 16, 17, 18, 19, 20},
    }
    assert model.calls == (0 if case.question_index in structural_cases[case.battery_id] else 1)
    return result


@pytest.mark.parametrize("case", SOURCE_BOUND_CASES, ids=lambda case: case.id)
@pytest.mark.asyncio
async def test_all_source_bound_frozen_a_b03_requests_reach_owned_answer(
    case, settings, storage, monkeypatch
):
    document = battery._case_document(case)
    assert document is not None
    result = await _ask(
        case, base64.b64decode(document["content_base64"], validate=True), settings, storage, monkeypatch
    )
    content = battery.oracle_for_case(case)["content"]
    if content["office_structure_summary"] is not None:
        assert battery._office_structure_summary_matches(
            result["message"], content["office_structure_summary"]
        )
    else:
        expected = battery._expected_document_row_count(case)
        assert battery._answer_has_affirmative_integer(result["message"], expected)
        assert not battery._answer_conflicting_integer_values(result["message"], expected)


def test_unbound_b15_is_an_explicit_oracle_correction_instead_of_a_row_count():
    """A product-path name alone does not bind the attached CSV as its source."""

    document = battery._case_document(UNBOUND_B15)
    assert document is not None
    projected = _bounded_attachment_projection(
        [_attachment(base64.b64decode(document["content_base64"], validate=True))]
    )
    assert office_request_kind(UNBOUND_B15.question) == ""
    assert not office_exact_request_detected(UNBOUND_B15.question)
    assert not office_arbiter_applies(UNBOUND_B15.question, projected)
    assert (
        code_owned_office_answer(
            UNBOUND_B15.question,
            projected,
            kind_override="count_records",
        )
        is None
    )


@pytest.mark.parametrize(
    ("index", "data", "expected"),
    [
        (8, "ID,Статус\nA,готово\n,готово\nB,готово\n", 2),
        (9, "ID,Статус\nA,готово\nB,готово\n,\n", 2),
        (12, "ID,Статус\nA,готово\nA,готово\nB,готово\n", 2),
        (12, "ID,Статус\nA,готово\na,готово\nB,готово\n", 3),
    ],
)
@pytest.mark.asyncio
async def test_frozen_predicate_meaning_survives_data_changes(
    index, data, expected, settings, storage, monkeypatch
):
    result = await _ask(B_CASES[index - 1], data.encode(), settings, storage, monkeypatch)
    assert battery._answer_has_affirmative_integer(result["message"], expected)
    assert not battery._answer_conflicting_integer_values(result["message"], expected)
