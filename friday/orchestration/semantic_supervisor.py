"""Optional GPT-OSS semantic supervisor.  P1 is shadow-only and never owns a turn."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, TypeVar
from urllib.parse import unquote, unquote_plus, urlsplit

from friday.interaction_control_plane import (
    archive_candidate_cancel_requested,
    parse_archive_candidate_ordinal,
)
from friday.model_input_hygiene import secondary_model_messages_are_secret_free
from friday.orchestration.capability_manifest import bounded_capability_manifest
from friday.orchestration.contracts import TurnInput
from friday.orchestration.policy_kernel import (
    PolicyAdmissionContext,
    PolicyDecision,
    admit_supervisor_proposal,
)
from friday.orchestration.supervisor_contracts import (
    ARCHIVE_SEARCH_ID,
    FILE_CURRENT_READ_ID,
    PRIMARY_SYNTHESIS_ID,
    SUPERVISOR_PRODUCT_POLICY_ID,
    SUPERVISOR_PRODUCT_POLICY_SHA256,
    SUPERVISOR_PROPOSAL_SCHEMA,
    WEB_SEARCH_CURRENT_ID,
    ContinuationDecision,
    ContinuationState,
    SupervisorBudgets,
    SupervisorContinuation,
    SupervisorContractError,
    SupervisorInput,
    SupervisorMode,
    SupervisorProposal,
    SupervisorTurnProjection,
    TaskClass,
    canonical_dumps,
    supervisor_proposal_json_schema,
)
from friday.orchestration.supervisor_observation import (
    SupervisorObservation,
    SupervisorSkipReason,
    parsed_observation,
    skipped_observation,
)
from friday.secondary_brain import (
    EffectClass,
    ModelModality,
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryBrainScheduler,
    SecondaryFailure,
    SecondaryResult,
)
from friday.secondary_brain.contracts import SECONDARY_CONTEXT_TOKEN_RESERVE
from friday.semantic_supervisor_policy import SUPERVISOR_RUNTIME_PROFILE_ID, admitted_supervisor_tasks

T = TypeVar("T")

_SUPERVISOR_SYSTEM_PROMPT = """\
Return one JSON object and no prose. Copy every literal and array shape from
response_template. Change only goal, purpose, and archive/web query_intent to short
natural-language strings grounded in untrusted_turn. Advisory only: never add keys,
steps, capabilities, commands, paths, IDs, tools, effects, review, or publication.
"""
_SMALL_TALK = ("привет", "здравствуй", "добрый", "hello", "hi", "hey", "спасибо", "ок", "ok")
_WEB_CUES = ("интернет", "web", "публичн", "актуальн", "нынешн", "в сети", "current public")
_COMPARE_CUES = ("сравни", "отлич", "разниц", "versus", " vs ", "сопостав")
_ARCHIVE_CUES = ("архив", "в базе", "найди документ", "в знаниях")
_HTTP_URL_TOKEN = re.compile(r"\bhttps?://[^\s<>\"'{}]*", re.IGNORECASE)
_PUBLIC_HTTP_URL = re.compile(
    r"\bhttps?://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
    r"(?::[0-9]{1,5})?(?:[/?#][^\s<>\"'{}]*)?",
    re.IGNORECASE,
)
_PRIVATE_PATH = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9:])/(?:[^/\\\s]+(?:[/\\][^/\\\s]+)*)"
    r"|(?<![A-Za-z0-9])~[/\\](?:[^\s]+)"
    r"|(?<![A-Za-z0-9])\.\.?[/\\](?:[^\s]+)"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s]+"
    r"|(?<!\\)\\\\[^\\\s]+\\[^\\\s]+"
    r"|\bfile:///"
    r"|(?<![A-Za-z0-9_:@.-])(?:[^/\\\s]+[/\\])+[^/\\\s]+"
    r")",
    re.IGNORECASE,
)
_PRIVATE_IDENTIFIER = re.compile(
    r"\b[a-z][a-z0-9_]{0,31}_[0-9a-f]{16,64}\b",
    re.IGNORECASE,
)
_PERCENT_ESCAPE = re.compile(r"%[0-9a-f]{2}", re.IGNORECASE)
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-f]{2})", re.IGNORECASE)
_FILE_LOCATOR = re.compile(r"\bfile://", re.IGNORECASE)
_PRIVATE_PUBLIC_PATH_MARKER = re.compile(
    r"(?:"
    r"(?:^|[/\\])[A-Za-z]:[/\\]"
    r"|(?:^|[/\\])\.\.(?:[/\\]|$)"
    r"|(?:^|[/\\])\\\\[^/\\\s]+\\[^/\\\s]+"
    r")",
    re.IGNORECASE,
)
_BINDING_HMAC_KEY = secrets.token_bytes(32)
_BINDING_DOMAIN = b"friday/semantic-supervisor-binding/v1\0"
_ADMITTED_TASKS = {
    TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value,
    TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB.value,
}
_SUPERVISOR_CONTEXT_TOKENS = 4_096
_SUPERVISOR_MAX_OUTPUT_TOKENS = 512
SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES = (
    _SUPERVISOR_CONTEXT_TOKENS - _SUPERVISOR_MAX_OUTPUT_TOKENS - SECONDARY_CONTEXT_TOKEN_RESERVE
)
_PRIVATE_PATH_DENIAL_PROJECTION = "/private/redacted"
_PRIVATE_IDENTIFIER_DENIAL_PROJECTION = "raw_0000000000000000"
_SECRET_DENIAL_PROJECTION = "Bearer AAAAAAAAAAAAAAAA"


@dataclass(frozen=True, slots=True)
class SupervisorEligibility:
    eligible: bool
    skip_reason: SupervisorSkipReason
    task_class: TaskClass


@dataclass(frozen=True, slots=True)
class ParsedSupervisorProposal:
    """Process-local parse result; only its code-owned decision may execute."""

    proposal_digest: str
    decision: PolicyDecision


def supervisor_mode_from_settings(settings: object) -> SupervisorMode:
    return SupervisorMode.fail_closed(getattr(settings, "semantic_supervisor_mode", "off"))


def supervisor_task_allowlist(settings: object) -> frozenset[str]:
    tasks = admitted_supervisor_tasks(getattr(settings, "semantic_supervisor_tasks", ()))
    return frozenset(task for task in tasks if task in _ADMITTED_TASKS)


def supervisor_budgets_from_settings(settings: object) -> SupervisorBudgets:
    max_steps = getattr(settings, "semantic_supervisor_max_steps", 6)
    if not isinstance(max_steps, int) or isinstance(max_steps, bool):
        max_steps = 6
    return SupervisorBudgets(
        max_steps=max(1, min(6, max_steps)),
        max_parallel_reads=2,
        max_review_rounds=0,
    )


def supervisor_review_rounds_from_settings(settings: object) -> int:
    """P1 never reviews.  The setting is retained for a later admitted phase."""

    raw = getattr(settings, "semantic_supervisor_max_review_rounds", 1)
    if not isinstance(raw, int) or isinstance(raw, bool):
        return 0
    return 0


def supervisor_timeout_sec(settings: object) -> float:
    raw = getattr(settings, "semantic_supervisor_timeout_sec", 12.0)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return 12.0
    return max(0.1, min(15.0, float(raw)))


def _folded(message: str) -> str:
    return message.casefold()


def _has_any(message: str, needles: tuple[str, ...]) -> bool:
    folded = _folded(message)
    return any(needle in folded for needle in needles)


def _url_component_contains_private_path(
    value: str,
    *,
    ordinary_public_path: bool = False,
    query_form_encoding: bool = False,
) -> bool:
    """Reject decoded private material inside an otherwise public URL."""

    def denied(candidate: str) -> bool:
        return bool(
            _INVALID_PERCENT_ESCAPE.search(candidate)
            or _HTTP_URL_TOKEN.search(candidate)
            or _FILE_LOCATOR.search(candidate)
            or (not ordinary_public_path and _PRIVATE_PATH.search(candidate))
            or (ordinary_public_path and _PRIVATE_PUBLIC_PATH_MARKER.search(candidate))
            or _PRIVATE_IDENTIFIER.search(candidate)
            or not secondary_model_messages_are_secret_free(({"role": "user", "content": candidate},))
        )

    candidate = value
    for _ in range(3):
        # Nested locators are unnecessary to the compact planning projection
        # and otherwise provide an encoding layer around a private query value.
        if denied(candidate):
            return True
        try:
            decoder = unquote_plus if query_form_encoding else unquote
            decoded = decoder(candidate, errors="strict")
        except UnicodeError:
            return True
        if decoded == candidate:
            return False
        candidate = decoded
    return bool(_PERCENT_ESCAPE.search(candidate) or denied(candidate))


def _public_url_contains_private_path(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    try:
        parsed_port = parsed.port
    except ValueError:
        return True
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "%" in parsed.netloc
        or "\\" in parsed.netloc
        or (parsed_port is not None and not 1 <= parsed_port <= 65_535)
    ):
        return True
    return (
        _url_component_contains_private_path(
            parsed.path,
            ordinary_public_path=True,
        )
        or (
            bool(parsed.query)
            and _url_component_contains_private_path(
                parsed.query,
                query_form_encoding=True,
            )
        )
        or (bool(parsed.fragment) and _url_component_contains_private_path(parsed.fragment))
    )


def _contains_private_path(message: str) -> bool:
    """Reject filesystem-like tokens while allowing a well-formed public URL."""

    private_locator_in_url = False

    def _remove_public_url(match: re.Match[str]) -> str:
        nonlocal private_locator_in_url
        token = match.group(0)
        syntax_token = token.rstrip(".,;!?)]")
        if _PUBLIC_HTTP_URL.fullmatch(syntax_token) is None or _public_url_contains_private_path(token):
            private_locator_in_url = True
        return ""

    without_public_urls = _HTTP_URL_TOKEN.sub(_remove_public_url, message)
    return private_locator_in_url or _PRIVATE_PATH.search(without_public_urls) is not None


def _bounded_message_source(message: str) -> str:
    """Preserve full-source privacy rejection without retaining its body.

    The useful prompt is a bounded prefix, but a credential or path after that
    prefix must not disappear from the admission decision.  A fixed marker
    carries only the rejection class into the existing request guards.
    """

    if _contains_private_path(message):
        return _PRIVATE_PATH_DENIAL_PROJECTION
    if _PRIVATE_IDENTIFIER.search(message) is not None:
        return _PRIVATE_IDENTIFIER_DENIAL_PROJECTION
    if not secondary_model_messages_are_secret_free(({"role": "user", "content": message},)):
        return _SECRET_DENIAL_PROJECTION
    return message[:1_200]


def _with_projected_message(
    supervisor_input: SupervisorInput,
    message: str,
) -> SupervisorInput:
    return replace(
        supervisor_input,
        turn=replace(supervisor_input.turn, message=message),
    )


def _supervisor_message_bytes(supervisor_input: SupervisorInput) -> int:
    return sum(
        len(item["content"].encode("utf-8", errors="strict"))
        for item in build_supervisor_messages(supervisor_input)
    )


def _fit_supervisor_message_envelope(supervisor_input: SupervisorInput) -> SupervisorInput:
    """Choose the longest character-safe prefix that fits the exact 4K profile."""

    if _candidate_task_class(supervisor_input) is TaskClass.UNKNOWN:
        # Incomplete shapes remain available to the policy-kernel contract
        # tests, but can never produce an admitted model request.
        return supervisor_input
    message = supervisor_input.turn.message
    empty = _with_projected_message(supervisor_input, "")
    if _supervisor_message_bytes(empty) > SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES:
        raise SupervisorContractError("fixed supervisor envelope exceeds the adapter input budget")
    if _supervisor_message_bytes(supervisor_input) <= SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES:
        return supervisor_input

    lower = 0
    upper = len(message)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        candidate = _with_projected_message(supervisor_input, message[:middle])
        if _supervisor_message_bytes(candidate) <= SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES:
            lower = middle
        else:
            upper = middle - 1
    projected = _with_projected_message(supervisor_input, message[:lower])
    if _supervisor_message_bytes(projected) > SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES:
        raise SupervisorContractError("supervisor projection exceeds the adapter input budget")
    return projected


def classify_supervisor_task(turn: TurnInput) -> TaskClass:
    message = turn.message
    web = _has_any(message, _WEB_CUES)
    compare = _has_any(message, _COMPARE_CUES)
    archive = _has_any(message, _ARCHIVE_CUES)
    if turn.attachments and web and compare:
        return TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
    if not turn.attachments and turn.conversation_present and web and archive:
        return TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB
    if turn.attachments and not web:
        return TaskClass.UNKNOWN
    return TaskClass.ORDINARY_DIALOGUE


def _is_small_talk(turn: TurnInput) -> bool:
    if turn.attachments or turn.enable_tools is False:
        return False
    text = turn.message.strip()
    if not text or len(text) > 48:
        return False
    return _has_any(text, _SMALL_TALK) and not _has_any(text, _WEB_CUES + _COMPARE_CUES + _ARCHIVE_CUES)


def exact_lane_owns_turn(
    turn: TurnInput,
    *,
    pending_bound: bool = False,
) -> bool:
    if pending_bound or archive_candidate_cancel_requested(turn.message):
        return True
    return parse_archive_candidate_ordinal(turn.message) is not None and not turn.attachments


def supervisor_eligibility(
    turn: TurnInput,
    settings: object,
    *,
    pending_bound: bool = False,
) -> SupervisorEligibility:
    mode = supervisor_mode_from_settings(settings)
    if mode is SupervisorMode.OFF:
        return SupervisorEligibility(False, SupervisorSkipReason.MODE_OFF, TaskClass.UNKNOWN)
    if exact_lane_owns_turn(turn, pending_bound=pending_bound):
        return SupervisorEligibility(False, SupervisorSkipReason.EXACT_LANE, TaskClass.UNKNOWN)
    if _is_small_talk(turn):
        return SupervisorEligibility(False, SupervisorSkipReason.SMALL_TALK, TaskClass.ORDINARY_DIALOGUE)
    task = classify_supervisor_task(turn)
    allowlist = supervisor_task_allowlist(settings)
    if not allowlist:
        return SupervisorEligibility(False, SupervisorSkipReason.TASK_NOT_ALLOWLISTED, task)
    if task is TaskClass.UNKNOWN and turn.attachments:
        return SupervisorEligibility(False, SupervisorSkipReason.ESTABLISHED_FILE_READ, task)
    if task is TaskClass.ORDINARY_DIALOGUE:
        return SupervisorEligibility(False, SupervisorSkipReason.ORDINARY_DIALOGUE, task)
    if task.value not in allowlist:
        return SupervisorEligibility(False, SupervisorSkipReason.TASK_NOT_ALLOWLISTED, task)
    if task is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB and (
        len(turn.attachments) != 1 or not turn.attachments[0].extracted_text_available
    ):
        return SupervisorEligibility(False, SupervisorSkipReason.EVIDENCE_UNAVAILABLE, task)
    return SupervisorEligibility(True, SupervisorSkipReason.NONE, task)


def _language_hint(message: str) -> str:
    if any("а" <= char <= "я" or char in "ё" for char in message.casefold()):
        return "ru"
    if any("a" <= char <= "z" for char in message.casefold()):
        return "en"
    return "und"


def _reply_kind(turn: TurnInput) -> str:
    if turn.reply_assistant_reference:
        return "assistant"
    if turn.reply_quote:
        return "quote"
    return "none"


def _available_evidence(turn: TurnInput) -> tuple[str, ...]:
    domains: list[str] = []
    if turn.attachments:
        domains.append("current_attachment")
    if turn.conversation_present:
        domains.append("conversation_window")
        domains.append("archive")
    if turn.enable_tools:
        domains.append("web")
    return tuple(domains)


def build_supervisor_input(
    turn: TurnInput,
    settings: object,
    *,
    pending_bound: bool = False,
    pending_kind: str = "",
) -> SupervisorInput:
    attachments = tuple(
        {
            "ordinal": item.ordinal,
            "media_kind": item.media_type,
            "text_available": item.extracted_text_available,
        }
        for item in turn.attachments
    )
    state = (
        ContinuationState.OWNED
        if pending_bound
        else (ContinuationState.POSSIBLE if turn.conversation_present else ContinuationState.NONE)
    )
    allowed = (
        (ContinuationDecision.CONTINUE, ContinuationDecision.CANCEL)
        if pending_bound
        else (ContinuationDecision.NEW_TASK, ContinuationDecision.CANCEL)
    )
    supervisor_input = SupervisorInput(
        request_class="user_turn",
        turn=SupervisorTurnProjection.parse(
            {
                "message": _bounded_message_source(turn.message),
                "language_hint": _language_hint(turn.message),
                "attachments": list(attachments),
                "reply_kind": _reply_kind(turn),
            }
        ),
        continuation=SupervisorContinuation(
            state=state,
            work_item_kind=pending_kind if pending_bound else "",
            allowed_actions=allowed,
        ),
        available_evidence=_available_evidence(turn),
        manifest=bounded_capability_manifest(turn),
        budgets=supervisor_budgets_from_settings(settings),
    )
    return _fit_supervisor_message_envelope(supervisor_input)


def _candidate_task_class(supervisor_input: SupervisorInput) -> TaskClass:
    evidence = set(supervisor_input.available_evidence)
    if (
        len(supervisor_input.turn.attachments) == 1
        and supervisor_input.turn.attachments[0].text_available
        and {"current_attachment", "web"} <= evidence
    ):
        return TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
    if not supervisor_input.turn.attachments and {"archive", "web"} <= evidence:
        return TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB
    return TaskClass.UNKNOWN


def _response_template(supervisor_input: SupervisorInput, task: TaskClass) -> dict[str, Any]:
    if task is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB:
        first_target = FILE_CURRENT_READ_ID
        first_input: dict[str, Any] = {"attachment_ordinal": 1}
        first_outcome = "complete_source_evidence"
        first_purpose = "Read the current attachment."
        first_criterion = "current_attachment_evidence_present"
    elif task is TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB:
        first_target = ARCHIVE_SEARCH_ID
        first_input = {"query_intent": "archive evidence relevant to the comparison"}
        first_outcome = "archive_evidence"
        first_purpose = "Find relevant archived evidence."
        first_criterion = "archive_evidence_present"
    else:
        raise SupervisorContractError("supervisor input has no admitted P1 task shape")
    return {
        "schema": SUPERVISOR_PROPOSAL_SCHEMA,
        "manifest_id": supervisor_input.manifest.manifest_id,
        "task_class": task.value,
        "goal": "Compare supplied evidence with current public rules.",
        "continuation_decision": "new_task",
        "risk_hints": ["external_read", "multi_source"],
        "steps": [
            {
                "step_id": "s1",
                "kind": "capability",
                "target_id": first_target,
                "purpose": first_purpose,
                "depends_on": [],
                "parallel_group": "evidence",
                "input": first_input,
                "expected_outcome": first_outcome,
            },
            {
                "step_id": "s2",
                "kind": "capability",
                "target_id": WEB_SEARCH_CURRENT_ID,
                "purpose": "Find relevant current public rules.",
                "depends_on": [],
                "parallel_group": "evidence",
                "input": {"query_intent": "current public rules relevant to supplied evidence"},
                "expected_outcome": "verified_current_sources",
            },
            {
                "step_id": "s3",
                "kind": "model",
                "target_id": PRIMARY_SYNTHESIS_ID,
                "purpose": "Compare admitted evidence with citations.",
                "depends_on": ["s1", "s2"],
                "parallel_group": None,
                "input": {},
                "expected_outcome": "cited_comparison",
            },
        ],
        "completion_criteria": [
            first_criterion,
            "current_public_evidence_has_coverage",
            "material_differences_source_bound",
        ],
        "review_mode": "none",
        "fallback": "primary_only",
    }


def _compact_manifest(supervisor_input: SupervisorInput, task: TaskClass) -> dict[str, Any]:
    manifest = supervisor_input.manifest
    capability_ids = (
        {FILE_CURRENT_READ_ID, WEB_SEARCH_CURRENT_ID}
        if task is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
        else {ARCHIVE_SEARCH_ID, WEB_SEARCH_CURRENT_ID}
    )
    return {
        "manifest_id": manifest.manifest_id,
        "capabilities": [
            {
                "id": item.id,
                "class": item.effect_class.value,
                "availability": item.availability.value,
                "input_schema_id": item.input_schema_id,
            }
            for item in manifest.capabilities
            if item.id in capability_ids
        ],
        "model_roles": [
            {"id": item.id, "availability": item.availability.value}
            for item in manifest.model_roles
            if item.id == PRIMARY_SYNTHESIS_ID
        ],
    }


def build_supervisor_messages(supervisor_input: SupervisorInput) -> tuple[dict[str, str], ...]:
    task = _candidate_task_class(supervisor_input)
    trusted = {
        "policy_id": SUPERVISOR_PRODUCT_POLICY_ID,
        "policy_sha256": SUPERVISOR_PRODUCT_POLICY_SHA256,
        "tools_allowed": False,
        "effects_allowed": False,
        "publication_allowed": False,
    }
    payload = {
        "trusted_policy": trusted,
        "untrusted_turn": {
            "message": supervisor_input.turn.message,
            "language_hint": supervisor_input.turn.language_hint,
            "attachment_count": len(supervisor_input.turn.attachments),
            "reply_kind": supervisor_input.turn.reply_kind,
        },
        "untrusted_evidence_summary": [],
        "untrusted_payload": {
            "capability_manifest": _compact_manifest(supervisor_input, task),
            "constraints": {
                "continuation_decision": "new_task",
                "max_steps": 3,
                "max_parallel_reads": supervisor_input.budgets.max_parallel_reads,
                "max_review_rounds": 0,
            },
            "response_template": _response_template(supervisor_input, task),
        },
    }
    return (
        {"role": "system", "content": _SUPERVISOR_SYSTEM_PROMPT},
        {"role": "user", "content": canonical_dumps(payload)},
    )


def build_supervisor_request(
    supervisor_input: SupervisorInput,
    *,
    absolute_deadline_monotonic: float,
    max_output_tokens: int = 512,
) -> ModelRequest:
    if _contains_private_path(supervisor_input.turn.message):
        raise SupervisorContractError("supervisor turn contains a private path")
    if _PRIVATE_IDENTIFIER.search(supervisor_input.turn.message) is not None:
        raise SupervisorContractError("supervisor turn contains a private identifier")
    task = _candidate_task_class(supervisor_input)
    messages = build_supervisor_messages(supervisor_input)
    if not secondary_model_messages_are_secret_free(messages):
        raise SupervisorContractError("supervisor messages contain secret material")
    if _supervisor_message_bytes(supervisor_input) > SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES:
        raise SupervisorContractError("supervisor request exceeds the adapter input budget")
    return ModelRequest(
        workload=ModelWorkload.PLAN_CANDIDATE,
        messages=messages,
        max_output_tokens=max(1, min(_SUPERVISOR_MAX_OUTPUT_TOKENS, max_output_tokens)),
        absolute_deadline_monotonic=absolute_deadline_monotonic,
        priority=ModelPriority.BACKGROUND,
        effect_class=EffectClass.NONE,
        modality=ModelModality.TEXT,
        require_structured_output=True,
        structured_output_schema=supervisor_proposal_json_schema(task_class=task),
        require_independent_model=True,
        contains_private_text=True,
    )


def binding_digest(*parts: str) -> str:
    material = "\0".join(parts).encode("utf-8")
    return hmac.new(_BINDING_HMAC_KEY, _BINDING_DOMAIN + material, hashlib.sha256).hexdigest()


def _structured_to_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def validate_shadow_proposal(
    result: SecondaryResult,
    supervisor_input: SupervisorInput,
    context: PolicyAdmissionContext,
) -> tuple[str, str, str, int, tuple[str, ...]]:
    parsed = parse_and_admit_supervisor_proposal(result, supervisor_input, context)
    decision = parsed.decision
    structured = _structured_to_mapping(result.structured_output)
    assert structured is not None  # proved by parse_and_admit_supervisor_proposal
    proposal = SupervisorProposal.parse(structured)
    effects = tuple(
        dict.fromkeys(step.effect_class.value for step in (decision.plan.steps if decision.plan else ()))
    )
    return (
        parsed.proposal_digest,
        "valid" if decision.admitted else "rejected",
        decision.reason_code,
        len(proposal.steps),
        effects,
    )


def parse_and_admit_supervisor_proposal(
    result: SecondaryResult,
    supervisor_input: SupervisorInput,
    context: PolicyAdmissionContext,
) -> ParsedSupervisorProposal:
    """Reparse one exact response and retain the kernel-minted plan in memory."""

    structured = _structured_to_mapping(result.structured_output)
    if structured is None:
        raise SupervisorContractError("supervisor proposal must be one JSON object")
    # The endpoint adapter exposes a convenient parsed mapping, but ordinary
    # json.loads has already collapsed duplicate keys.  Reparse the exact raw
    # visible object with the supervisor contract's duplicate/non-finite guard,
    # then prove that the transport projection names the same proposal.
    proposal = SupervisorProposal.parse(result.visible_content)
    structured_proposal = SupervisorProposal.parse(structured)
    if structured_proposal.canonical_sha256() != proposal.canonical_sha256():
        raise SupervisorContractError("supervisor raw and structured proposals differ")
    decision = admit_supervisor_proposal(proposal, supervisor_input, context)
    return ParsedSupervisorProposal(
        proposal_digest=proposal.canonical_sha256(),
        decision=decision,
    )


async def observe_semantic_supervisor_shadow(
    turn: TurnInput,
    settings: object,
    primary: Callable[[], Awaitable[T]],
    *,
    scheduler: SecondaryBrainScheduler | None = None,
    pending_bound: bool = False,
    pending_kind: str = "",
    current_route: str = "legacy",
    actor_binding_sha256: str = "",
    conversation_binding_sha256: str = "",
    observer: Callable[[SupervisorObservation], Awaitable[None] | None] | None = None,
) -> tuple[T, SupervisorObservation]:
    """Run the primary path first.  A proposal never changes that result."""

    requested = supervisor_mode_from_settings(settings).value
    configured_profile = str(getattr(settings, "secondary_llm_profile", "") or "")
    accepted_profile_id = (
        SUPERVISOR_RUNTIME_PROFILE_ID if configured_profile == SUPERVISOR_RUNTIME_PROFILE_ID else ""
    )
    eligibility = supervisor_eligibility(turn, settings, pending_bound=pending_bound)
    if not eligibility.eligible:
        observation = skipped_observation(
            requested_mode=requested,
            skip_reason=eligibility.skip_reason,
            current_route=current_route,
            accepted_profile_id=accepted_profile_id,
        )
        result = await primary()
        if observer is not None:
            maybe = observer(observation)
            if maybe is not None:
                await maybe
        return result, observation

    supervisor_input = build_supervisor_input(
        turn,
        settings,
        pending_bound=pending_bound,
        pending_kind=pending_kind,
    )

    async def _primary() -> T:
        return await primary()

    if scheduler is None:
        observation = skipped_observation(
            requested_mode=requested,
            skip_reason=SupervisorSkipReason.SECONDARY_UNAVAILABLE,
            current_route=current_route,
            accepted_profile_id=accepted_profile_id,
            manifest_digest=supervisor_input.manifest.digest_hex(),
            supervisor_input_digest=binding_digest("supervisor-input", supervisor_input.canonical_sha256()),
        )
        result = await _primary()
        if observer is not None:
            maybe = observer(observation)
            if maybe is not None:
                await maybe
        return result, observation

    try:
        context = PolicyAdmissionContext(
            actor_binding_sha256=actor_binding_sha256,
            conversation_binding_sha256=conversation_binding_sha256,
        )
    except SupervisorContractError:
        observation = skipped_observation(
            requested_mode=requested,
            skip_reason=SupervisorSkipReason.BINDING_UNAVAILABLE,
            current_route=current_route,
            accepted_profile_id=accepted_profile_id,
            manifest_digest=supervisor_input.manifest.digest_hex(),
            supervisor_input_digest=binding_digest("supervisor-input", supervisor_input.canonical_sha256()),
        )
        result = await _primary()
        if observer is not None:
            maybe = observer(observation)
            if maybe is not None:
                await maybe
        return result, observation

    captured: dict[str, Any] = {
        "proposal_digest": "",
        "proposal_parse_status": "skipped",
        "policy_verdict": "not_evaluated",
        "policy_reason": "none",
        "task_class": eligibility.task_class.value,
        "step_count": 0,
        "effect_classes": (),
        "health": "called",
        "skip": SupervisorSkipReason.NONE,
    }

    try:
        request = build_supervisor_request(
            supervisor_input,
            absolute_deadline_monotonic=time.monotonic() + supervisor_timeout_sec(settings),
        )
    except SupervisorContractError:
        observation = parsed_observation(
            requested_mode=requested,
            manifest_digest=supervisor_input.manifest.digest_hex(),
            supervisor_input_digest=binding_digest("supervisor-input", supervisor_input.canonical_sha256()),
            proposal_digest="",
            proposal_parse_status="secret_denied",
            policy_verdict="not_evaluated",
            policy_reason="none",
            task_class=eligibility.task_class.value,
            step_count=0,
            effect_classes=(),
            current_route=current_route,
            endpoint_health_class="not_called",
            accepted_profile_id=accepted_profile_id,
            skip_reason=SupervisorSkipReason.SECRET_MATERIAL,
        )
        result = await _primary()
        if observer is not None:
            maybe = observer(observation)
            if maybe is not None:
                await maybe
        return result, observation

    def _validator(secondary_result: SecondaryResult) -> bool:
        try:
            digest, verdict, reason, steps, effects = validate_shadow_proposal(
                secondary_result,
                supervisor_input,
                context,
            )
        except SupervisorContractError:
            captured["proposal_parse_status"] = "malformed"
            captured["policy_verdict"] = "not_evaluated"
            captured["skip"] = SupervisorSkipReason.MALFORMED_PROPOSAL
            return False
        captured["proposal_digest"] = digest
        captured["proposal_parse_status"] = "parsed"
        captured["policy_verdict"] = verdict
        captured["policy_reason"] = reason
        captured["step_count"] = steps
        captured["effect_classes"] = effects
        if verdict != "valid":
            captured["skip"] = SupervisorSkipReason.POLICY_REJECTED
            return False
        return True

    # This helper is deliberately synchronous and diagnostic: the live wrapper
    # owns background scheduling.  Draining one attempt here ensures the
    # returned observation describes that exact attempt rather than a task that
    # may not have run yet.
    result = await _primary()
    attempt = await scheduler.evaluate_shadow(
        request,
        validator=_validator,
        invalidate_on_rejection=False,
    )
    if not attempt.succeeded and captured["skip"] is SupervisorSkipReason.NONE:
        captured["skip"] = shadow_attempt_skip_reason(attempt)
    captured["health"] = "accepted" if attempt.succeeded else "closed_failure"

    observation = parsed_observation(
        requested_mode=requested,
        manifest_digest=supervisor_input.manifest.digest_hex(),
        supervisor_input_digest=binding_digest("supervisor-input", supervisor_input.canonical_sha256()),
        proposal_digest=(
            binding_digest("proposal", str(captured["proposal_digest"]))
            if captured["proposal_digest"]
            else ""
        ),
        proposal_parse_status=str(captured["proposal_parse_status"]),
        policy_verdict=str(captured["policy_verdict"]),
        policy_reason=str(captured["policy_reason"]),
        task_class=str(captured["task_class"]),
        step_count=int(captured["step_count"]),
        effect_classes=tuple(captured["effect_classes"]),
        current_route=current_route,
        endpoint_health_class=str(captured["health"]),
        accepted_profile_id=accepted_profile_id,
        skip_reason=captured["skip"],
    )
    if observer is not None:
        maybe = observer(observation)
        if maybe is not None:
            await maybe
    return result, observation


def map_secondary_failure(failure: SecondaryFailure | None) -> SupervisorSkipReason:
    if failure is None:
        return SupervisorSkipReason.NONE
    if failure in {
        SecondaryFailure.DISABLED,
        SecondaryFailure.MISCONFIGURED,
        SecondaryFailure.CONNECT_FAILED,
    }:
        return SupervisorSkipReason.SECONDARY_UNAVAILABLE
    if failure is SecondaryFailure.SECRET_MATERIAL_DENIED:
        return SupervisorSkipReason.SECRET_MATERIAL
    if failure in {SecondaryFailure.ADMISSION_BUSY, SecondaryFailure.COOLDOWN}:
        return SupervisorSkipReason.SATURATED
    if failure in {SecondaryFailure.DEADLINE, SecondaryFailure.TIMEOUT}:
        return SupervisorSkipReason.TIMEOUT
    if failure is SecondaryFailure.WORKLOAD_DISALLOWED:
        return SupervisorSkipReason.WORKLOAD_DISALLOWED
    if failure in {SecondaryFailure.MALFORMED_RESPONSE, SecondaryFailure.DEGENERATION}:
        return SupervisorSkipReason.MALFORMED_PROPOSAL
    return SupervisorSkipReason.SECONDARY_UNAVAILABLE


def shadow_attempt_skip_reason(attempt: SecondaryAttempt) -> SupervisorSkipReason:
    if attempt.succeeded:
        return SupervisorSkipReason.NONE
    return map_secondary_failure(attempt.failure)
