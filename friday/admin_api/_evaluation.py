"""Admin API: retrieval quality: the gold set, ablation and explain.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``friday.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter

from friday.admin_api._deps import (
    Any,
    HTTPException,
    InboxStatus,
    Query,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
    _target_user,
)
from friday.storage._base import _SEARCH_TEXT_LEN_SQL
from friday.storage._privacy import _not_private_knowledge_dependency

router = APIRouter()


def _embedding_index_health(state: Any, user_id: str) -> dict[str, Any]:
    """Tenant-scoped vector freshness counts; never expose rows or model text."""

    backend = state.embeddings
    if not getattr(backend, "remote_enabled", False):
        return {
            "status": "disabled",
            "missing_objects": None,
            "stale_objects": None,
            "freshness": "not_applicable",
        }

    from friday.retrieval import chunk_scheme

    settings = state.settings
    scheme = chunk_scheme(settings)
    row = state.storage.execute(
        f"""SELECT
                  SUM(CASE WHEN e.knowledge_object_id IS NULL THEN 1 ELSE 0 END) AS missing,
                  SUM(CASE WHEN e.knowledge_object_id IS NOT NULL
                            AND (e.model != ? OR e.source_version != k.version
                                 OR (e.chunk_scheme != ? AND {_SEARCH_TEXT_LEN_SQL} > ?))
                           THEN 1 ELSE 0 END) AS stale
               FROM knowledge_objects k
               LEFT JOIN knowledge_embeddings e
                 ON e.knowledge_object_id=k.id AND e.user_id=k.user_id
              WHERE k.user_id=? AND k.deleted_at IS NULL
                AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
        (
            settings.embeddings_model,
            scheme,
            settings.embeddings_chunk_chars,
            user_id,
        ),
    ).fetchone()
    missing = int(row["missing"] or 0) if row else 0
    stale = int(row["stale"] or 0) if row else 0
    return {
        "status": "complete" if missing + stale == 0 else "incomplete",
        "missing_objects": missing,
        "stale_objects": stale,
        "freshness": "measured_from_source_version_and_chunk_scheme",
    }


