"""Bounded, synthetic-only live attestation for the V12 model profile.

This module owns evaluation, not transport.  The caller supplies a
:class:`V12ModelProbeClient`; no socket, environment variable, file or credential
is read here.  Every request is a fixed synthetic case, every model-controlled
string is hidden from repr, and success returns only the sanitized
:class:`~friday.model_profiles.V12LiveAttestation` capability token.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from typing import Any, Protocol, TypeVar

from friday.model_profiles import (
    ModelCapability,
    ModelEffect,
    V12LiveAttestation,
    V12ModelProfileSpec,
    v12_model_profile_for,
)
from friday.orchestration.contracts import (
    AttachmentDescriptor,
    OutputFormat,
    RouteClass,
    TurnInput,
    TurnPlan,
)
from friday.orchestration.file_read_contract import (
    V12_FILE_SYNTHESIS_SYSTEM,
    V12_FILE_VERIFIER_SCHEMA,
    V12_FILE_VERIFIER_SYSTEM,
    file_read_plan_supports_attachment_count,
    parse_file_verifier_result,
    validate_file_synthesis_answer,
)

PLAN_CASE_TIMEOUT_SEC = 12.0
SYNTHESIS_TIMEOUT_SEC = 60.0
VERIFIER_TIMEOUT_SEC = 30.0
CONTEXT_TIMEOUT_SEC = 60.0
LOAD_TIMEOUT_SEC = 2.0
CANCELLATION_TIMEOUT_SEC = 6.0
REMOTE_QUEUE_DRAIN_MAX_MS = 5_000
POST_CANCELLATION_QUIET_OBSERVATIONS = 2
POST_CANCELLATION_QUIET_INTERVAL_SEC = 0.05
TASK_CANCELLATION_DRAIN_SEC = 0.05
MAX_COMPLETION_CHARS = 65_536

_SHA256 = re.compile(r"[0-9a-f]{64}")
_T = TypeVar("_T")


class ModelProbeFailure(StrEnum):
    PROFILE_REJECTED = "profile_rejected"
    ENDPOINT_BINDING_REJECTED = "endpoint_binding_rejected"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    LOAD_CALL_FAILED = "load_call_failed"
    LOAD_INVALID = "load_invalid"
    MODEL_BUSY = "model_busy"
    EPOCH_CHANGED = "epoch_changed"
    PLAN_CALL_FAILED = "plan_call_failed"
    PLAN_INVALID = "plan_invalid"
    SYNTHESIS_CALL_FAILED = "synthesis_call_failed"
    SYNTHESIS_INVALID = "synthesis_invalid"
    VERIFIER_CALL_FAILED = "verifier_call_failed"
    VERIFIER_INVALID = "verifier_invalid"
    CONTEXT_CALL_FAILED = "context_call_failed"
    CONTEXT_INVALID = "context_invalid"
    CANCELLATION_CALL_FAILED = "cancellation_call_failed"
    CANCELLATION_INVALID = "cancellation_invalid"
    QUEUE_NOT_DRAINED = "queue_not_drained"
    ATTESTATION_REJECTED = "attestation_rejected"


class ModelProbeError(RuntimeError):
    """A closed failure code which cannot echo model or transport content."""

    def __init__(self, code: ModelProbeFailure) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ModelLoadSample:
    running: float
    waiting: float
    process_epoch_sha256: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProbeCompletion:
    """Minimal OpenAI-compatible response projection; raw fields stay private."""

    content: str = field(repr=False)
    finish_reason: str = field(repr=False)
    tool_calls: tuple[str, ...] = field(repr=False)
    prompt_tokens: int


@dataclass(frozen=True, slots=True)
class CancellationProbeResult:
    phase: str = field(repr=False)
    accepted_request_witness_sha256: str = field(repr=False)
    local_task_drained: bool
    remote_queue_drain_ms: int


@dataclass(frozen=True, slots=True)
class PlanProbeCase:
    case_id: str
    turn: TurnInput = field(repr=False)
    expected_route: RouteClass
    expected_output_format: OutputFormat | None = None
    expected_language: str = "ru"


@dataclass(frozen=True, slots=True)
class SynthesisProbeRequest:
    case_id: str
    prompt: str = field(repr=False)
    system_prompt: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerifierProbeRequest:
    case_id: str
    prompt: str = field(repr=False)
    system_prompt: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ContextProbeRequest:
    case_id: str
    prompt: str = field(repr=False)
    start_marker: str = field(repr=False)
    end_marker: str = field(repr=False)
    minimum_prompt_tokens: int


@dataclass(frozen=True, slots=True)
class CancellationProbeRequest:
    case_id: str
    prompt: str = field(repr=False)
    cancel_after_ms: int
    queue_drain_timeout_ms: int


class V12ModelProbeClient(Protocol):
    """Injected transport seam used by production adapters and offline tests."""

    async def sample_load(self, *, absolute_deadline: float) -> ModelLoadSample: ...

    async def complete_plan(
        self,
        case: PlanProbeCase,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion: ...

    async def complete_synthesis(
        self,
        request: SynthesisProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion: ...

    async def complete_verifier(
        self,
        request: VerifierProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion: ...

    async def complete_context(
        self,
        request: ContextProbeRequest,
        *,
        absolute_deadline: float,
    ) -> ProbeCompletion: ...

    async def cancel_and_drain(
        self,
        request: CancellationProbeRequest,
        *,
        absolute_deadline: float,
    ) -> CancellationProbeResult: ...


def _attachment(
    ordinal: int,
    name: str,
    media_type: str,
    *,
    extracted: bool,
) -> AttachmentDescriptor:
    return AttachmentDescriptor(
        ordinal=ordinal,
        name=name,
        media_type=media_type,
        size_bytes=1_024,
        extracted_text_available=extracted,
    )


def _turn(message: str, *attachments: AttachmentDescriptor) -> TurnInput:
    return TurnInput(
        message=message,
        message_truncated=False,
        reply_quote="",
        reply_quote_truncated=False,
        conversation_present=False,
        conversation_mode="dialogue",
        enable_tools=True,
        attachments=tuple(attachments),
        attachments_truncated=False,
        synthetic_document_notice=False,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
        actor_is_owner=True,
        shared_archive=False,
    )


_PDF = _attachment(1, "probe-act.pdf", "application/pdf", extracted=True)
_JPG_1 = _attachment(1, "probe-scan-1.jpg", "image/jpeg", extracted=False)
_TXT_1 = _attachment(1, "probe-note-1.txt", "text/plain", extracted=True)
_TXT_2 = _attachment(2, "probe-note-2.txt", "text/plain", extracted=True)

PLAN_PROBE_CASES: tuple[PlanProbeCase, ...] = (
    PlanProbeCase(
        "file_summary",
        _turn("Обобщи приложенный текстовый документ и укажи ключевые факты.", _TXT_1),
        RouteClass.FILE_READ,
        OutputFormat.TEXT,
    ),
    PlanProbeCase(
        "file_compare",
        _turn("Сравни эти два текстовых документа и ответь одним сообщением.", _TXT_1, _TXT_2),
        RouteClass.FILE_READ,
        OutputFormat.TEXT,
    ),
    PlanProbeCase(
        "file_ocr",
        _turn("Распознай текст на приложенном скане.", _JPG_1),
        RouteClass.FILE_READ,
        OutputFormat.TEXT,
    ),
    PlanProbeCase(
        "effect_document",
        _turn("Создай DOCX по приложенному акту.", _PDF),
        RouteClass.EFFECT,
        OutputFormat.DOCUMENT,
    ),
    PlanProbeCase(
        "effect_reminder",
        _turn("Напомни завтра проверить приложенный акт.", _PDF),
        RouteClass.EFFECT,
        OutputFormat.TEXT,
    ),
    PlanProbeCase(
        "archive_date",
        _turn("Покажи документы, которые я прислал вчера."),
        RouteClass.ARCHIVE_READ,
        OutputFormat.TEXT,
    ),
    PlanProbeCase(
        "web_current",
        _turn("Найди актуальный официальный курс синтетической валюты Альфа."),
        RouteClass.WEB_READ,
        OutputFormat.TEXT,
    ),
    PlanProbeCase(
        "small_talk",
        _turn("Привет! Как дела?"),
        RouteClass.SMALL_TALK,
        OutputFormat.TEXT,
    ),
    PlanProbeCase(
        "ordinary_dialogue",
        _turn("Объясни простыми словами, чем таблица отличается от списка."),
        RouteClass.ORDINARY_DIALOGUE,
        OutputFormat.TEXT,
    ),
)

_SYNTHESIS_INPUT = {
    "schema": "friday.v12-file-synthesis.v1",
    "request": "Назови код и контрольную дату проекта одним сообщением.",
    "objective": "Сообщить два точных факта из двух приложенных источников.",
    "output": {
        "format": "text",
        "language": "ru",
        "one_message": True,
        "require_citations": True,
    },
    "evidence": {
        "schema": "friday.evidence-bundle.v1",
        "parts": [
            {
                "display_name": "probe-note-1.txt",
                "label": "A1",
                "media_type": "text/plain",
                "text": "Код синтетического проекта: СЕВЕР-42.",
            },
            {
                "display_name": "probe-note-2.txt",
                "label": "A2",
                "media_type": "text/plain",
                "text": "Контрольная дата синтетического проекта: 7 октября 2099 года.",
            },
        ],
    },
}
SYNTHESIS_PROBE = SynthesisProbeRequest(
    case_id="production_file_synthesis_2",
    system_prompt=V12_FILE_SYNTHESIS_SYSTEM,
    prompt=json.dumps(
        _SYNTHESIS_INPUT,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ),
)
_SYNTHESIS_EXPECTED = (
    "Код синтетического проекта: СЕВЕР-42 [A1]. "
    "Контрольная дата синтетического проекта: 7 октября 2099 года [A2]."
)

VERIFIER_PROBES: tuple[VerifierProbeRequest, ...] = (
    VerifierProbeRequest(
        case_id="verifier_case_31",
        system_prompt=V12_FILE_VERIFIER_SYSTEM,
        prompt=(
            '{"answer":"Код синтетического проекта: СЕВЕР-42 [A1]. Контрольная дата '
            'синтетического проекта: 7 октября 2099 года [A2].","evidence":'
            + json.dumps(_SYNTHESIS_INPUT["evidence"], ensure_ascii=False, separators=(",", ":"))
            + ',"request":"Назови код и контрольную дату проекта одним сообщением.",'
            '"schema":"friday.v12-file-verification-input.v1"}'
        ),
    ),
    VerifierProbeRequest(
        case_id="verifier_case_79",
        system_prompt=V12_FILE_VERIFIER_SYSTEM,
        prompt=(
            '{"answer":"Код синтетического проекта: СЕВЕР-42 [A1]. Контрольная дата '
            "синтетического проекта: 7 октября 2099 года [A2]. Бюджет проекта — "
            '13 миллионов рублей [A1].","evidence":'
            + json.dumps(_SYNTHESIS_INPUT["evidence"], ensure_ascii=False, separators=(",", ":"))
            + ',"request":"Назови код, контрольную дату и бюджет проекта.",'
            '"schema":"friday.v12-file-verification-input.v1"}'
        ),
    ),
)
_VERIFIER_EXPECTED: Mapping[str, Mapping[str, object]] = {
    "verifier_case_31": {
        "schema": V12_FILE_VERIFIER_SCHEMA,
        "supported": True,
        "citation_labels": ["A1", "A2"],
        "unsupported_claims": 0,
    },
    "verifier_case_79": {
        "schema": V12_FILE_VERIFIER_SCHEMA,
        "supported": False,
        "citation_labels": ["A1", "A2"],
        "unsupported_claims": 1,
    },
}

_CONTEXT_START = "CTX-НАЧАЛО-7F31"
_CONTEXT_END = "CTX-КОНЕЦ-91D4"
_CONTEXT_FILLER = " ".join(f"unit-{index:05d}" for index in range(7_000))
CONTEXT_PROBE = ContextProbeRequest(
    case_id="context_8k_edges",
    prompt=(
        f"Запомни крайние маркеры. Начальный маркер: {_CONTEXT_START}.\n"
        f"Синтетическое наполнение: {_CONTEXT_FILLER}\n"
        f"Конечный маркер: {_CONTEXT_END}.\n"
        "Верни строго JSON с ключами начало и конец и точными маркерами."
    ),
    start_marker=_CONTEXT_START,
    end_marker=_CONTEXT_END,
    minimum_prompt_tokens=8_192,
)

CANCELLATION_PROBE = CancellationProbeRequest(
    case_id="submitted_cancellation",
    prompt=(
        "Это синтетическая проверка отмены. Перечисляй натуральные числа по одному "
        "до десяти тысяч без сокращений."
    ),
    cancel_after_ms=250,
    queue_drain_timeout_ms=REMOTE_QUEUE_DRAIN_MAX_MS,
)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _attachment_manifest(value: AttachmentDescriptor) -> Mapping[str, object]:
    return {
        "ordinal": value.ordinal,
        "name": value.name,
        "media_type": value.media_type,
        "size_bytes": value.size_bytes,
        "extracted_text_available": value.extracted_text_available,
    }


def _turn_manifest(value: TurnInput) -> Mapping[str, object]:
    return {
        "message": value.message,
        "message_truncated": value.message_truncated,
        "reply_quote": value.reply_quote,
        "reply_quote_truncated": value.reply_quote_truncated,
        "conversation_present": value.conversation_present,
        "conversation_mode": value.conversation_mode,
        "enable_tools": value.enable_tools,
        "attachments": [_attachment_manifest(item) for item in value.attachments],
        "attachments_truncated": value.attachments_truncated,
        "synthetic_document_notice": value.synthetic_document_notice,
        "quoted_attachment_reference": value.quoted_attachment_reference,
        "reply_assistant_reference": value.reply_assistant_reference,
        "actor_is_owner": value.actor_is_owner,
        "shared_archive": value.shared_archive,
    }


def _cancellation_request_witness_sha256(request: CancellationProbeRequest) -> str:
    """Bind a transport acceptance witness to the exact synthetic request.

    A production adapter may return this digest only after its transport has
    observed server-side acceptance of this exact request.  The digest is a
    binding value, not permission for an adapter to self-attest acceptance.
    """

    return _canonical_sha256(
        {
            "schema": "friday.v12-cancellation-acceptance-witness.v1",
            "case_id": request.case_id,
            "prompt": request.prompt,
            "cancel_after_ms": request.cancel_after_ms,
            "queue_drain_timeout_ms": request.queue_drain_timeout_ms,
        }
    )


def _probe_suite_manifest() -> Mapping[str, object]:
    """Return the complete, code-owned semantics covered by the suite hash."""

    return {
        "schema": "friday.qwen36-27b-v12-probe-suite.v3",
        "completion_contract": {
            "max_chars": MAX_COMPLETION_CHARS,
            "finish_reason": "stop",
            "tool_calls": [],
            "utf8": "strict",
        },
        "timeouts_sec": {
            "plan_case": PLAN_CASE_TIMEOUT_SEC,
            "synthesis": SYNTHESIS_TIMEOUT_SEC,
            "verifier": VERIFIER_TIMEOUT_SEC,
            "context": CONTEXT_TIMEOUT_SEC,
            "load": LOAD_TIMEOUT_SEC,
            "cancellation": CANCELLATION_TIMEOUT_SEC,
            "task_cancellation_drain": TASK_CANCELLATION_DRAIN_SEC,
        },
        "plan": {
            "validator": {
                "version": "turn-plan-production-applicability-native-text.v3",
                "parser": "friday.turn-plan.v1",
                "reject_duplicate_keys": True,
            },
            "cases": [
                {
                    "case_id": case.case_id,
                    "turn": _turn_manifest(case.turn),
                    "expected_route": case.expected_route.value,
                    "expected_output_format": (
                        case.expected_output_format.value if case.expected_output_format is not None else None
                    ),
                    "expected_language": case.expected_language,
                }
                for case in PLAN_PROBE_CASES
            ],
        },
        "synthesis": {
            "request": {
                "case_id": SYNTHESIS_PROBE.case_id,
                "system_prompt": SYNTHESIS_PROBE.system_prompt,
                "prompt": SYNTHESIS_PROBE.prompt,
            },
            "validator": {
                "version": "production-file-prose-exact-citations.v3",
                "expected": _SYNTHESIS_EXPECTED,
                "reject_extra_claims": True,
                "reject_invalid_citation_markers": True,
            },
        },
        "verifier": {
            "validator": {
                "version": "production-file-verifier-schema-positive-negative.v2",
                "exact_keys": [
                    "citation_labels",
                    "schema",
                    "supported",
                    "unsupported_claims",
                ],
                "reject_duplicate_keys": True,
            },
            "cases": [
                {
                    "case_id": request.case_id,
                    "system_prompt": request.system_prompt,
                    "prompt": request.prompt,
                    "expected": _VERIFIER_EXPECTED[request.case_id],
                }
                for request in VERIFIER_PROBES
            ],
        },
        "context": {
            "request": {
                "case_id": CONTEXT_PROBE.case_id,
                "prompt": CONTEXT_PROBE.prompt,
                "start_marker": CONTEXT_PROBE.start_marker,
                "end_marker": CONTEXT_PROBE.end_marker,
                "minimum_prompt_tokens": CONTEXT_PROBE.minimum_prompt_tokens,
            },
            "validator": "strict-json-exact-edges-measured-prompt-tokens.v1",
        },
        "cancellation": {
            "request": {
                "case_id": CANCELLATION_PROBE.case_id,
                "prompt": CANCELLATION_PROBE.prompt,
                "cancel_after_ms": CANCELLATION_PROBE.cancel_after_ms,
                "queue_drain_timeout_ms": CANCELLATION_PROBE.queue_drain_timeout_ms,
                "acceptance_witness_sha256": _cancellation_request_witness_sha256(CANCELLATION_PROBE),
            },
            "validator": {
                "version": "accepted-submitted-local-drain-remote-stable-quiet.v2",
                "accepted_phase": "submitted",
                "remote_queue_drain_max_ms": REMOTE_QUEUE_DRAIN_MAX_MS,
                "quiet_observations": POST_CANCELLATION_QUIET_OBSERVATIONS,
                "quiet_interval_sec": POST_CANCELLATION_QUIET_INTERVAL_SEC,
                "same_process_epoch": True,
            },
        },
        "load_validator": "finite-nonnegative-idle-same-positive-epoch.v1",
        "attested_limits": {
            "context_tokens": 8_192,
            "prepared_evidence_items": 2,
            "tool_steps": 0,
            "effects": [ModelEffect.READ.value],
            "verifier_required_after_all_cases": True,
        },
    }


def _probe_suite_sha256() -> str:
    return _canonical_sha256(_probe_suite_manifest())


def _validate_profile(profile: object, endpoint_binding_sha256: object) -> V12ModelProfileSpec:
    if (
        not isinstance(profile, V12ModelProfileSpec)
        or v12_model_profile_for(
            profile.runtime_profile_name,
            profile.served_model_alias,
        )
        is not profile
    ):
        raise ModelProbeError(ModelProbeFailure.PROFILE_REJECTED)
    expected_capabilities = frozenset(
        {
            ModelCapability.TURN_PLAN_V1,
            ModelCapability.RU_PLANNING,
            ModelCapability.PREPARED_EVIDENCE_2,
            ModelCapability.CONTEXT_8K,
            ModelCapability.REMOTE_CANCELLATION,
        }
    )
    if not (
        profile.probe_suite_sha256 == _probe_suite_sha256()
        and profile.required_capabilities == expected_capabilities
        and profile.allowed_capabilities == expected_capabilities
        and profile.minimum_context_tokens == profile.max_context_tokens == 8_192
        and profile.max_prepared_evidence_items == 2
        and profile.max_tool_steps == 0
        and profile.allowed_effects == frozenset({ModelEffect.READ})
        and profile.verifier_required
    ):
        raise ModelProbeError(ModelProbeFailure.PROFILE_REJECTED)
    if not isinstance(endpoint_binding_sha256, str) or _SHA256.fullmatch(endpoint_binding_sha256) is None:
        raise ModelProbeError(ModelProbeFailure.ENDPOINT_BINDING_REJECTED)
    return profile


def _remaining(deadline: float, ceiling: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(deadline) or remaining <= 0.0:
        raise ModelProbeError(ModelProbeFailure.DEADLINE_EXHAUSTED)
    return min(remaining, ceiling)


async def _bounded_call(
    operation: Callable[[], Awaitable[_T]],
    *,
    deadline: float,
    ceiling: float,
    failure: ModelProbeFailure,
) -> _T:
    timeout = _remaining(deadline, ceiling)
    try:
        owned = asyncio.ensure_future(operation())
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ModelProbeError(failure) from None
    try:
        done, _ = await asyncio.wait((owned,), timeout=timeout)
    except asyncio.CancelledError:
        await _cancel_and_boundedly_drain(owned)
        raise
    if not done:
        await _cancel_and_boundedly_drain(owned)
        raise ModelProbeError(ModelProbeFailure.DEADLINE_EXHAUSTED) from None
    try:
        return owned.result()
    except asyncio.CancelledError:
        raise ModelProbeError(failure) from None
    except ModelProbeError:
        raise
    except Exception:
        raise ModelProbeError(failure) from None


def _consume_abandoned_result(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    with suppress(asyncio.CancelledError, Exception):
        task.exception()


async def _cancel_and_boundedly_drain(task: asyncio.Future[Any]) -> None:
    """Cancel an owned async operation without letting it extend the probe forever.

    Cooperative clients drain here.  A client coroutine which suppresses
    ``CancelledError`` cannot be force-killed by Python; in that case ownership
    is retained through a result-consuming callback and the probe fails within
    a fixed cleanup grace instead of awaiting the hostile coroutine forever.
    CPU-bound code that never yields can still block its event loop and must be
    isolated by the production transport adapter.
    """

    task.cancel()
    try:
        await asyncio.wait((task,), timeout=TASK_CANCELLATION_DRAIN_SEC)
    finally:
        if not task.done():
            task.add_done_callback(_consume_abandoned_result)


def _completion_content(response: object, *, failure: ModelProbeFailure) -> str:
    if not isinstance(response, ProbeCompletion):
        raise ModelProbeError(failure)
    content = response.content
    if (
        not isinstance(content, str)
        or not content
        or len(content) > MAX_COMPLETION_CHARS
        or response.finish_reason != "stop"
        or not isinstance(response.tool_calls, tuple)
        or response.tool_calls
    ):
        raise ModelProbeError(failure)
    try:
        content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ModelProbeError(failure) from None
    return content


def _evaluate_plan(case: PlanProbeCase, response: object) -> None:
    content = _completion_content(response, failure=ModelProbeFailure.PLAN_INVALID)
    try:
        plan = TurnPlan.parse(content)
    except Exception:
        raise ModelProbeError(ModelProbeFailure.PLAN_INVALID) from None
    if not (
        plan.route is case.expected_route
        and plan.output.language == case.expected_language
        and not plan.tool_intents
        and (case.expected_output_format is None or plan.output.format is case.expected_output_format)
        and (
            plan.route is not RouteClass.FILE_READ
            or file_read_plan_supports_attachment_count(plan, len(case.turn.attachments))
        )
    ):
        raise ModelProbeError(ModelProbeFailure.PLAN_INVALID)


def _evaluate_synthesis(response: object) -> None:
    content = _completion_content(response, failure=ModelProbeFailure.SYNTHESIS_INVALID)
    try:
        normalized = validate_file_synthesis_answer(content, ("A1", "A2"))
    except ValueError:
        raise ModelProbeError(ModelProbeFailure.SYNTHESIS_INVALID) from None
    if normalized != _SYNTHESIS_EXPECTED:
        raise ModelProbeError(ModelProbeFailure.SYNTHESIS_INVALID)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(_constant: str) -> None:
    raise ValueError("non-finite JSON number")


def _strict_json_object(content: str, *, failure: ModelProbeFailure) -> dict[str, Any]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError):
        raise ModelProbeError(failure) from None
    if not isinstance(value, dict):
        raise ModelProbeError(failure)
    return value


def _evaluate_verifier(case: VerifierProbeRequest, response: object) -> None:
    content = _completion_content(response, failure=ModelProbeFailure.VERIFIER_INVALID)
    try:
        value = parse_file_verifier_result(content)
    except ValueError:
        raise ModelProbeError(ModelProbeFailure.VERIFIER_INVALID) from None
    if value != _VERIFIER_EXPECTED.get(case.case_id):
        raise ModelProbeError(ModelProbeFailure.VERIFIER_INVALID)


def _evaluate_context(response: object) -> None:
    content = _completion_content(response, failure=ModelProbeFailure.CONTEXT_INVALID)
    assert isinstance(response, ProbeCompletion)
    if (
        not isinstance(response.prompt_tokens, int)
        or isinstance(response.prompt_tokens, bool)
        or response.prompt_tokens < CONTEXT_PROBE.minimum_prompt_tokens
    ):
        raise ModelProbeError(ModelProbeFailure.CONTEXT_INVALID)
    value = _strict_json_object(content, failure=ModelProbeFailure.CONTEXT_INVALID)
    if value != {"начало": CONTEXT_PROBE.start_marker, "конец": CONTEXT_PROBE.end_marker}:
        raise ModelProbeError(ModelProbeFailure.CONTEXT_INVALID)


def _load_sample(value: object) -> ModelLoadSample:
    if not isinstance(value, ModelLoadSample):
        raise ModelProbeError(ModelProbeFailure.LOAD_INVALID)
    numbers = (value.running, value.waiting)
    if (
        any(
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or not math.isfinite(float(number))
            or float(number) < 0.0
            for number in numbers
        )
        or not isinstance(value.process_epoch_sha256, str)
        or _SHA256.fullmatch(value.process_epoch_sha256) is None
    ):
        raise ModelProbeError(ModelProbeFailure.LOAD_INVALID)
    return value


def _require_idle(sample: ModelLoadSample, *, post_cancellation: bool = False) -> None:
    if float(sample.running) != 0.0 or float(sample.waiting) != 0.0:
        code = ModelProbeFailure.QUEUE_NOT_DRAINED if post_cancellation else ModelProbeFailure.MODEL_BUSY
        raise ModelProbeError(code)


def _require_same_epoch(expected_sha256: str, observed: ModelLoadSample) -> None:
    if observed.process_epoch_sha256 != expected_sha256:
        raise ModelProbeError(ModelProbeFailure.EPOCH_CHANGED)


def _evaluate_cancellation(request: CancellationProbeRequest, value: object) -> None:
    if not isinstance(value, CancellationProbeResult) or not (
        value.phase == "submitted"
        and isinstance(value.accepted_request_witness_sha256, str)
        and value.accepted_request_witness_sha256 == _cancellation_request_witness_sha256(request)
        and value.local_task_drained is True
        and isinstance(value.remote_queue_drain_ms, int)
        and not isinstance(value.remote_queue_drain_ms, bool)
        and 0 <= value.remote_queue_drain_ms <= REMOTE_QUEUE_DRAIN_MAX_MS
    ):
        raise ModelProbeError(ModelProbeFailure.CANCELLATION_INVALID)


async def _quiet_observation_interval(*, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if not math.isfinite(deadline) or remaining < POST_CANCELLATION_QUIET_INTERVAL_SEC:
        raise ModelProbeError(ModelProbeFailure.DEADLINE_EXHAUSTED)
    await asyncio.sleep(POST_CANCELLATION_QUIET_INTERVAL_SEC)


async def run_v12_live_probe(
    profile: V12ModelProfileSpec,
    client: V12ModelProbeClient,
    *,
    endpoint_binding_sha256: str,
    absolute_deadline: float,
) -> V12LiveAttestation:
    """Run the all-or-nothing synthetic suite under one absolute deadline."""

    profile = _validate_profile(profile, endpoint_binding_sha256)
    _remaining(absolute_deadline, LOAD_TIMEOUT_SEC)

    first = _load_sample(
        await _bounded_call(
            lambda: client.sample_load(absolute_deadline=absolute_deadline),
            deadline=absolute_deadline,
            ceiling=LOAD_TIMEOUT_SEC,
            failure=ModelProbeFailure.LOAD_CALL_FAILED,
        )
    )
    _require_idle(first)

    for case in PLAN_PROBE_CASES:
        response = await _bounded_call(
            partial(client.complete_plan, case, absolute_deadline=absolute_deadline),
            deadline=absolute_deadline,
            ceiling=PLAN_CASE_TIMEOUT_SEC,
            failure=ModelProbeFailure.PLAN_CALL_FAILED,
        )
        _evaluate_plan(case, response)

    synthesis = await _bounded_call(
        lambda: client.complete_synthesis(SYNTHESIS_PROBE, absolute_deadline=absolute_deadline),
        deadline=absolute_deadline,
        ceiling=SYNTHESIS_TIMEOUT_SEC,
        failure=ModelProbeFailure.SYNTHESIS_CALL_FAILED,
    )
    _evaluate_synthesis(synthesis)

    verifier_probes_clear = False
    for request in VERIFIER_PROBES:
        verification = await _bounded_call(
            partial(client.complete_verifier, request, absolute_deadline=absolute_deadline),
            deadline=absolute_deadline,
            ceiling=VERIFIER_TIMEOUT_SEC,
            failure=ModelProbeFailure.VERIFIER_CALL_FAILED,
        )
        _evaluate_verifier(request, verification)
    verifier_probes_clear = True

    context = await _bounded_call(
        lambda: client.complete_context(CONTEXT_PROBE, absolute_deadline=absolute_deadline),
        deadline=absolute_deadline,
        ceiling=CONTEXT_TIMEOUT_SEC,
        failure=ModelProbeFailure.CONTEXT_CALL_FAILED,
    )
    _evaluate_context(context)

    before_cancel = _load_sample(
        await _bounded_call(
            lambda: client.sample_load(absolute_deadline=absolute_deadline),
            deadline=absolute_deadline,
            ceiling=LOAD_TIMEOUT_SEC,
            failure=ModelProbeFailure.LOAD_CALL_FAILED,
        )
    )
    _require_same_epoch(first.process_epoch_sha256, before_cancel)
    _require_idle(before_cancel)

    cancellation = await _bounded_call(
        lambda: client.cancel_and_drain(CANCELLATION_PROBE, absolute_deadline=absolute_deadline),
        deadline=absolute_deadline,
        ceiling=CANCELLATION_TIMEOUT_SEC,
        failure=ModelProbeFailure.CANCELLATION_CALL_FAILED,
    )
    _evaluate_cancellation(CANCELLATION_PROBE, cancellation)

    for observation in range(POST_CANCELLATION_QUIET_OBSERVATIONS):
        if observation:
            await _quiet_observation_interval(deadline=absolute_deadline)
        after_cancel = _load_sample(
            await _bounded_call(
                lambda: client.sample_load(absolute_deadline=absolute_deadline),
                deadline=absolute_deadline,
                ceiling=LOAD_TIMEOUT_SEC,
                failure=ModelProbeFailure.LOAD_CALL_FAILED,
            )
        )
        _require_same_epoch(first.process_epoch_sha256, after_cancel)
        _require_idle(after_cancel, post_cancellation=True)

    try:
        return V12LiveAttestation(
            profile_id=profile.profile_id,
            planner_contract_sha256=profile.planner_contract_sha256,
            probe_suite_sha256=profile.probe_suite_sha256,
            endpoint_binding_sha256=endpoint_binding_sha256,
            process_epoch_sha256=first.process_epoch_sha256,
            capabilities=profile.required_capabilities,
            verified_context_tokens=profile.minimum_context_tokens,
            max_prepared_evidence_items=profile.max_prepared_evidence_items,
            max_tool_steps=profile.max_tool_steps,
            allowed_effects=profile.allowed_effects,
            verifier_required=profile.verifier_required and verifier_probes_clear,
        )
    except Exception:
        raise ModelProbeError(ModelProbeFailure.ATTESTATION_REJECTED) from None


__all__ = [
    "CANCELLATION_PROBE",
    "CONTEXT_PROBE",
    "CancellationProbeRequest",
    "CancellationProbeResult",
    "ContextProbeRequest",
    "ModelLoadSample",
    "ModelProbeError",
    "ModelProbeFailure",
    "PLAN_PROBE_CASES",
    "PlanProbeCase",
    "ProbeCompletion",
    "SYNTHESIS_PROBE",
    "SynthesisProbeRequest",
    "VERIFIER_PROBES",
    "VerifierProbeRequest",
    "V12ModelProbeClient",
    "run_v12_live_probe",
]
