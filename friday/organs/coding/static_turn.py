"""Owner-only Coding Mode turn: static inspect, isolated worker, never execute."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from friday.orchestration.coding_inspect_report import (
    CodingInspectReportState,
    CodingInspectReportV1,
    build_coding_inspect_report,
)
from friday.orchestration.coding_worker_admission import CodingWorkerAdmissionState
from friday.organs.coding.worker_boundary import (
    CodingWorkerBoundaryV1,
    default_coding_worker_boundary,
)
from friday.organs.coding.worker_spawn import (
    CodingWorkerRunner,
    CodingWorkerSpawnV1,
    compose_coding_worker_admission,
    spawn_coding_worker,
)
from friday.permissions import ActorContext, AuthorizationError

_EXECUTE_CLAIM_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:run|execute|exec|compile|rebuild|pytest|npm|make|cargo|go\s+test)\b"
    r"|запусти|выполн|скомпилир|пересобери|прогон"
    r")"
)


def _require_coding_actor(actor: ActorContext) -> None:
    if not actor.is_private_telegram_chat:
        raise AuthorizationError("Coding mode requires the owner's private Telegram chat")
    if not actor.is_owner:
        raise AuthorizationError("Coding mode is available only to the installation owner")


def _execute_claimed(message: str) -> bool:
    return _EXECUTE_CLAIM_RE.search(message or "") is not None


def _members_from_attachments(attachments: Sequence[object] | None) -> list[dict[str, object]]:
    members: list[dict[str, object]] = []
    for item in attachments or ():
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("filename") or item.get("name") or item.get("relative_path") or "").strip()
        if not name:
            continue
        size = item.get("size")
        if type(size) is not int or size < 0:
            size = 0
        executable = item.get("executable") is True
        members.append(
            {
                "relative_path": name,
                "size": size,
                "file_kind": "regular_file",
                "executable": executable,
                "link_kind": "none",
            }
        )
    return members


def _snapshot_sha256(members: Sequence[Mapping[str, object]]) -> str:
    payload = json.dumps(list(members), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _homes() -> tuple[str, str, str]:
    from friday.config import default_home

    home = Path(default_home())
    return str(home), str(Path.home()), str(home / "data" / "state")


def _russian_inspect_reply(
    report: CodingInspectReportV1,
    *,
    execute_claimed: bool,
    worker_admitted: bool,
) -> str:
    refused = "Исполнение, сборка и тесты не допущены."
    if execute_claimed:
        refused = "Запрос на выполнение отклонён. " + refused
    if worker_admitted:
        refused = refused + " Изолированный worker допущен, код загрузок не исполнялся."
    else:
        refused = refused + " Изолированный worker не допущен."
    if report.report is CodingInspectReportState.EMPTY:
        return "Режим Coding: статический осмотр. В этом ходе нет исходников для осмотра. " + refused
    if report.report is CodingInspectReportState.BLOCKED:
        return "Режим Coding: осмотр заблокирован. Имена и состав исходников не раскрываю. " + refused
    inspection = report.inspection
    hazards = report.hazards
    hint = report.toolchain_hint
    if inspection is None:
        return "Режим Coding: осмотр заблокирован. Имена и состав исходников не раскрываю. " + refused
    parts = [
        "Режим Coding: статический осмотр завершён.",
        f"Файлов: {inspection.file_count}, каталогов: {inspection.directory_count}.",
    ]
    if hint is not None and hint.language_hints:
        parts.append("Языки: " + ", ".join(hint.language_hints) + ".")
    if hazards is not None and hazards.hazards.value == "present":
        kinds = ", ".join(kind.value for kind in hazards.hazard_kinds)
        parts.append(f"Признаки риска: {kinds}.")
    else:
        parts.append("Признаков риска в метаданных нет.")
    parts.append("Код не исполнялся и не пересобирался.")
    parts.append(refused)
    return " ".join(parts)


def _ensure_conversation(
    storage: Any,
    *,
    person_id: str,
    conversation_id: str | None,
    message: str,
) -> str | None:
    if storage is None:
        return conversation_id
    conversation = storage.get_conversation(conversation_id, person_id) if conversation_id else None
    if conversation is None:
        title = (message or "Coding").strip()[:80] or "Coding"
        conversation = storage.create_conversation(person_id, title=title, mode="coding")
    elif str(conversation.get("mode") or "") != "coding":
        conversation = (
            storage.set_conversation_mode(str(conversation["id"]), person_id, "coding") or conversation
        )
    return str(conversation["id"])


def handle_coding_static_turn(
    *,
    storage: Any,
    user_id: str,
    actor: ActorContext,
    message: str,
    conversation_id: str | None,
    attachments: list[dict[str, Any]] | None,
    enable_tools: bool = False,
    worker_boundary: CodingWorkerBoundaryV1 | None = None,
    spawn_runner: CodingWorkerRunner | None = None,
) -> dict[str, Any]:
    """Inspect attachment metadata. Spawn an isolated worker only if admitted."""

    del enable_tools
    _require_coding_actor(actor)
    if not actor.shared_tenant and actor.user_id != user_id and not actor.is_owner:
        raise PermissionError("actor cannot chat as another user")
    person_id = actor.own_id if actor.shared_tenant else user_id
    members = _members_from_attachments(attachments)
    report_id = "coding-inspect-" + secrets.token_hex(8)
    turn_id = "coding-turn-" + secrets.token_hex(8)
    operation_id = "coding-op-" + secrets.token_hex(8)
    snapshot = _snapshot_sha256(members)
    report = build_coding_inspect_report(report_id, turn_id, members=members)
    execute_claimed = _execute_claimed(message)
    friday_home, owner_home, database_path = _homes()
    boundary = worker_boundary or default_coding_worker_boundary(
        friday_home=friday_home,
        owner_home=owner_home,
        database_path=database_path,
    )
    boundary = replace(
        boundary,
        workspace_path="work/" + operation_id,
        export_path="out/" + operation_id,
    )
    admission = compose_coding_worker_admission(
        admission_id="coding-adm-" + secrets.token_hex(8),
        authenticated_turn_id=turn_id,
        worker_id="coding-w-" + secrets.token_hex(8),
        operation_id=operation_id,
        project_id="coding-p-" + secrets.token_hex(8),
        revision_selector=snapshot,
        boundary=boundary,
    )
    spawn = CodingWorkerSpawnV1(False, admission.admission, "skipped", False)
    if execute_claimed and admission.admission is CodingWorkerAdmissionState.ADMITTED:
        spawn = spawn_coding_worker(admission, boundary, runner=spawn_runner)
    worker_admitted = admission.admission is CodingWorkerAdmissionState.ADMITTED
    text = _russian_inspect_reply(
        report, execute_claimed=execute_claimed, worker_admitted=worker_admitted
    )
    persisted_id = _ensure_conversation(
        storage,
        person_id=person_id,
        conversation_id=conversation_id,
        message=message,
    )
    assistant_id = None
    if storage is not None and persisted_id:
        storage.store_message(
            persisted_id,
            person_id,
            "user",
            message or "",
            metadata={
                "interaction_mode": "coding",
                "tools_enabled": False,
                "had_attachments": bool(members),
            },
        )
        assistant = storage.store_message(
            persisted_id,
            person_id,
            "assistant",
            text,
            metadata={
                "interaction_mode": "coding",
                "coding_inspect_report": report.report.value,
                "coding_inspect_reason": report.reason.value,
                "coding_worker_admission": admission.admission.value,
                "coding_worker_admission_reason": admission.reason.value,
            },
        )
        assistant_id = assistant.get("id")
    context: dict[str, Any] = {
        "interaction_mode": "coding",
        "coding_inspect_report": report.report.value,
        "coding_inspect_reason": report.reason.value,
        "coding_execution_attempted": False,
        "coding_worker_admission": admission.admission.value,
        "coding_worker_admission_reason": admission.reason.value,
        "coding_worker_spawned": spawn.spawned,
        "coding_worker_probe": spawn.probe,
        "llm_failed": False,
    }
    if report.report is not CodingInspectReportState.BLOCKED:
        context["coding_member_count"] = report.member_count
    return {
        "conversation_id": persisted_id or conversation_id or "",
        "message_id": assistant_id,
        "message": text,
        "message_format": "plain",
        "verified": False,
        "citations": [],
        "tools_used": [],
        "files": [],
        "voice": None,
        "web_evidence_status": "none",
        "web_evidence_scope": "none",
        "web_sources": [],
        "attachment_context_available": False,
        "context": context,
    }
