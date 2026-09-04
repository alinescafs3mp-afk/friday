"""Owner-only trust-root endpoints for semantic-supervisor rollout evidence."""

from __future__ import annotations

import time

from fastapi import APIRouter, Response

from friday.admin_api._deps import (
    HTTPException,
    Request,
    _audit,
    _request_json,
    _require,
    _services,
)
from friday.orchestration.supervisor_contracts import SupervisorMode
from friday.orchestration.supervisor_representative_window_attestation import (
    REPRESENTATIVE_WINDOW_RUNTIME_REFRESH_SEC,
    RepresentativeWindowAttestationError,
    consume_representative_window_attestation,
    issue_representative_window_attestation,
    refresh_representative_window_runtime_admission,
    representative_window_canonical,
    representative_window_current_server_identity,
    validate_representative_window_consume_request,
    validate_representative_window_issue_request,
)

router = APIRouter()


def _owner(request: Request):
    actor = _require(request, "admin.all_data.manage")
    if not actor.is_owner or actor.identity_id != "owner-token":
        raise HTTPException(
            status_code=403,
            detail="Semantic-supervisor witness доступен только владельцу",
        )
    return actor


def _current_identity(request: Request, mode: SupervisorMode) -> dict[str, object]:
    state = _services(request)
    try:
        return representative_window_current_server_identity(
            state.settings,
            state.secondary_brain,
            target_mode=mode,
        )
    except RepresentativeWindowAttestationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Live semantic-supervisor witness сейчас недоступен",
        ) from exc


async def _identity_after_runtime_refresh(
    request: Request,
    mode: SupervisorMode,
) -> dict[str, object]:
    await refresh_representative_window_runtime_admission(
        getattr(_services(request), "secondary_brain", None),
        absolute_deadline_monotonic=time.monotonic() + REPRESENTATIVE_WINDOW_RUNTIME_REFRESH_SEC,
    )
    return _current_identity(request, mode)


@router.post("/semantic-supervisor-witness/issue-representative-window-attestation")
async def issue_semantic_supervisor_representative_window(request: Request) -> Response:
    """Recompute and issue one short-lived, one-use production witness."""

    actor = _owner(request)
    body = await _request_json(request)
    if not validate_representative_window_issue_request(body):
        raise HTTPException(status_code=400, detail="Некорректный representative-window candidate")
    mode = SupervisorMode(body["target_mode"])
    try:
        result = issue_representative_window_attestation(
            _services(request).storage,
            user_id=actor.user_id,
            request_value=body,
            current_server_identity=await _identity_after_runtime_refresh(request, mode),
        )
    except RepresentativeWindowAttestationError as exc:
        raise HTTPException(
            status_code=400,
            detail="Representative-window candidate не подтверждён сервером",
        ) from exc
    _audit(
        request,
        "admin.semantic_supervisor.issue_representative_window_attestation",
        "semantic_supervisor_witness",
        None,
        after={
            "status": "unused",
            "target_mode": mode.value,
            "server_attestation_sha256": result["server_attestation_sha256"],
        },
    )
    return Response(content=representative_window_canonical(result), media_type="application/json")


@router.post("/semantic-supervisor-witness/consume-representative-window-attestation")
async def consume_semantic_supervisor_representative_window(request: Request) -> Response:
    """Atomically burn one exact server-issued witness before operator mutation."""

    actor = _owner(request)
    body = await _request_json(request)
    if not validate_representative_window_consume_request(body):
        raise HTTPException(status_code=400, detail="Некорректный representative-window witness")
    mode = SupervisorMode(body["target_mode"])
    try:
        result = consume_representative_window_attestation(
            _services(request).storage,
            user_id=actor.user_id,
            request_value=body,
            current_server_identity=await _identity_after_runtime_refresh(request, mode),
        )
    except RepresentativeWindowAttestationError as exc:
        raise HTTPException(
            status_code=400,
            detail="Representative-window witness не подтверждён сервером",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail="Representative-window witness уже использован или изменился",
        ) from exc
    _audit(
        request,
        "admin.semantic_supervisor.consume_representative_window_attestation",
        "semantic_supervisor_witness",
        None,
        after={
            "status": "consumed",
            "target_mode": mode.value,
            "server_attestation_sha256": result["server_attestation_sha256"],
            "consume_binding_sha256": result["consume_binding_sha256"],
        },
    )
    return Response(content=representative_window_canonical(result), media_type="application/json")


__all__ = ["router"]
