"""Owner-only Coding Mode turn: static inspect, isolated worker, fail-closed execute."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from friday.orchestration.coding_inspect_report import (
    CodingInspectReportState,
    CodingInspectReportV1,
    build_coding_inspect_report,
)
from friday.orchestration.coding_mode_execute_claim import (
    CodingModeExecuteClaimState,
    CodingModeExecuteClaimV1,
    CodingModeExecuteOperation,
    build_coding_mode_execute_claim,
)
from friday.orchestration.coding_mode_intent import build_coding_mode_intent
from friday.orchestration.coding_mode_plan_gate import build_coding_mode_plan_gate
from friday.orchestration.coding_mode_snapshot import build_coding_mode_snapshot
from friday.orchestration.coding_mode_view import build_coding_mode_view
from friday.orchestration.coding_project_identity import build_coding_project_identity
from friday.orchestration.coding_worker_admission import CodingWorkerAdmissionState
from friday.organs.coding.create import (
    CodingCreateObserveState,
    create_requested,
    observe_coding_create,
)
from friday.organs.coding.extract import (
    CodingArchiveExtractObserveReason,
    CodingArchiveExtractObserveState,
    CodingArchiveExtractObserveV1,
    first_archive_bytes,
    observe_coding_archive_extract,
)
from friday.organs.coding.loop import (
    CodingIsolatedLoopReason,
    CodingIsolatedLoopState,
    CodingIsolatedLoopV1,
    observe_coding_isolated_loop,
)
from friday.organs.coding.model_edit import (
    CodingModelEditProposal,
    CodingModelEditState,
    model_edit_requested,
    parse_model_edit_request,
)
from friday.organs.coding.modify import (
    modify_requested,
    observe_coding_upload_modification,
    prepare_revision_edit,
)
from friday.organs.coding.result_archive import observe_coding_result_archive
from friday.organs.coding.revision import CodingRevisionUnavailable, load_coding_revision, revision_record
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
from friday.organs.coding.workspace_io import publish_members
from friday.permissions import ActorContext, AuthorizationError

_RESTORE_PREFIX_RE = re.compile(r"(?i)^(?:restore|восстанови)(?:\s|$)")
_RESTORE_RE = re.compile(r"(?i)^(?:restore|восстанови) (msg_[0-9a-f]{16}) ([0-9a-f]{64})$")

_EDIT_PREFIX_RE = re.compile(r"(?i)^(?:edit|modify|измени)\s+msg_")
_EDIT_RE = re.compile(r"(?i)^(?:edit|modify|измени) (msg_[0-9a-f]{16}) ([0-9a-f]{64})\r?\n([\s\S]+)$")

_TEST_CLAIM_RE = re.compile(r"(?i)(?:\b(?:pytest|py\.test)\b|\bgo\s+test\b|прогон)")
_BUILD_CLAIM_RE = re.compile(r"(?i)(?:\b(?:compile|rebuild|build)\b|скомпилир|пересобери)")
_EXECUTE_CLAIM_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:run|execute|exec|npm|make|cargo)\b"
    r"|запусти|выполн"
    r")"
)


def _require_coding_actor(actor: ActorContext) -> None:
    if not actor.is_private_telegram_chat:
        raise AuthorizationError("Coding mode requires the owner's private Telegram chat")
    if not actor.is_owner:
        raise AuthorizationError("Coding mode is available only to the installation owner")


def _claimed_operation(message: str) -> CodingModeExecuteOperation | None:
    text = message or ""
    if _TEST_CLAIM_RE.search(text) is not None:
        return CodingModeExecuteOperation.TEST
    if _BUILD_CLAIM_RE.search(text) is not None:
        return CodingModeExecuteOperation.BUILD
    if _EXECUTE_CLAIM_RE.search(text) is not None:
        return CodingModeExecuteOperation.EXECUTE
    return None


def _compose_execute_claim(
    *,
    turn_id: str,
    intent: object,
    message: str,
    admission: object,
) -> tuple[bool, CodingModeExecuteClaimV1]:
    operation = _claimed_operation(message)
    execute_requested = operation is not None
    if execute_requested:
        claim = build_coding_mode_execute_claim(
            f"{turn_id}-claim",
            turn_id,
            intent,
            worker=admission,
            operation=operation,
        )
    else:
        claim = build_coding_mode_execute_claim(f"{turn_id}-claim", turn_id, intent)
    return execute_requested, claim


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
    extract: CodingArchiveExtractObserveV1,
    loop: CodingIsolatedLoopV1,
    created_state: str,
    archive_state: str,
    plan_gate: str,
    modify_state: str = "empty",
) -> str:
    if loop.state is CodingIsolatedLoopState.BUILT:
        refused = (
            "Изолированная компиляция выполнена. Код загрузок не исполнялся. Это не сертификат безопасности."
        )
    elif loop.state is CodingIsolatedLoopState.TESTED:
        refused = "Изолированный тест выполнен. Это не сертификат безопасности."
    elif loop.reason is CodingIsolatedLoopReason.RESOURCE_ENFORCEMENT_UNAVAILABLE:
        refused = "Запуск тестов загруженного проекта заблокирован: изоляция ресурсов дерева процессов ещё не подтверждена."
    elif loop.reason is CodingIsolatedLoopReason.NO_TESTS:
        refused = "Поиск тестов не нашёл тестовых случаев. Модули проекта могли быть импортированы."
    elif loop.reason is CodingIsolatedLoopReason.BUILD_FAILED:
        refused = "Изолированная компиляция не удалась. Код не исполнялся."
    elif loop.reason is CodingIsolatedLoopReason.TEST_FAILED:
        refused = "Изолированный тест не прошёл. Это не сертификат безопасности."
    elif loop.reason is CodingIsolatedLoopReason.EXECUTE_FORBIDDEN:
        refused = "Запрос на выполнение отклонён. Исполнение, сборка и тесты не допущены."
    else:
        refused = "Исполнение, сборка и тесты не допущены."
        if execute_claimed:
            refused = "Запрос на выполнение отклонён. " + refused
    if worker_admitted:
        if loop.untrusted_execute:
            refused = refused + " Изолированный worker допущен."
        else:
            refused = refused + " Изолированный worker допущен, код загрузок не исполнялся."
    else:
        refused = refused + " Изолированный worker не допущен."
    if extract.state is CodingArchiveExtractObserveState.EXTRACTED:
        refused = refused + " Архив распакован в изолированное рабочее место."
    elif extract.state is CodingArchiveExtractObserveState.BLOCKED:
        refused = refused + " Распаковка архива не допущена."
    if created_state == "written":
        refused = (
            refused + " Небольшой проект записан в изолированное рабочее место. Программа не исполнялась."
        )
    elif created_state == "blocked":
        refused = refused + " Создание проекта не допущено."
    if modify_state == "admitted":
        refused = refused + " Правка загрузки допущена. Файлы загруженного проекта не изменялись."
    elif modify_state == "blocked":
        refused = refused + " Правка загрузки не допущена."
    if archive_state == "archive":
        refused = refused + " Итоговый архив исходников подготовлен."
    elif archive_state == "file":
        refused = refused + " Итоговый файл исходников подготовлен."
    elif archive_state == "blocked":
        refused = (
            refused + " Выдача исходников заблокирована: состав или целостность результата не подтверждены."
        )
    if plan_gate == "blocked":
        refused = refused + " План Coding не допущен."
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
    model_edit: CodingModelEditProposal | None = None,
) -> dict[str, Any]:
    """Inspect, create or edit an explicit source revision. Never run revision edits."""

    del enable_tools
    _require_coding_actor(actor)
    if not actor.shared_tenant and actor.user_id != user_id and not actor.is_owner:
        raise PermissionError("actor cannot chat as another user")
    person_id = actor.own_id if actor.shared_tenant else user_id
    members = _members_from_attachments(attachments)
    report_id = "coding-inspect-" + secrets.token_hex(8)
    turn_id = "coding-turn-" + secrets.token_hex(8)
    operation_id = "coding-op-" + secrets.token_hex(8)
    project_id = "coding-p-" + secrets.token_hex(8)
    restoring = _RESTORE_PREFIX_RE.match((message or "").strip()) is not None
    model_editing = not restoring and model_edit_requested(message or "")
    editing = model_editing or (not restoring and _EDIT_PREFIX_RE.match((message or "").strip()) is not None)
    revision_operation = restoring or editing
    restore_state = "blocked" if restoring else "empty"
    edit_state = "blocked" if editing else "empty"
    restored = None
    candidate = None
    edit_targets = None
    selection: re.Match[str] | tuple[str, str, str, str] | None = (
        _RESTORE_RE.fullmatch((message or "").strip())
        if restoring
        else _EDIT_RE.fullmatch((message or "").strip())
        if editing
        else None
    )
    if model_editing:
        # A model proposal is data bound to this exact owner request. The source
        # and all write admissions are rechecked here, after the model await.
        try:
            selected = parse_model_edit_request(message)
        except UnicodeError:
            selected = None
        selection = None
        if selected is not None:
            selection = ("", selected[0], selected[1], "")
        if (
            type(model_edit) is CodingModelEditProposal
            and model_edit.message == message
            and model_edit.state is CodingModelEditState.PREPARED
            and type(model_edit.payload) is str
            and selected is not None
        ):
            selection = ("", selected[0], selected[1], model_edit.payload)
    if selection and storage is not None and conversation_id and not attachments:
        try:
            restored = load_coding_revision(
                storage,
                storage.settings.files_dir,
                person_id=person_id,
                tenant_id=actor.user_id,
                conversation_id=conversation_id,
                message_id=selection[1],
                revision_sha256=selection[2],
            )
        except CodingRevisionUnavailable:
            pass
        else:
            project_id = restored.project_id
            members = [
                {
                    "relative_path": name,
                    "size": len(body),
                    "file_kind": "regular_file",
                    "executable": False,
                    "link_kind": "none",
                }
                for name, body in restored.members
            ]
    if editing and restored is not None and selection is not None:
        with suppress(ValueError, TypeError, RecursionError):
            candidate, edit_targets = prepare_revision_edit(restored, selection[3])
    snapshot = restored.revision_sha256 if restored is not None else _snapshot_sha256(members)
    report = build_coding_inspect_report(report_id, turn_id, members=members)
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
        project_id=project_id,
        revision_selector=snapshot,
        boundary=boundary,
    )
    worker_admitted = admission.admission is CodingWorkerAdmissionState.ADMITTED
    creating = not revision_operation and create_requested(message, has_members=bool(members))
    modifying = editing or (not restoring and modify_requested(message, has_members=bool(members)))
    if creating:
        intent = build_coding_mode_intent(f"{turn_id}-intent", turn_id, prompt=message)
    elif modifying:
        intent = build_coding_mode_intent(f"{turn_id}-intent", turn_id, upload=True)
    else:
        intent = build_coding_mode_intent(f"{turn_id}-intent", turn_id, inspect=True)
    execute_claimed, execute_claim = _compose_execute_claim(
        turn_id=turn_id,
        intent=intent,
        message="" if revision_operation else message,
        admission=admission,
    )
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    export_path = Path(boundary.worker_root) / boundary.export_path
    if restoring and restored is not None and worker_admitted:
        try:
            publish_members(workspace, list(restored.members))
        except (OSError, ValueError):
            pass
        else:
            restore_state = "restored"
    created = observe_coding_create(
        turn_id=turn_id,
        project_id=project_id,
        message="" if revision_operation else message,
        workspace=workspace,
        worker_admitted=worker_admitted,
        has_members=bool(members),
    )
    modified = observe_coding_upload_modification(
        turn_id=turn_id,
        project_id=project_id,
        revision_selector=snapshot,
        message="edit" if editing and candidate is not None else "" if revision_operation else message,
        workspace=workspace,
        inspect_report=report,
        members=members,
        creating=creating,
        target_paths=edit_targets,
    )
    if editing and candidate is not None:
        edit_gate = build_coding_mode_plan_gate(
            f"{turn_id}-edit-gate",
            turn_id,
            intent,
            execute_claim,
            modification_admission=modified.admission,
            worker_admission=admission,
        )
        if edit_gate.gate.value == "modify":
            try:
                publish_members(workspace, list(candidate.members))
            except (OSError, ValueError):
                pass
            else:
                edit_state = "written"
                modified = replace(modified, applied=True)
                members = [
                    {
                        "relative_path": name,
                        "size": len(body),
                        "file_kind": "regular_file",
                        "executable": False,
                        "link_kind": "none",
                    }
                    for name, body in candidate.members
                ]
                report = build_coding_inspect_report(report_id, turn_id, members=members)
    if created.state is CodingCreateObserveState.WRITTEN:
        written_members: list[dict[str, object]] = []
        for name in created.files:
            path = workspace / name
            size = path.stat().st_size if path.is_file() else 0
            written_members.append(
                {
                    "relative_path": name,
                    "size": size,
                    "file_kind": "regular_file",
                    "executable": False,
                    "link_kind": "none",
                }
            )
        members = written_members
        report = build_coding_inspect_report(report_id, turn_id, members=members)
    spawn = CodingWorkerSpawnV1(False, admission.admission, "skipped", False)
    if execute_claimed and worker_admitted:
        spawn = spawn_coding_worker(admission, boundary, runner=spawn_runner)
    extract = CodingArchiveExtractObserveV1(
        CodingArchiveExtractObserveState.EMPTY,
        CodingArchiveExtractObserveReason.NO_ARCHIVE,
        0,
        False,
    )
    if execute_claim.claim is CodingModeExecuteClaimState.EXECUTE_CLAIMED:
        extract = observe_coding_archive_extract(
            extract_id="coding-x-" + secrets.token_hex(8),
            authenticated_turn_id=turn_id,
            workspace=workspace,
            raw=first_archive_bytes(attachments),
        )
    loop = CodingIsolatedLoopV1(
        CodingIsolatedLoopState.EMPTY,
        CodingIsolatedLoopReason.NO_WORKSPACE,
        False,
    )
    if execute_claim.claim is CodingModeExecuteClaimState.EXECUTE_CLAIMED:
        operation = execute_claim.operation or CodingModeExecuteOperation.EXECUTE
        loop = observe_coding_isolated_loop(
            admission=admission,
            boundary=boundary,
            spawn=spawn,
            extract=extract,
            operation=operation,
            runner=spawn_runner,
            created=created,
        )
    ready = (
        created.state is CodingCreateObserveState.WRITTEN
        or extract.state is CodingArchiveExtractObserveState.EXTRACTED
        or restore_state == "restored"
        or edit_state == "written"
    )
    result_archive = observe_coding_result_archive(
        turn_id=turn_id,
        workspace=workspace,
        export_path=export_path,
        ready=ready,
        expected_revision=(
            candidate.revision_sha256
            if editing and candidate is not None
            else restored.revision_sha256
            if restored is not None
            else created.identity.revision_selector
            if created.state is CodingCreateObserveState.WRITTEN
            else None
        ),
    )
    create_admission = created.admission if creating else None
    modification_admission = modified.admission if modifying else None
    plan_gate = build_coding_mode_plan_gate(
        f"{turn_id}-gate",
        turn_id,
        intent,
        execute_claim,
        create_admission=create_admission,
        modification_admission=modification_admission,
        worker_admission=admission,
    )
    source_record = revision_record(project_id, result_archive)
    snapshot_members = dict(result_archive.source_digests) if source_record else None
    snapshot_view = (
        build_coding_mode_snapshot(f"{turn_id}-snap", turn_id, snapshot_members)
        if snapshot_members
        else build_coding_mode_snapshot(f"{turn_id}-snap", turn_id)
    )
    view = build_coding_mode_view(
        f"{turn_id}-view",
        turn_id,
        intent=intent,
        snapshot=snapshot_view,
        execute_claim=execute_claim,
        plan_gate=plan_gate,
        carrier=result_archive.carrier,
        inspect_report=report,
        worker_admission=admission,
        project_identity=(
            build_coding_project_identity(
                f"{turn_id}-result-ident",
                turn_id,
                project_id=project_id,
                revision_selector=result_archive.source_revision,
            )
            if source_record
            else None
        ),
    )
    text = _russian_inspect_reply(
        report,
        execute_claimed=execute_claimed,
        worker_admitted=worker_admitted,
        extract=extract,
        loop=loop,
        created_state=created.state.value,
        archive_state=result_archive.state.value,
        plan_gate=plan_gate.gate.value,
        modify_state=modified.state.value,
    )
    if restoring:
        if restore_state == "restored" and source_record:
            text = "Исходники указанной ревизии восстановлены в отдельную рабочую папку. Код не исполнялся и не изменялся."
        else:
            restore_state = "blocked"
            text = (
                "Указанная ревизия недоступна или её целостность не подтверждена. "
                "Используй restore <message_id> <sha256> для точного результата из этого Coding-диалога, без вложений. "
                "Другая версия не подставлялась; код не исполнялся."
            )
    if editing:
        if edit_state == "written" and source_record:
            edit_state = "applied"
            text = (
                "Правки применены к отдельной копии указанной ревизии. Новая версия исходников подготовлена; "
                "исходная версия сохранена. Код не исполнялся; сборка и тесты не запускались."
            )
        else:
            edit_state = "blocked"
            text = (
                "Правка ревизии заблокирована: выбор версии, изменения или целостность результата не подтверждены. "
                "Используй edit <message_id> <sha256>, затем с новой строки JSON с replace/add/delete, без вложений. "
                "Исходная версия не менялась; другая версия не подставлялась; код не исполнялся."
            )
    if model_editing:
        if edit_state == "applied":
            text = (
                "Изменения по текстовому заданию подготовлены моделью и применены к отдельной копии "
                "точно выбранной ревизии. Исходная версия сохранена. "
                "Код не исполнялся; сборка и тесты не запускались. Корректность поведения ещё не подтверждена."
            )
        else:
            text = (
                "Изменение по текстовому заданию заблокировано: выбранные исходники, ответ модели "
                "или целостность результата не подтверждены. Используй revise (или доработай) "
                "<message_id> <sha256>, затем с новой строки задание, без вложений. "
                "Исходная версия не менялась; другая модель или ревизия не подставлялась. Код не исполнялся."
            )
    model_metadata = (
        {"coding_model_edit": model_edit.state.value, "coding_model_calls": model_edit.calls}
        if model_editing and type(model_edit) is CodingModelEditProposal and model_edit.message == message
        else {"coding_model_edit": "model_unavailable", "coding_model_calls": 0}
        if model_editing
        else {}
    )
    source_parent = (
        {"message_id": selection[1], "revision_sha256": restored.revision_sha256}
        if source_record and restored is not None and selection is not None
        else None
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
                **model_metadata,
                **({"coding_source_revision": source_record} if source_record else {}),
                **({"coding_source_parent": source_parent} if source_parent else {}),
            },
        )
        assistant_id = assistant.get("id")
    context: dict[str, Any] = {
        "interaction_mode": "coding",
        "coding_inspect_report": report.report.value,
        "coding_inspect_reason": report.reason.value,
        "coding_execution_attempted": loop.untrusted_execute,
        "coding_worker_admission": admission.admission.value,
        "coding_worker_admission_reason": admission.reason.value,
        "coding_worker_spawned": spawn.spawned,
        "coding_worker_probe": spawn.probe,
        "coding_archive_extract": extract.state.value,
        "coding_archive_extract_reason": extract.reason.value,
        "coding_archive_extracted_count": extract.extracted_count,
        "coding_archive_digest": extract.digest_state,
        "coding_archive_overwrite": extract.overwrite_state,
        "coding_loop": loop.state.value,
        "coding_loop_reason": loop.reason.value,
        "coding_loop_untrusted_execute": loop.untrusted_execute,
        "coding_create": created.state.value,
        "coding_create_reason": created.reason.value,
        "coding_upload_modification": modified.state.value,
        "coding_upload_modification_reason": modified.reason.value,
        "coding_upload_applied": modified.applied,
        "coding_plan_gate": plan_gate.gate.value,
        "coding_mode_view": view.state.value,
        "coding_result_archive": result_archive.state.value,
        "coding_revision_restore": restore_state,
        "coding_revision_edit": edit_state,
        **({"coding_source_parent": source_parent} if source_parent else {}),
        **({"coding_source_revision": source_record} if source_record else {}),
        "coding_carrier": result_archive.carrier.carrier.value,
        "coding_result_restart": result_archive.restart_state,
        "coding_result_rollback": result_archive.rollback_state,
        **model_metadata,
        "llm_failed": model_metadata.get("coding_model_edit")
        in {
            "model_unavailable",
            "model_failed",
            "deadline",
            "output_rejected",
        },
    }
    if report.report is not CodingInspectReportState.BLOCKED:
        context["coding_member_count"] = report.member_count
    files = list(result_archive.files)
    return {
        "conversation_id": persisted_id or conversation_id or "",
        "message_id": assistant_id,
        "message": text,
        "message_format": "plain",
        "verified": False,
        "citations": [],
        "tools_used": [],
        "files": files,
        "voice": None,
        "web_evidence_status": "none",
        "web_evidence_scope": "none",
        "web_sources": [],
        "attachment_context_available": False,
        "context": context,
    }
