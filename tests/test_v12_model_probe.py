from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import replace
from typing import Any

import pytest

import friday.model_probe as model_probe_module
from friday.evidence_bundle import CitationBinding, EvidenceBundle, EvidencePart
from friday.model_probe import (
    CANCELLATION_PROBE,
    CONTEXT_PROBE,
    PLAN_PROBE_CASES,
    SYNTHESIS_PROBE,
    SYNTHESIS_PROBES,
    VERIFIER_PROBES,
    CancellationProbeRequest,
    CancellationProbeResult,
    ContextProbeRequest,
    ModelLoadSample,
    ModelProbeError,
    ModelProbeFailure,
    PlanProbeCase,
    ProbeCompletion,
    SynthesisProbeRequest,
    VerifierProbeRequest,
    run_v12_live_probe,
)
from friday.model_profiles import (
    QWEN36_27B_V12_PROFILE,
    ModelCapability,
    ModelEffect,
    V12LiveAttestation,
)
from friday.orchestration.contracts import EvidenceKind, OutputFormat, RouteClass
from friday.orchestration.file_read_contract import (
    build_file_verifier_messages,
    validate_file_synthesis_answer,
)

_BINDING = "a" * 64
_EPOCH_SHA256 = "c" * 64
_OTHER_EPOCH_SHA256 = "d" * 64
_CANCELLATION_WITNESS = model_probe_module._cancellation_request_witness_sha256(CANCELLATION_PROBE)


def _plan_payload(case: PlanProbeCase, **changes: Any) -> dict[str, Any]:
    route = case.expected_route
    evidence: list[dict[str, Any]] = []
    if route is RouteClass.FILE_READ:
        evidence = [{"kind": "attached_files", "query": "", "max_items": 2, "required": True}]
    elif route is RouteClass.ARCHIVE_READ:
        evidence = [{"kind": "archive", "query": "probe", "max_items": 10, "required": True}]
    elif route is RouteClass.WEB_READ:
        evidence = [{"kind": "web", "query": "probe", "max_items": 5, "required": True}]
    payload: dict[str, Any] = {
        "schema": "friday.turn-plan.v1",
        "route": route.value,
        "objective": "synthetic objective",
        "evidence_requests": evidence,
        "tool_intents": [],
        "output": {
            "format": (case.expected_output_format or OutputFormat.TEXT).value,
            "language": "ru",
            "require_citations": route
            in {RouteClass.FILE_READ, RouteClass.ARCHIVE_READ, RouteClass.WEB_READ},
            "one_message": True,
        },
        "confidence": 0.9,
        "fallback": "legacy",
        "reason_code": case.case_id,
    }
    payload.update(changes)
    return payload


def _plan_response(case: PlanProbeCase, **changes: Any) -> ProbeCompletion:
    response = ProbeCompletion(
        content=json.dumps(_plan_payload(case), ensure_ascii=False, separators=(",", ":")),
        finish_reason="stop",
        tool_calls=(),
        prompt_tokens=1_024,
    )
    return replace(response, **changes)


