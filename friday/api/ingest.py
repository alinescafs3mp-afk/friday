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
from friday.diagnostics.runtime_lease import ProcessLease, RuntimeLeaseError
from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionState,
    build_web_research_consumption,
)
from friday.secondary_product_witness import (
    SECONDARY_PRODUCT_BACKUP_LEASE_FILENAME,
    SECONDARY_PRODUCT_BACKUP_LEASE_PROTOCOL,
    SECONDARY_PRODUCT_WITNESS_SOURCE_PREFIX,
    is_secondary_product_witness_raw,
    parse_secondary_product_witness_source_ref,
    secondary_product_storage_binding,
    secondary_product_witness_content,
)

router = APIRouter(prefix="/api/ingest", tags=["ingestion"])


@router.post("", tags=["knowledge"])
async def ingest(request: Request) -> dict[str, Any]:
    actor = _require(request, "knowledge.create")
    body = await _request_json(request)
    content = str(body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Нужен content")
    force_knowledge = _parse_json_bool(body.get("force_knowledge"), field="force_knowledge", default=False)
    force_review = _parse_json_bool(body.get("force_review"), field="force_review", default=False)
    if force_knowledge and force_review:
        raise HTTPException(
            status_code=400,
            detail="force_knowledge и force_review взаимоисключающие",
        )
    source_ref = str(body.get("source_ref") or "")
    metadata = {
        **(dict(given) if isinstance(given := body.get("metadata"), dict) else {}),
        "uploaded_by": actor.own_id,
    }
    reserved_witness = source_ref.startswith(SECONDARY_PRODUCT_WITNESS_SOURCE_PREFIX) or (
        "secondary_product_witness" in metadata
    )
    if reserved_witness:
        parsed_witness = parse_secondary_product_witness_source_ref(source_ref)
        if (
            metadata.get("secondary_product_witness") is not True
            or parsed_witness is None
            or content != secondary_product_witness_content(*parsed_witness)
            or force_review is not True
            or force_knowledge is not False
        ):
            raise HTTPException(status_code=400, detail="Некорректный secondary product witness")
        if not actor.is_owner or actor.identity_id != "owner-token":
            raise HTTPException(status_code=403, detail="Secondary product witness доступен только владельцу")
        request.app.state.auth_service.require(actor, "admin.all_data.manage")
    elif force_review:
        raise HTTPException(status_code=400, detail="force_review зарезервирован для product witness")

    boundary: ProcessLease | None = None
    if reserved_witness:
        try:
            boundary = ProcessLease(
                request.app.state.settings.state_dir / SECONDARY_PRODUCT_BACKUP_LEASE_FILENAME,
                protocol=SECONDARY_PRODUCT_BACKUP_LEASE_PROTOCOL,
            )
            boundary.acquire()
        except RuntimeLeaseError as exc:
            raise HTTPException(
                status_code=503,
                detail="Secondary product witness временно заблокирован снимком базы",
            ) from exc
    try:
        outcome = await request.app.state.ingestion.ingest_text(
            actor.user_id,
            content,
            source="api",
            source_ref=source_ref,
            force_knowledge=force_knowledge,
            force_review=force_review,
            # Кто принёс материал: единый ключ на всех дорогах приёма.
            metadata={**metadata, "uploaded_by": actor.own_id},
        )
    finally:
        if boundary is not None:
            boundary.release()
    receipt = public_ingestion_receipt(
        outcome,
        include_resource_id=True,
        storage=request.app.state.storage,
        resource_user_id=actor.user_id,
        resource_owner_id=actor.own_id,
    )
    if reserved_witness:
        raw = request.app.state.storage.get_raw_object(str(outcome.get("raw_object_id") or ""), actor.user_id)
        inbox = request.app.state.storage.get_inbox_item(str(outcome.get("inbox_id") or ""), actor.user_id)
        if (
            is_secondary_product_witness_raw(raw)
            and inbox
            and inbox.get("status") == "pending"
            and inbox.get("knowledge_object_id") is None
        ):
            # A POST may commit and lose its response.  Replaying the exact reserved
            # source_ref must recover the same cleanable pending receipt, not the
            # sparse generic idempotency projection.
            receipt.update(
                queued_for_review=True,
                promoted=False,
                persisted=True,
                action="review",
            )
            receipt["secondary_product_storage_binding_sha256"] = secondary_product_storage_binding(
                raw, inbox
            )
            receipt["secondary_product_storage_user_id"] = actor.user_id
    return receipt


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
    consumption = build_web_research_consumption(
        "ingest.url",
        "ingest.url",
        WebCurrentnessDecision.SEARCH_REQUIRED,
        None,
        source_urls=(url,),
        topic="",
    )
    if consumption.usability is WebResearchConsumptionState.BLOCKED_PRIVATE:
        raise HTTPException(
            status_code=422,
            detail="Не удалось получить читаемую страницу: source_fact_private",
        )
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
