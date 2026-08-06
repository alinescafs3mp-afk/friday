"""HTTP routes for files.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from friday.api.deps import _audit, _json_load, _require
from friday.file_delivery import (
    AuthorizedFileReadError,
    FileRecordUnavailable,
    attachment_content_disposition,
    read_authorized_file,
)
from friday.storage._privacy import _not_private_raw_dependency

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
        metadata={"uploaded_via": "api", "uploaded_by": actor.own_id},
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
        f"""SELECT r.id, r.source_ref, r.metadata_json, r.received_at, r.deleted_at
           FROM raw_objects r
           WHERE r.user_id=? AND r.content_type='file' AND r.deleted_at IS NULL
             AND {_not_private_raw_dependency("r")}
           ORDER BY r.received_at DESC LIMIT ?""",  # nosec B608
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
    try:
        stored = await run_in_threadpool(
            read_authorized_file,
            state.storage,
            state.settings.files_dir,
            raw_id,
            actor.user_id,
        )
    except (FileRecordUnavailable, AuthorizedFileReadError):
        raise HTTPException(status_code=404, detail="Файл не найден") from None
    # File bytes leaving the system are always audited.
    _audit(
        request,
        "file.download",
        "raw_object",
        raw_id,
        after={"filename": stored.filename},
    )
    return Response(
        content=stored.content,
        media_type=stored.mime_type,
        headers={"Content-Disposition": attachment_content_disposition(stored.filename)},
    )