class _Client:
    def __init__(self) -> None:
        self.loads: list[ModelLoadSample] = [
            ModelLoadSample(0, 0, _EPOCH_SHA256),
            ModelLoadSample(0, 0, _EPOCH_SHA256),
            ModelLoadSample(0, 0, _EPOCH_SHA256),
            ModelLoadSample(0, 0, _EPOCH_SHA256),
        ]
        self.load_index = 0
        self.load_times: list[float] = []
        self.plan_overrides: dict[str, ProbeCompletion] = {}
        self.syntheses = {
            "production_file_synthesis_1": ProbeCompletion(
                content="Код синтетического проекта: СЕВЕР-42 [A1].",
                finish_reason="stop",
                tool_calls=(),
                prompt_tokens=256,
            ),
            "production_file_synthesis_2": ProbeCompletion(
                content=(
                    "Код синтетического проекта: СЕВЕР-42 [A1]. "
                    "Контрольная дата синтетического проекта: 7 октября 2099 года [A2]."
                ),
                finish_reason="stop",
                tool_calls=(),
                prompt_tokens=256,
            ),
        }
        self.verifier: dict[str, ProbeCompletion] = {
            "production_file_synthesis_1_verifier_clear": ProbeCompletion(
                content=(
                    '{"schema":"friday.v12-file-verifier.v1","supported":true,'
                    '"citation_labels":["A1"],"unsupported_claims":0}'
                ),
                finish_reason="stop",
                tool_calls=(),
                prompt_tokens=256,
            ),
            "production_file_synthesis_2_verifier_clear": ProbeCompletion(
                content=(
                    '{"schema":"friday.v12-file-verifier.v1","supported":true,'
                    '"citation_labels":["A1","A2"],"unsupported_claims":0}'
                ),
                finish_reason="stop",
                tool_calls=(),
                prompt_tokens=256,
            ),
            "verifier_case_79": ProbeCompletion(
                content=(
                    '{"schema":"friday.v12-file-verifier.v1","supported":false,'
                    '"citation_labels":["A1","A2"],"unsupported_claims":1}'
                ),
                finish_reason="stop",
                tool_calls=(),
                prompt_tokens=256,
            ),
        }
        self.context = ProbeCompletion(
            content=json.dumps(
                {"начало": CONTEXT_PROBE.start_marker, "конец": CONTEXT_PROBE.end_marker},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            finish_reason="stop",
            tool_calls=(),
            prompt_tokens=8_192,
        )
        self.cancellation = CancellationProbeResult(
            "submitted",
            _CANCELLATION_WITNESS,
            True,
            350,
        )
        self.plan_ids: list[str] = []
        self.deadlines: list[float] = []
        self.requests: list[object] = []
        self.raise_phase = ""
        self.block_plan = False
        self.plan_started = asyncio.Event()
        self.plan_cancelled = False
        self.ignore_first_plan_cancellation = False
        self.ignored_plan_cancellation = asyncio.Event()
        self.release_hostile_plan = asyncio.Event()

    async def sample_load(self, *, absolute_deadline: float) -> ModelLoadSample:
        self.deadlines.append(absolute_deadline)
        self.load_times.append(time.monotonic())
        if self.raise_phase == "load":
            raise RuntimeError("private load exception")
        value = self.loads[min(self.load_index, len(self.loads) - 1)]
        self.load_index += 1
        return value

    async def complete_plan(
        self,
        case: PlanProbeCase,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        self.deadlines.append(absolute_deadline)
        self.plan_ids.append(case.case_id)
        self.requests.append(case)
        if self.raise_phase == "plan":
            raise RuntimeError("private plan response")
        if self.block_plan:
            self.plan_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.plan_cancelled = True
                if self.ignore_first_plan_cancellation:
                    self.ignored_plan_cancellation.set()
                    await self.release_hostile_plan.wait()
                    return _plan_response(case)
                raise
        return self.plan_overrides.get(case.case_id, _plan_response(case))

    async def complete_synthesis(
        self,
        request: SynthesisProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        self.deadlines.append(absolute_deadline)
        self.requests.append(request)
        if self.raise_phase == "synthesis":
            raise RuntimeError("private synthesis response")
        return self.syntheses[request.case_id]

    async def complete_verifier(
        self,
        request: VerifierProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        self.deadlines.append(absolute_deadline)
        self.requests.append(request)
        if self.raise_phase == "verifier":
            raise RuntimeError("private verifier response")
        return self.verifier[request.case_id]

    async def complete_context(
        self,
        request: ContextProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion:
        self.deadlines.append(absolute_deadline)
        self.requests.append(request)
        if self.raise_phase == "context":
            raise RuntimeError("private context response")
        return self.context

    async def cancel_and_drain(
        self,
        request: CancellationProbeRequest,
        *,
        absolute_deadline: float,
    ) -> CancellationProbeResult:
        self.deadlines.append(absolute_deadline)
        self.requests.append(request)
        if self.raise_phase == "cancellation":
            raise RuntimeError("private cancellation response")
        return self.cancellation


async def _run(client: _Client, *, deadline: float | None = None) -> V12LiveAttestation:
    return await run_v12_live_probe(
        QWEN36_27B_V12_PROFILE,
        client,
        endpoint_binding_sha256=_BINDING,
        absolute_deadline=deadline if deadline is not None else time.monotonic() + 10,
    )


@pytest.mark.asyncio
async def test_clear_suite_returns_only_the_sanitized_read_only_attestation() -> None:
    client = _Client()

    report = await _run(client)

    assert isinstance(report, V12LiveAttestation)
    assert report.profile_id == QWEN36_27B_V12_PROFILE.profile_id
    assert report.capabilities == frozenset(
        {
            ModelCapability.TURN_PLAN_V1,
            ModelCapability.RU_PLANNING,
            ModelCapability.PREPARED_EVIDENCE_2,
            ModelCapability.CONTEXT_8K,
            ModelCapability.REMOTE_CANCELLATION,
        }
    )
    assert ModelCapability.RAW_VISION not in report.capabilities
    assert ModelCapability.NATIVE_TOOL_CALLS not in report.capabilities
    assert report.verified_context_tokens == 8_192
    assert report.max_prepared_evidence_items == 2
    assert report.max_tool_steps == 0
    assert report.allowed_effects == frozenset({ModelEffect.READ})
    assert report.verifier_required is True
    assert [request.case_id for request in client.requests if isinstance(request, SynthesisProbeRequest)] == [
        request.case_id for request in SYNTHESIS_PROBES
    ]
    assert [request.case_id for request in client.requests if isinstance(request, VerifierProbeRequest)] == [
        "production_file_synthesis_1_verifier_clear",
        "production_file_synthesis_2_verifier_clear",
        *[request.case_id for request in VERIFIER_PROBES],
    ]
    assert len(client.load_times) == 4
    assert (
        client.load_times[-1] - client.load_times[-2]
        >= model_probe_module.POST_CANCELLATION_QUIET_INTERVAL_SEC * 0.9
    )
    assert _BINDING not in repr(report)
    assert _EPOCH_SHA256 not in repr(report)


@pytest.mark.asyncio
async def test_probe_runs_the_exact_nine_ru_route_cases_under_one_deadline() -> None:
    client = _Client()
    deadline = time.monotonic() + 10

    await _run(client, deadline=deadline)

    assert client.plan_ids == [
        "file_summary",
        "file_compare",
        "file_ocr",
        "effect_document",
        "effect_reminder",
        "archive_date",
        "web_current",
        "small_talk",
        "ordinary_dialogue",
    ]
    assert len(PLAN_PROBE_CASES) == 9
    assert [case.expected_route for case in PLAN_PROBE_CASES] == [
        RouteClass.FILE_READ,
        RouteClass.FILE_READ,
        RouteClass.FILE_READ,
        RouteClass.EFFECT,
        RouteClass.EFFECT,
        RouteClass.ARCHIVE_READ,
        RouteClass.WEB_READ,
        RouteClass.SMALL_TALK,
        RouteClass.ORDINARY_DIALOGUE,
    ]
    assert all(case.expected_language == "ru" for case in PLAN_PROBE_CASES)
    assert client.deadlines and all(value == deadline for value in client.deadlines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        ProbeCompletion("```json\n{}\n```", "stop", (), 10),
        ProbeCompletion('{"schema":"friday.turn-plan.v1","schema":"forged"}', "stop", (), 10),
        ProbeCompletion("{}", "length", (), 10),
        ProbeCompletion("{}", "stop", ("forbidden",), 10),
    ],
)
async def test_plan_requires_one_strict_json_object_and_no_protocol_tool_call(
    replacement: ProbeCompletion,
) -> None:
    client = _Client()
    client.plan_overrides[PLAN_PROBE_CASES[0].case_id] = replacement

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.PLAN_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["route", "language", "intent", "format"])
async def test_plan_requires_exact_route_ru_language_zero_tools_and_output_shape(mutation: str) -> None:
    client = _Client()
    case = PLAN_PROBE_CASES[0]
    payload = _plan_payload(case)
    if mutation == "route":
        payload["route"] = "effect"
        payload["evidence_requests"] = []
        payload["output"]["require_citations"] = False
    elif mutation == "language":
        payload["output"]["language"] = "en"
    elif mutation == "intent":
        payload["tool_intents"] = [{"name": "probe", "arguments": {}, "effect": "read", "purpose": "probe"}]
    else:
        payload["output"]["format"] = "table"
    client.plan_overrides[case.case_id] = _plan_response(
        case,
        content=json.dumps(payload, ensure_ascii=False),
    )

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.PLAN_INVALID


def test_effect_plan_may_declare_a_write_intent_but_never_emit_protocol_tool_calls() -> None:
    case = next(item for item in PLAN_PROBE_CASES if item.case_id == "effect_document")
    payload = _plan_payload(case)
    payload["tool_intents"] = [
        {
            "name": "documents.create",
            "arguments": {"format": "docx"},
            "effect": "write",
            "purpose": "create the requested document",
        }
    ]

    model_probe_module._evaluate_plan(
        case,
        _plan_response(case, content=json.dumps(payload, ensure_ascii=False)),
    )

    with pytest.raises(ModelProbeError) as raised:
        model_probe_module._evaluate_plan(
            case,
            ProbeCompletion(
                content=json.dumps(payload, ensure_ascii=False),
                finish_reason="stop",
                tool_calls=("forbidden-protocol-call",),
                prompt_tokens=10,
            ),
        )
    assert raised.value.code is ModelProbeFailure.PLAN_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["insufficient_max_items", "extra_conversation"])
async def test_file_plan_probe_requires_the_exact_production_applicability_shape(
    mutation: str,
) -> None:
    client = _Client()
    case = PLAN_PROBE_CASES[1]
    payload = _plan_payload(case)
    if mutation == "insufficient_max_items":
        payload["evidence_requests"][0]["max_items"] = 1
    else:
        payload["evidence_requests"].append(
            {"kind": "conversation", "query": "", "max_items": 1, "required": True}
        )
    client.plan_overrides[case.case_id] = _plan_response(
        case,
        content=json.dumps(payload, ensure_ascii=False),
    )

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.PLAN_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["insufficient_max_items", "extra_conversation", "optional_archive"])
async def test_archive_plan_probe_requires_the_exact_bounded_handler_shape(mutation: str) -> None:
    client = _Client()
    case = next(item for item in PLAN_PROBE_CASES if item.case_id == "archive_date")
    payload = _plan_payload(case)
    if mutation == "insufficient_max_items":
        payload["evidence_requests"][0]["max_items"] = 1
    elif mutation == "extra_conversation":
        payload["evidence_requests"].append(
            {"kind": "conversation", "query": "", "max_items": 1, "required": True}
        )
    else:
        payload["evidence_requests"][0]["required"] = False
    client.plan_overrides[case.case_id] = _plan_response(
        case,
        content=json.dumps(payload, ensure_ascii=False),
    )

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.PLAN_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '{"claims":[{"fact":"Код синтетического проекта: СЕВЕР-42","citation":"A1"}]}',
        (
            '{"claims":['
            '{"fact":"Код синтетического проекта: СЕВЕР-42","citation":"A2"},'
            '{"fact":"Контрольная дата синтетического проекта: 7 октября 2099 года",'
            '"citation":"A1"}]}'
        ),
        (
            '{"claims":['
            '{"fact":"Код синтетического проекта: СЕВЕР-42","citation":"A1"},'
            '{"fact":"Контрольная дата синтетического проекта: 7 октября 2099 года",'
            '"citation":"A1"}]}'
        ),
        (
            '{"claims":['
            '{"fact":"Код синтетического проекта: СЕВЕР-42","citation":"A1"},'
            '{"fact":"Контрольная дата синтетического проекта: 7 октября 2099 года",'
            '"citation":"A2"},{"fact":"Бюджет: 13 млн","citation":"A1"}]}'
        ),
        (
            '{"claims":['
            '{"fact":"Код синтетического проекта: СЕВЕР-42","citation":"A01"},'
            '{"fact":"Контрольная дата синтетического проекта: 7 октября 2099 года",'
            '"citation":"A999"}]}'
        ),
        '{"claims":[],"claims":[]}',
        "<think>private</think>",
    ],
)
async def test_two_source_synthesis_requires_exact_source_fact_map_and_no_extra_claims(
    content: str,
) -> None:
    client = _Client()
    client.syntheses[SYNTHESIS_PROBE.case_id] = replace(
        client.syntheses[SYNTHESIS_PROBE.case_id],
        content=content,
    )

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.SYNTHESIS_INVALID


