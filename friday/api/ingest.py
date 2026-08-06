"""HTTP routes for ingestion.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from friday.api.deps import _audit, _parse_json_bool, _request_json, _require
from friday.api.projections import public_ingestion_receipt

router = APIRouter(prefix="/api/ingest", tags=["ingestion"])


@router.post("", tags=["knowledge"])
async def ingest(request: Request) -> dict[str, Any]:
    actor = _require(request, "knowledge.create")
    body = await _request_json(request)
    content = str(body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Нужен content")
    force_knowledge = _parse_json_bool(body.get("force_knowledge"), field="force_knowledge", default=False)
    outcome = await request.app.state.ingestion.ingest_text(
        actor.user_id,
        content,
        source="api",
        source_ref=str(body.get("source_ref") or ""),
        force_knowledge=force_knowledge,
        # Кто принёс материал: единый ключ на всех дорогах приёма.
        metadata={
            **(dict(given) if isinstance(given := body.get("metadata"), dict) else {}),
            "uploaded_by": actor.own_id,
        },
    )
    return public_ingestion_receipt(
        outcome,
        include_resource_id=True,
        storage=request.app.state.storage,
        resource_user_id=actor.user_id,
        resource_owner_id=actor.own_id,
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
        raise HTTPException(status_code=400, detail="Нужен url")
    result = await request.app.state.web_surfer.fetch(url)
    if result.error or not result.text.strip():
        # fetch() never raises — SSRF blocks, non-2xx and empty pages all
        # surface as an error string; ingesting empty text is refused.
        raise HTTPException(
            status_code=422,
            detail=f"Не удалось получить читаемую страницу: {result.error or 'пустой текст'}",
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
        # `force_review` is what makes the comment above true. Without it the default
        # (non-strict) policy auto-promotes anything the classifier finds
        # substantial, and a real fetched page — headings, dates, names, contacts —
        # is exactly that: measured, an article-shaped body returns promoted=True and
        # a Knowledge Object with no human in the loop. That contradicts
        # ARCHITECTURE §3 ("Knowledge Object только после review") and this route's
        # own comment. Third path found bypassing this gate, after
        # bulk_classify_inbox and the disk importer; both were closed the same way.
        force_review=True,
        metadata={
            "url": result.url,
            "title": title,
            "status_code": result.status_code,
            "content_source": "web_fetch",
            # В общем архиве арендатор один на всех; автора называет учётка
            # аутентифицированного человека, а не tenant строки.
            "uploaded_by": actor.own_id,
            # Неполнота — свойство сохраняемого объекта, а не подробность ответа.
            # Провенанс указывает на ПОЛНЫЙ адрес, поэтому без этой строки ревьюер
            # (и всё, что придёт потом: поиск, модель, повторный разбор) принимает
            # первые страницы за весь документ. Причина обрыва любая — срок разбора,
            # потолок знаков, потолок страниц.
            **({"content_truncated": True} if result.truncated else {}),
        },
    )
    _audit(
        request,
        "knowledge.ingest_url",
        "raw_object",
        outcome.get("raw_object_id"),
        after={"url": result.url},
    )
    return {
        **public_ingestion_receipt(
            outcome,
            include_resource_id=True,
            storage=request.app.state.storage,
            resource_user_id=actor.user_id,
            resource_owner_id=actor.own_id,
        ),
        "url": result.url,
        "title": title,
    }
