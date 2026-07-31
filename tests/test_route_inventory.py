"""The HTTP surface is a contract; moving code must not move the contract.

`create_app` grew into a 1295-line function with 51 routes declared as nested
closures, which is what makes splitting it risky: a route that silently fails to
mount, changes method, or loses its path breaks clients, and no unit test aimed at a
*different* endpoint would notice.

The surface is read from the OpenAPI schema, NOT from ``app.routes``: on FastAPI
0.139 routes added via ``include_router`` do not appear in ``app.routes`` at all, so
introspecting that attribute silently misses every router-mounted endpoint — 67 of
them here, including the whole admin API. The schema is what clients actually see.

The count is asserted rather than the full list: the list lives in the schema, and a
second copy of 134 operation strings would rot. What must never happen silently is an
endpoint appearing or vanishing.

Counting is not enough once a router is split across modules, because the schema is
order-blind and FastAPI is not: the *order* the modules are included in decides which
handler answers. So there are two further checks here — that no literal path is
shadowed by an earlier parameterized one, and that every admin module actually answers
a live request.
"""

from __future__ import annotations

from jericho.server import create_app

# Bumped deliberately when an endpoint is added or removed, never to make a test pass.
# 149 → 151: пакетное подтверждение сущностей — GET групп и POST решения
# по группе (одно решение вместо 57 поштучных).
# 151 → 152: GET /api/knowledge/by-date — документы по собственной дате для хроники.
# 152 → 153: GET /api/admin/knowledge/{id}/entity-mentions — позиции упоминаний
# подтверждённых сущностей в тексте, для подсветки в инспекции.
# 153 → 154: GET /api/admin/knowledge/timeline — плотность корпуса по времени
# и лента окна; стоит ПЕРЕД /knowledge/{knowledge_id}, иначе будет проглочен.
# 154 → 156: GET /api/kg/conflicts + POST /api/kg/conflicts/{id}/decide —
# разбор конфликтов знаний из чата (Telegram /conflicts) и инструментов агента.
# 156 → 160: GET+POST /api/kg/merges[/{id}/undo] и /api/admin/merges[/{id}/undo] —
# откат слияния сущностей (#51); transfer set пишется при merge.
# 160 → 161: PATCH /api/me/instructions — короткое пожелание о стиле ответов,
# которое человек задаёт себе сам (Telegram /instructions); self-service, гейт
# chat.use, действует только на свой user_id.
# 161 → 162: POST /api/me/regenerate — «ещё раз» для последнего user-хода
# (Telegram /retry); self-service, chat.use, только свой conversation_id.
# 162 → 163: GET /api/me/messages/search — FTS по истории переписки (G16).
# 163 → 164: PATCH /api/conversations/{id} — self-service rename (G18c).
# 164 → 166: GET /api/me/reminders + POST /api/me/reminders/{id}/dismiss (G19).
# 166 → 167: GET /api/conversations/{id}/export — plain-text transcript (G20).
EXPECTED_OPERATIONS = 167
# Areas that are mounted through include_router, i.e. exactly the ones app.routes
# cannot see. Pinning their sizes catches a router that quietly stops being included.
EXPECTED_BY_PREFIX = {
    "/api/admin": 87,
    "/api/kg": 18,
    "/api/missions": 4,
}


def _surface(settings) -> set[str]:
    schema = create_app(settings).openapi()
    return {
        f"{method.upper()} {path}" for path, operations in schema["paths"].items() for method in operations
    }


def test_http_contract_size_is_pinned(settings):
    surface = _surface(settings)
    assert len(surface) == EXPECTED_OPERATIONS, (
        f"the HTTP surface changed: {len(surface)} operations, expected {EXPECTED_OPERATIONS}. "
        "Update EXPECTED_OPERATIONS only when an endpoint was added or removed on purpose."
    )


def test_router_mounted_areas_are_all_present(settings):
    """These are invisible to app.routes, so nothing else would notice them vanishing."""
    surface = _surface(settings)
    for prefix, expected in EXPECTED_BY_PREFIX.items():
        paths = {item.split(" ", 1)[1] for item in surface if item.split(" ", 1)[1].startswith(prefix)}
        assert len(paths) == expected, f"{prefix}: {len(paths)} paths, expected {expected}"