def test_two_source_synthesis_accepts_the_closed_grounded_prose_variant() -> None:
    model_probe_module._evaluate_synthesis(
        SYNTHESIS_PROBE,
        ProbeCompletion(
            content=(
                "Код синтетического проекта — СЕВЕР-42 [A1], а его контрольная дата — "
                "7 октября 2099 года [A2]."
            ),
            finish_reason="stop",
            tool_calls=(),
            prompt_tokens=256,
        ),
    )


@pytest.mark.parametrize(
    "foreign_marker",
    [
        "[A0]",
        "[A9999]",
        "[A1000000]",
        "[AA1]",
        "[A_1]",
        "[A-1]",
        "[A 1]",
        "[A.1]",
        "[A/1]",
        "[A:1]",
        "[A\u200b1]",
        "[ A1]",
        "[A1 ]",
        "[B#1]",
        "[B1]",
        "[Б1]",
        r"\[B1\]",
        "［B1］",
        "【B1】",
        "[приложение]",
    ],
)
def test_shared_synthesis_contract_rejects_every_citation_like_foreign_marker(
    foreign_marker: str,
) -> None:
    with pytest.raises(ValueError, match="citations do not match"):
        validate_file_synthesis_answer(f"Факт [A1]. Чужая метка {foreign_marker}.", ("A1",))


