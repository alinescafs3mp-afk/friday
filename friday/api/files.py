"""HTTP routes for files.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from friday.api.deps import _audit, _json_load, _require, _safe_owned_file

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("", tags=["files"])
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    source_ref: str = Form(""),
) -> dict[str, Any]:
    actor = _require(request, "files.upload")
    content = await file.read(request.app.state.settings.max_upload_bytes + 1)
    if len(content) > request.app.state.settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Файл слишком большой")
    result = await request.app.state.ingestion.ingest_file(
        actor.user_id,
        None,
        content,
        filename=file.filename or "upload.bin",
        mime_type=file.content_type or "application/octet-stream",
        source_ref=source_ref,
        metadata={"uploaded_via": "api"},
    )
    _audit(
        request,
        "file.upload",
        "raw_object",
        result.get("raw_object_id"),
        after={"filename": file.filename, "size_bytes": len(content)},
    )
    return result


@router.get("", tags=["files"])
async def list_files(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    actor = _require(request, "files.read")
    rows = request.app.state.storage.execute(
        """SELECT id, source_ref, metadata_json, received_at, deleted_at
           FROM raw_objects
           WHERE user_id=? AND content_type='file' AND deleted_at IS NULL
           ORDER BY received_at DESC LIMIT ?""",
        (actor.user_id, limit),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _json_load(item.pop("metadata_json", "{}"), {})
        items.append(item)
    return {"items": items, "count": len(items)}


@router.get("/{raw_id}", tags=["files"])
async def download_file(raw_id: str, request: Request):
    actor = _require(request, "files.read")
    state = request.app.state
    raw = state.storage.get_raw_object(raw_id, actor.user_id)
    if not raw or raw.get("content_type") != "file" or raw.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Файл не найден")
    metadata = _json_load(raw.get("metadata_json"), {})
    path = _safe_owned_file(state.settings.files_dir, str(metadata.get("stored_path") or ""))
    # File bytes leaving the system are always audited.
    _audit(
        request,
        "file.download",
        "raw_object",
        raw_id,
        after={"filename": str(metadata.get("filename") or "")},
    )
    return FileResponse(path, filename=str(metadata.get("filename") or path.name))