def _ordered_routes(app) -> list[tuple[str, str, str]]:
    """(method, path, name) in the order FastAPI will try to match them.

    ``include_router`` on 0.139 stores an ``_IncludedRouter`` wrapper rather than
    flattening, so the nested router has to be walked to recover the real order.
    """
    ordered: list[tuple[str, str, str]] = []

    def walk(routes, prefix: str) -> None:
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                context = getattr(route, "include_context", None)
                walk(nested.routes, prefix + getattr(context, "prefix", ""))
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            for method in sorted(getattr(route, "methods", None) or []):
                ordered.append((method, prefix + path, getattr(route, "name", "?")))

    walk(app.routes, "")
    return ordered


def _would_shadow(pattern: str, literal: str) -> bool:
    left, right = pattern.strip("/").split("/"), literal.strip("/").split("/")
    if len(left) != len(right):
        return False
    return all(a.startswith("{") or a == b for a, b in zip(left, right, strict=True))


def test_no_literal_route_is_shadowed_by_an_earlier_pattern(settings):
    """A parameterized path registered first swallows every literal path it matches.

    Verified against this FastAPI (0.139): registration order still decides, for one
    router and across separately included ones alike — the literal does not win by
    being more specific. So splitting a router into modules can silently break an
    endpoint by changing nothing but the order the modules are included in, and the
    endpoint keeps answering 200 from the *wrong* handler, which no status-code check
    would catch. One such pair exists today: ``/api/admin/knowledge/tags`` must stay
    ahead of ``/api/admin/knowledge/{knowledge_id}``.
    """
    ordered = _ordered_routes(create_app(settings))
    shadowed = [
        f"{method} {pattern} ({pattern_name}) shadows {later} ({later_name})"
        for index, (method, pattern, pattern_name) in enumerate(ordered)
        if "{" in pattern
        for later_method, later, later_name in ordered[index + 1 :]
        if later_method == method and "{" not in later and _would_shadow(pattern, later)
    ]
    assert not shadowed, "unreachable endpoints: " + "; ".join(shadowed)


def test_every_admin_module_is_mounted_and_answers(settings):
    """One live GET per admin router module.

    Counting paths catches a module that stops being included; it does not catch one
    that is included but broken, nor a request reaching the wrong handler. These are
    the cheapest endpoints of each module, asserted on the response and not only the
    status code where the distinction matters.
    """
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=headers).json()["items"][0]["id"]
        probes = [
            "/api/admin/overview",  # _overview
            "/api/admin/users",  # _users
            "/api/admin/knowledge",  # _knowledge
            "/api/admin/inbox",  # _inbox
            "/api/admin/entities",  # _graph
            f"/api/admin/conflicts?user_id={user_id}",  # _conflicts
            f"/api/admin/lifecycle?user_id={user_id}",  # _lifecycle
            "/api/admin/conversations",  # _conversations
            "/api/admin/files",  # _files
            "/api/admin/eval/cases",  # _evaluation
            "/api/admin/backups",  # _maintenance
        ]
        failed = [
            (p, r.status_code) for p in probes if (r := client.get(p, headers=headers)).status_code != 200
        ]
        assert not failed, f"admin modules not answering: {failed}"

        # The one order-critical pair, checked by behaviour rather than by route table:
        # `inspect_knowledge` requires a `user_id` query parameter and `tags` does not,
        # so a 200 here means `tags` was served by its own handler. Were it shadowed,
        # this would be a 422 demanding `user_id` — as the unknown id below is.
        assert "items" in client.get("/api/admin/knowledge/tags", headers=headers).json()
        assert client.get("/api/admin/knowledge/no-such-id", headers=headers).status_code == 422


def test_extracted_kg_router_is_reachable(settings):
    """The knowledge-graph routes were lifted out of create_app into their own module;
    they must still answer on the same paths."""
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        assert client.get("/api/kg/stats", headers=headers).status_code == 200
        assert client.get("/api/kg/entities", headers=headers).status_code == 200