@pytest.mark.asyncio
async def test_positive_verifier_receives_the_exact_accepted_synthesis() -> None:
    client = _Client()
    accepted = (
        "Код синтетического проекта — СЕВЕР-42 [A1], а его контрольная дата — 7 октября 2099 года [A2]."
    )
    client.syntheses[SYNTHESIS_PROBE.case_id] = replace(
        client.syntheses[SYNTHESIS_PROBE.case_id],
        content=accepted,
    )

    await _run(client)

    request = next(
        item
        for item in client.requests
        if isinstance(item, VerifierProbeRequest)
        and item.case_id == "production_file_synthesis_2_verifier_clear"
    )
    assert json.loads(request.prompt)["answer"] == accepted


def test_probe_verifier_prompt_is_byte_exact_with_the_production_builder() -> None:
    accepted = (
        "Код синтетического проекта: СЕВЕР-42 [A1]. "
        "Контрольная дата синтетического проекта: 7 октября 2099 года [A2]."
    )
    parts = (
        EvidencePart(
            "A1", "probe-note-1.txt", "text/plain", "a" * 64, "Код синтетического проекта: СЕВЕР-42."
        ),
        EvidencePart(
            "A2",
            "probe-note-2.txt",
            "text/plain",
            "b" * 64,
            "Контрольная дата синтетического проекта: 7 октября 2099 года.",
        ),
    )
    bundle = EvidenceBundle(
        parts=parts,
        citations=(CitationBinding("A1", "a" * 64), CitationBinding("A2", "b" * 64)),
        file_evidence_set_sha256="c" * 64,
    )
    turn = replace(
        PLAN_PROBE_CASES[0].turn,
        message="Назови код и контрольную дату проекта одним сообщением.",
    )
    probe = model_probe_module._positive_verifier_request(SYNTHESIS_PROBE, accepted)

    assert probe.prompt == build_file_verifier_messages(turn, bundle, accepted)[1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "content"),
    [
        (
            "production_file_synthesis_2_verifier_clear",
            '{"schema":"friday.v12-file-verifier.v1","supported":false,'
            '"citation_labels":["A1","A2"],"unsupported_claims":1}',
        ),
        (
            "verifier_case_79",
            '{"schema":"friday.v12-file-verifier.v1","supported":true,'
            '"citation_labels":["A1","A2"],"unsupported_claims":0}',
        ),
        (
            "production_file_synthesis_2_verifier_clear",
            '{"schema":"friday.v12-file-verifier.v1","supported":true,'
            '"citation_labels":["A2","A1"],"unsupported_claims":0}',
        ),
        (
            "production_file_synthesis_2_verifier_clear",
            '{"schema":"friday.v12-file-verifier.v1","supported":true,'
            '"citation_labels":["A1","A2"],"unsupported_claims":false}',
        ),
        (
            "production_file_synthesis_2_verifier_clear",
            (
                '{"schema":"friday.v12-file-verifier.v1","supported":true,"supported":false,'
                '"citation_labels":["A1","A2"],"unsupported_claims":0}'
            ),
        ),
        (
            "production_file_synthesis_2_verifier_clear",
            (
                '{"schema":"friday.v12-file-verifier.v1","supported":true,'
                '"citation_labels":["A1","A2"],"unsupported_claims":0,"extra":0}'
            ),
        ),
        (
            "production_file_synthesis_2_verifier_clear",
            '{"supported":true,"citation_labels":["A1","A2"],"unsupported_claims":0}',
        ),
    ],
)
async def test_both_verifier_cases_require_strict_grounding_verdicts(
    case_id: str,
    content: str,
) -> None:
    client = _Client()
    client.verifier[case_id] = replace(client.verifier[case_id], content=content)

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.VERIFIER_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        ProbeCompletion('{"начало":"wrong","конец":"wrong"}', "stop", (), 8_192),
        ProbeCompletion(
            json.dumps({"начало": CONTEXT_PROBE.start_marker, "конец": CONTEXT_PROBE.end_marker}),
            "stop",
            (),
            8_191,
        ),
        ProbeCompletion(
            '{"начало":"CTX-НАЧАЛО-7F31","начало":"forged","конец":"CTX-КОНЕЦ-91D4"}',
            "stop",
            (),
            8_192,
        ),
    ],
)
async def test_context_requires_exact_edges_strict_json_and_measured_8k_usage(
    response: ProbeCompletion,
) -> None:
    client = _Client()
    client.context = response

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.CONTEXT_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        CancellationProbeResult("admission", _CANCELLATION_WITNESS, True, 100),
        CancellationProbeResult("submitted", "b" * 64, True, 100),
        CancellationProbeResult("submitted", _CANCELLATION_WITNESS, False, 100),
        CancellationProbeResult("submitted", _CANCELLATION_WITNESS, True, 5_001),
        CancellationProbeResult("submitted", _CANCELLATION_WITNESS, True, -1),
    ],
)
async def test_cancellation_requires_acceptance_witness_submission_and_bounded_drain(
    result: CancellationProbeResult,
) -> None:
    client = _Client()
    client.cancellation = result

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.CANCELLATION_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loads", "expected"),
    [
        ([ModelLoadSample(1, 0, _EPOCH_SHA256)] * 3, ModelProbeFailure.MODEL_BUSY),
        (
            [
                ModelLoadSample(0, 0, _EPOCH_SHA256),
                ModelLoadSample(0, 0, _OTHER_EPOCH_SHA256),
                ModelLoadSample(0, 0, _OTHER_EPOCH_SHA256),
            ],
            ModelProbeFailure.EPOCH_CHANGED,
        ),
        (
            [
                ModelLoadSample(0, 0, _EPOCH_SHA256),
                ModelLoadSample(0, 0, _EPOCH_SHA256),
                ModelLoadSample(0, 1, _EPOCH_SHA256),
            ],
            ModelProbeFailure.QUEUE_NOT_DRAINED,
        ),
        (
            [ModelLoadSample(float("nan"), 0, _EPOCH_SHA256)] * 3,
            ModelProbeFailure.LOAD_INVALID,
        ),
        ([ModelLoadSample(0, 0, "invalid")] * 3, ModelProbeFailure.LOAD_INVALID),
    ],
)
async def test_queue_must_be_idle_and_process_epoch_stable(
    loads: list[ModelLoadSample],
    expected: ModelProbeFailure,
) -> None:
    client = _Client()
    client.loads = loads

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is expected


