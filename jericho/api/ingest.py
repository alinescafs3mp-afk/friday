"""HTTP routes for ingestion.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from jericho.api.deps import _audit, _parse_json_bool, _request_json, _require

router = APIRouter(prefix="/api/ingest", tags=["ingestion"])


@router.post("", tags=["knowledge"])
async def ingest(request: Request) -> dict[str, Any]:
    actor = _require(request, "knowledge.create")
    body = await _request_json(request)
    content = str(body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    force_knowledge = _parse_json_bool(body.get("force_knowledge"), field="force_knowledge", default=False)
    return await request.app.state.ingestion.ingest_text(
        actor.user_id,
        content,
        source="api",
        source_ref=str(body.get("source_ref") or ""),
        force_knowledge=force_knowledge,
        metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
    )


@router.post("/url", tags=["knowledge"])
async def ingest_url(request: Request) -> dict[str, Any]:
    # Fetching the public web needs web.fetch; turning it into knowledge
    # needs knowledge.create. Both are enforced before anything happens.
    actor = _require(request, "web.fetch")
    request.app.state.auth_service.require(actor, "knowledge.create")
    body = await _request_json(request)
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    result = await request.app.state.web_surfer.fetch(url)
    if result.error or not result.text.strip():
        # fetch() never raises — SSRF blocks, non-2xx and empty pages all
        # surface as an error string; ingesting empty text is refused.
        raise HTTPException(
            status_code=422,
            detail=f"Could not fetch a readable page: {result.error or 'empty content'}",
        )
    title = result.title or result.url
    # The page is captured as a Raw Object and routed through the Inbox like
    # any other material — it becomes a retrievable Knowledge Object only
    # after review, never silently.
    outcome = await request.app.state.ingestion.ingest_text(
        actor.user_id,
        result.text,
        source="web",
        source_ref=result.url,
        metadata={
            "url": result.url,
            "title": title,
            "status_code": result.status_code,
            "content_source": "web_fetch",
        },
    )
    _audit(
        request,
        "knowledge.ingest_url",
        "raw_object",
        outcome.get("raw_object_id"),
        after={"url": result.url},
    )
    return {**outcome, "url": result.url, "title": title}
