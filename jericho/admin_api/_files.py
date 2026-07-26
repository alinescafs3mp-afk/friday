"""Admin API: uploaded file listing and download.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``jericho.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter

from jericho.admin_api._deps import (
    Any,
    FileResponse,
    HTTPException,
    Query,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _json_value,
    _require,
    _safe_runtime_file,
    _services,
    _target_user,
)

router = APIRouter()


@router.get("/files")
async def list_files(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.files.read", target)
    rows = (
        _services(request)
        .storage.execute(
            """SELECT id, user_id, source_ref, metadata_json, received_at, deleted_at
           FROM raw_objects WHERE user_id=? AND content_type='file'
           ORDER BY received_at DESC LIMIT ?""",
            (target, limit),
        )
        .fetchall()
    )
    items = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _json_value(item.pop("metadata_json", "{}"), {})
        items.append(item)
    return {"user_id": target, "items": items, "count": len(items)}


@router.get("/files/{raw_id}/download")
async def download_file(raw_id: str, request: Request, user_id: str):
    _require(request, "admin.all_data.read")
    state = _services(request)
    raw = state.storage.get_raw_object(raw_id, user_id)
    if not raw or raw.get("content_type") != "file":
        raise HTTPException(status_code=404, detail="File not found")
    metadata = _json_value(raw.get("metadata_json"), {})
    path = _safe_runtime_file(state.settings.files_dir, str(metadata.get("stored_path") or ""))
    # Data egress is always audited, unlike ordinary list reads.
    _audit(
        request,
        "admin.file.download",
        "raw_object",
        raw_id,
        after={"target_user_id": user_id, "filename": str(metadata.get("filename") or "")},
    )
    return FileResponse(path, filename=str(metadata.get("filename") or path.name))