@pytest.mark.asyncio
async def test_post_cancellation_queue_must_remain_quiet_across_separated_observations() -> None:
    client = _Client()
    client.loads = [
        ModelLoadSample(0, 0, _EPOCH_SHA256),
        ModelLoadSample(0, 0, _EPOCH_SHA256),
        ModelLoadSample(0, 0, _EPOCH_SHA256),
        ModelLoadSample(1, 0, _EPOCH_SHA256),
    ]

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is ModelProbeFailure.QUEUE_NOT_DRAINED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("load", ModelProbeFailure.LOAD_CALL_FAILED),
        ("plan", ModelProbeFailure.PLAN_CALL_FAILED),
        ("synthesis", ModelProbeFailure.SYNTHESIS_CALL_FAILED),
        ("verifier", ModelProbeFailure.VERIFIER_CALL_FAILED),
        ("context", ModelProbeFailure.CONTEXT_CALL_FAILED),
        ("cancellation", ModelProbeFailure.CANCELLATION_CALL_FAILED),
    ],
)
async def test_client_failures_are_reduced_to_closed_codes_without_private_text(
    phase: str,
    expected: ModelProbeFailure,
) -> None:
    client = _Client()
    client.raise_phase = phase

    with pytest.raises(ModelProbeError) as raised:
        await _run(client)

    assert raised.value.code is expected
    assert "private" not in str(raised.value)
    assert "private" not in repr(raised.value)