@router.get("/quality")
async def knowledge_quality_dashboard(
    request: Request,
    user_id: str,
    lifecycle_limit: int = Query(50, ge=1, le=500),
    lifecycle_offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """One read-only view of the feedback loop and graph-review pressure."""

    _require(request, "admin.all_data.read")
    state = _services(request)
    _audit_cross_tenant_read(request, "admin.quality.read", user_id)
    if not state.storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    usage = state.storage.execute(
        f"""SELECT COUNT(*) AS tracked,
                  COALESCE(SUM(retrieval_count), 0) AS retrievals,
                  COALESCE(SUM(answer_count), 0) AS answers,
                  COALESCE(SUM(positive_feedback_count), 0) AS positive,
                  COALESCE(SUM(negative_feedback_count), 0) AS negative
           FROM knowledge_usage u
           JOIN knowledge_objects k
             ON k.id=u.knowledge_object_id AND k.user_id=u.user_id
            AND k.deleted_at IS NULL
            AND {_not_private_knowledge_dependency("k")}
           WHERE u.user_id=?""",  # nosec B608
        (user_id,),
    ).fetchone()

    # Counted, not measured by the length of a truncated page. Each of these tiles used
    # to be `len(list_...(limit=N))`, so a большой number was indistinguishable from the
    # cap — and the inbox one capped at 1000 while asking for 5000. The counters below
    # share their filters with the listings by construction, and they are also faster:
    # the rows themselves are not needed to draw a number (measured 0.24 ms against
    # 5.1 ms, 7.8 against 52.5, 12.6 against 51.3).
    lifecycle_candidates = state.storage.list_lifecycle_candidates(
        user_id, limit=lifecycle_limit, offset=lifecycle_offset
    )
    lifecycle_total = state.storage.count_lifecycle_candidates(user_id)
    return {
        "user_id": user_id,
        "graph": state.kg.get_stats(user_id),
        "usage": dict(usage) if usage else {},
        "feedback": {
            "history": state.storage.get_feedback_stats(user_id),
            # Counted, not measured by the length of a page capped at 5000. `score` is
            # REAL NOT NULL between -1 and 1, so the SQL predicate and the python it
            # replaces select the same rows.
            "current_count": state.storage.count_feedback_state(user_id),
            "classification_current": state.storage.count_feedback_state(
                user_id, feedback_type="classification"
            ),
            "classification_negative": state.storage.count_feedback_state(
                user_id, feedback_type="classification", negative_only=True
            ),
        },
        "review_pressure": {
            "pending_inbox": state.storage.count_inbox(user_id, InboxStatus.PENDING),
            "relation_candidates": state.storage.count_relation_candidates(user_id, status="suggested"),
            "conflicts": state.storage.count_knowledge_conflicts(user_id, status="suggested"),
            # The dirtiest of the four, and not ordinary saturation: the listing takes
            # 500 rows in `importance ASC` order and only THEN filters in python, so
            # protected file-derived objects at importance 0 eat the limit first. The
            # number therefore saturates BELOW the limit and looks like a real count —
            # measured, 900 true candidates showed as 200.
            "lifecycle_candidates": lifecycle_total,
        },
        "lifecycle_candidates": lifecycle_candidates,
        # The table pages now, so «Выбрать все» stops reading as «все кандидаты»:
        # «1-50 из 900» stands next to it.
        "lifecycle_total": lifecycle_total,
        "lifecycle_limit": lifecycle_limit,
        "lifecycle_offset": lifecycle_offset,
    }


@router.get("/eval/search")
async def eval_search(
    request: Request, q: str, user_id: str | None = None, limit: int = Query(15, ge=1, le=50)
) -> dict[str, Any]:
    from friday.retrieval import is_relational_query

    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.eval.read", target)
    state = _services(request)
    # Режим ТОТ ЖЕ, что у агента и у метрики: иначе список, которым проверяют
    # качество, относится к другому поиску. `record_usage=False` — не украшение:
    # счётчик обращений читается обратно ранжированием, и диагностика двигала бы
    # ту самую выдачу, которую показывает. Оба правила уже записаны у соседнего
    # `retrieval_explain`; здесь дорога осталась без них.
    result = await state.hybrid_searcher.search(
        target,
        q,
        limit=limit,
        kg=state.kg,
        graph_expansion=is_relational_query(q),
        record_usage=False,
    )
    items = [
        {
            "id": hit.get("id"),
            "title": hit.get("title") or "Без названия",
            "knowledge_kind": hit.get("knowledge_kind"),
            "lifecycle_stage": hit.get("lifecycle_stage"),
        }
        for hit in result.get("results", [])
        if hit.get("id")
    ]
    return {"user_id": target, "query": q, "items": items, "count": len(items)}


@router.get("/retrieval/explain")
async def retrieval_explain(
    request: Request,
    q: str,
    user_id: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    corpora: str = Query("knowledge", min_length=1, max_length=96),
    date_role: str = Query("document_or_mentioned_date", min_length=1, max_length=48),
    since: str | None = Query(None, max_length=10),
    until: str | None = Query(None, max_length=10),
) -> dict[str, Any]:
    """Explain-trace: why the ranker returned/discarded/ordered candidates for a
    query. Read-only, deterministic (no LLM) — the same HybridSearcher run the
    user sees, with the per-signal breakdown and discard reasons surfaced."""
    from friday.retrieval import is_relational_query
    from friday.retrieval.search_explain import (
        SEARCH_EXPLAIN_DATE_ROLES,
        build_search_explain_projection,
        parse_search_explain_corpora,
        validate_search_explain_date_range,
    )

    _require(request, "admin.all_data.read")
    try:
        selected_corpora = parse_search_explain_corpora(corpora)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    normalized_date_role = date_role.strip().casefold()
    if normalized_date_role not in SEARCH_EXPLAIN_DATE_ROLES:
        raise HTTPException(status_code=400, detail="date_role_unknown")
    try:
        normalized_date_role, since, until = validate_search_explain_date_range(
            normalized_date_role,
            since,
            until,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.eval.read", target)
    state = _services(request)
    if not state.storage.get_user(target):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    graph_selected = "knowledge" in selected_corpora and is_relational_query(q)
    # `record_usage=False` — не украшение: без него объявленный здесь read-only
    # писал счётчик обращений, который ранжирование читает обратно, и диагностика
    # меняла ту самую выдачу, которую показывает.
    # Режим ТОТ ЖЕ, что у человека и у замера, иначе объяснение относится к другому
    # поиску. `memory_search` — путь, которым ходит модель, — зовёт поиск БЕЗ `kg`;
    # `run_eval` включает граф только для relational-запросов, и на этом наборе он
    # не включается ни разу. А здесь граф стоял по умолчанию, и диагностика
    # показывала документы-концентраторы с графовым вкладом 0.744, которого в
    # боевой выдаче нет: «Судимости.docx» обгонял нужный листок ровно на эту
    # величину — в объяснении, но не в жизни.
    if "knowledge" in selected_corpora:
        result = await state.hybrid_searcher.search(
            target,
            q,
            limit=limit,
            kg=state.kg,
            graph_expansion=graph_selected,
            explain=True,
            since=since,
            until=until,
            record_usage=False,
        )
        authorized_knowledge_objects: int | None = state.storage.count_knowledge_objects(target)
        embedding_index = _embedding_index_health(state, target)
        fts_available = bool(getattr(state.storage, "_fts_available", False))
    else:
        # Documents/messages do not yet share this retrieval contour.  Returning
        # an explicit unavailable scope is safer than silently searching Knowledge
        # and presenting its empty page as proof that those corpora contain nothing.
        result = {
            "query": q,
            "results": [],
            "count": 0,
            "matched_at_least": 0,
            "trace": [],
            "strategy": {},
        }
        authorized_knowledge_objects = None
        embedding_index = {
            "status": "not_inspected",
            "missing_objects": None,
            "stale_objects": None,
            "freshness": "not_inspected",
        }
        fts_available = False
    trace = result.get("trace", [])
    discarded = [row for row in trace if row.get("status") == "discarded"]
    search_explain = build_search_explain_projection(
        result,
        selected_corpora=selected_corpora,
        authorized_knowledge_objects=authorized_knowledge_objects,
        date_role=normalized_date_role,
        since=since,
        until=until,
        graph_selected=graph_selected,
        fts_available=fts_available,
        embedding_index=embedding_index,
        result_limit=limit,
        dense_object_cap=state.settings.embeddings_dense_max_objects,
    )
    return {
        "user_id": target,
        "query": result.get("query", q),
        "limit": limit,
        "returned": result.get("count", 0),
        "candidates": len(trace),
        "discarded": len(discarded),
        "trace": trace,
        "strategy": result.get("strategy", {}),
        "search_explain": search_explain,
    }


@router.get("/eval/cases")
async def list_eval_cases(request: Request, user_id: str | None = None) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.eval.read", target)
    cases = _services(request).storage.list_eval_cases(target)
    return {"user_id": target, "items": cases, "count": len(cases)}


@router.post("/eval/cases")
async def add_eval_case(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    _protect_owner_target(request, target)
    expected = body.get("expected_ids")
    if not isinstance(expected, list):
        raise HTTPException(status_code=400, detail="expected_ids должен быть списком")
    try:
        case = _services(request).storage.add_eval_case(
            target,
            str(body.get("query") or ""),
            [str(item) for item in expected],
            note=str(body.get("note") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.eval.case_add", "eval_case", case.get("id"), after=case)
    return {"case": case}


@router.delete("/eval/cases/{case_id}")
async def delete_eval_case(case_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    _protect_owner_target(request, user_id)
    if not _services(request).storage.delete_eval_case(user_id, case_id):
        raise HTTPException(status_code=404, detail="Эталонный запрос не найден")
    _audit(request, "admin.eval.case_delete", "eval_case", case_id)
    return {"status": "deleted"}


@router.post("/eval/run")
async def run_eval_now(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    from friday.eval import run_eval

    state = _services(request)
    # Same k as the worker uses, so a manual run cannot look like a regression purely
    # because it measured a different metric than the stored baseline.
    report = await run_eval(state.storage, state.embeddings, state.settings, target, k=state.settings.eval_k)
    _audit(
        request,
        "admin.eval.run",
        "user",
        target,
        after={"recall_at_k": report.get("recall_at_k"), "cases": report.get("cases")},
    )
    return {"user_id": target, "report": report}


@router.post("/eval/ablation")
async def measure_signal_ablation(request: Request) -> dict[str, Any]:
    """Measure what each ranking weight earns, by switching it off on the gold set."""
    _require(request, "admin.all_data.read")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    from friday.eval import compare_signal_ablation

    state = _services(request)
    requested = body.get("signals")
    report = await compare_signal_ablation(
        state.storage,
        state.embeddings,
        state.settings,
        target,
        k=state.settings.eval_k,
        signals=[str(item) for item in requested] if isinstance(requested, list) else None,
    )
    _audit(
        request,
        "admin.eval.ablation",
        "user",
        target,
        after={"cases": report.get("cases"), "ranked": report.get("ranked")},
    )
    return {"user_id": target, "report": report}


@router.post("/eval/chunk-ab")
async def compare_chunk_recall_now(request: Request) -> dict[str, Any]:
    """A/B the gold set with and without passage-level recall on THIS corpus."""
    _require(request, "admin.all_data.read")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    from friday.eval import compare_chunk_recall

    state = _services(request)
    report = await compare_chunk_recall(state.storage, state.embeddings, state.settings, target)
    _audit(
        request,
        "admin.eval.chunk_ab",
        "user",
        target,
        after={"delta": report.get("delta"), "cases": report.get("cases")},
    )
    return {"user_id": target, "report": report}


@router.get("/feedback")
async def feedback_stats(request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.feedback.read", user_id)
    storage = _services(request).storage
    return {
        "user_id": user_id,
        "stats": storage.get_feedback_stats(user_id),
        "current": storage.get_feedback_state(user_id, limit=1000),
    }
