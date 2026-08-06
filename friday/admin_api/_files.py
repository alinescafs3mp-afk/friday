"""Admin API: uploaded file listing and download.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``friday.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from friday.admin_api._deps import (
    Any,
    HTTPException,
    Query,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _json_value,
    _require,
    _services,
    _target_user,
)
from friday.file_delivery import (
    AuthorizedFileReadError,
    FileRecordUnavailable,
    attachment_content_disposition,
    read_authorized_file,
)
from friday.storage._privacy import _not_private_raw_dependency

router = APIRouter()


@router.get("/files")
async def list_files(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.files.read", target)
    storage = _services(request).storage
    # `, id DESC` because `received_at` is written to second precision, so one upload
    # batch stamps every row identically and the order within it is not promised.
    rows = storage.execute(
        f"""SELECT r.id, r.user_id, r.source_ref, r.metadata_json, r.received_at, r.deleted_at
           FROM raw_objects r WHERE r.user_id=? AND r.content_type='file'
             AND {_not_private_raw_dependency("r")}
           ORDER BY r.received_at DESC, r.id DESC LIMIT ? OFFSET ?""",  # nosec B608
        (target, limit, offset),
    ).fetchall()
    # Deliberately WITHOUT `deleted_at IS NULL`, because the listing above is too:
    # this admin view shows soft-deleted files on purpose, and the user-facing
    # `GET /api/files` filters them. Copying the count from there would make the
    # total SMALLER than the page and stop «Вперёд» early.
    total_row = storage.execute(
        f"""SELECT COUNT(*) AS count FROM raw_objects r
             WHERE r.user_id=? AND r.content_type='file'
               AND {_not_private_raw_dependency("r")}""",  # nosec B608
        (target,),
    ).fetchone()
    items = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _json_value(item.pop("metadata_json", "{}"), {})
        items.append(item)
    return {
        "user_id": target,
        "items": items,
        "count": len(items),
        "total": int(total_row["count"] if total_row else 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/files/{raw_id}/download")
async def download_file(raw_id: str, request: Request, user_id: str):
    _require(request, "admin.all_data.read")
    state = _services(request)
    try:
        stored = await run_in_threadpool(
            read_authorized_file,
            state.storage,
            state.settings.files_dir,
            raw_id,
            user_id,
            include_deleted=True,
        )
    except (FileRecordUnavailable, AuthorizedFileReadError):
        raise HTTPException(status_code=404, detail="Файл не найден") from None
    # Data egress is always audited, unlike ordinary list reads.
    _audit(
        request,
        "admin.file.download",
        "raw_object",
        raw_id,
        after={"target_user_id": user_id, "filename": stored.filename},
    )
    return Response(
        content=stored.content,
        media_type=stored.mime_type,
        headers={"Content-Disposition": attachment_content_disposition(stored.filename)},
    )