@pytest.mark.asyncio
async def test_one_absolute_deadline_cancels_and_drains_a_blocked_client_call() -> None:
    client = _Client()
    client.block_plan = True

    with pytest.raises(ModelProbeError) as raised:
        await _run(client, deadline=time.monotonic() + 0.02)

    assert raised.value.code is ModelProbeFailure.DEADLINE_EXHAUSTED
    assert client.plan_cancelled is True


@pytest.mark.asyncio
async def test_deadline_remains_bounded_when_client_suppresses_the_first_cancellation() -> None:
    client = _Client()
    client.block_plan = True
    client.ignore_first_plan_cancellation = True
    started = time.monotonic()

    with pytest.raises(ModelProbeError) as raised:
        await _run(client, deadline=time.monotonic() + 0.02)

    elapsed = time.monotonic() - started
    assert raised.value.code is ModelProbeFailure.DEADLINE_EXHAUSTED
    assert client.ignored_plan_cancellation.is_set()
    assert elapsed < 0.25

    # Python cannot force-kill a coroutine which swallows CancelledError.  Let
    # this test double finish so the callback-owned result is also exercised.
    client.release_hostile_plan.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_external_cancellation_propagates_and_drains_the_injected_client() -> None:
    client = _Client()
    client.block_plan = True
    task = asyncio.create_task(_run(client, deadline=time.monotonic() + 10))
    await asyncio.wait_for(client.plan_started.wait(), timeout=0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.plan_cancelled is True


@pytest.mark.asyncio
async def test_expired_deadline_and_unregistered_profile_fail_before_client_work() -> None:
    client = _Client()
    with pytest.raises(ModelProbeError) as expired:
        await _run(client, deadline=time.monotonic() - 1)
    assert expired.value.code is ModelProbeFailure.DEADLINE_EXHAUSTED
    assert client.deadlines == []

    with pytest.raises(ModelProbeError) as unregistered:
        await run_v12_live_probe(
            replace(QWEN36_27B_V12_PROFILE),
            client,
            endpoint_binding_sha256=_BINDING,
            absolute_deadline=time.monotonic() + 10,
        )
    assert unregistered.value.code is ModelProbeFailure.PROFILE_REJECTED


@pytest.mark.asyncio
async def test_invalid_endpoint_binding_fails_without_a_probe_call() -> None:
    client = _Client()

    with pytest.raises(ModelProbeError) as raised:
        await run_v12_live_probe(
            QWEN36_27B_V12_PROFILE,
            client,
            endpoint_binding_sha256="not-a-digest",
            absolute_deadline=time.monotonic() + 10,
        )

    assert raised.value.code is ModelProbeFailure.ENDPOINT_BINDING_REJECTED
    assert client.deadlines == []


def test_requests_and_responses_hide_every_prompt_and_model_controlled_string_from_repr() -> None:
    response = ProbeCompletion("PRIVATE RESPONSE", "PRIVATE FINISH", ("PRIVATE TOOL",), 1)
    values = (
        *PLAN_PROBE_CASES,
        SYNTHESIS_PROBE,
        *SYNTHESIS_PROBES,
        *VERIFIER_PROBES,
        CONTEXT_PROBE,
        CANCELLATION_PROBE,
        response,
    )
    rendered = "\n".join(repr(value) for value in values)

    assert "Обобщи приложенный" not in rendered
    assert "СЕВЕР-42" not in rendered
    assert CONTEXT_PROBE.start_marker not in rendered
    assert "натуральные числа" not in rendered
    assert "PRIVATE" not in rendered


def test_probe_module_has_no_environment_file_or_network_implementation() -> None:
    source = inspect.getsource(model_probe_module)

    assert "os.environ" not in source
    assert "pathlib" not in source
    assert "httpx" not in source
    assert "open(" not in source


@pytest.mark.parametrize(
    "mutation",
    ["prompt", "validator", "case_semantics", "timeout", "cancellation_contract"],
)
def test_suite_hash_commits_to_prompts_validators_cases_timeouts_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    baseline = model_probe_module._probe_suite_sha256()
    if mutation == "prompt":
        monkeypatch.setattr(
            model_probe_module,
            "SYNTHESIS_PROBES",
            (
                replace(SYNTHESIS_PROBES[0], prompt=f"{SYNTHESIS_PROBES[0].prompt} changed"),
                *SYNTHESIS_PROBES[1:],
            ),
        )
    elif mutation == "validator":
        monkeypatch.setattr(
            model_probe_module,
            "_SYNTHESIS_EXPECTED_PATTERNS",
            {**model_probe_module._SYNTHESIS_EXPECTED_PATTERNS, SYNTHESIS_PROBE.case_id: "changed"},
        )
    elif mutation == "case_semantics":
        monkeypatch.setattr(
            model_probe_module,
            "PLAN_PROBE_CASES",
            (
                replace(PLAN_PROBE_CASES[0], expected_route=RouteClass.ORDINARY_DIALOGUE),
                *PLAN_PROBE_CASES[1:],
            ),
        )
    elif mutation == "timeout":
        monkeypatch.setattr(
            model_probe_module,
            "SYNTHESIS_TIMEOUT_SEC",
            model_probe_module.SYNTHESIS_TIMEOUT_SEC + 1,
        )
    else:
        monkeypatch.setattr(
            model_probe_module,
            "POST_CANCELLATION_QUIET_OBSERVATIONS",
            model_probe_module.POST_CANCELLATION_QUIET_OBSERVATIONS + 1,
        )

    assert model_probe_module._probe_suite_sha256() != baseline


def test_synthetic_file_cases_use_only_bounded_descriptors() -> None:
    file_cases = [case for case in PLAN_PROBE_CASES if case.expected_route is RouteClass.FILE_READ]

    assert len(file_cases) == 3
    assert [len(case.turn.attachments) for case in file_cases[:2]] == [1, 2]
    assert all(
        attachment.media_type == "text/plain" and attachment.extracted_text_available
        for case in file_cases[:2]
        for attachment in case.turn.attachments
    )
    assert all(case.turn.attachments for case in file_cases)
    assert all(
        request.kind is EvidenceKind.ATTACHED_FILES
        for case in file_cases
        for request in _parsed_evidence(case)
    )


def _parsed_evidence(case: PlanProbeCase):  # noqa: ANN201
    from friday.orchestration.contracts import TurnPlan

    return TurnPlan.parse(_plan_payload(case)).evidence_requests
