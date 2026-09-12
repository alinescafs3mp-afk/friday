"""Activity, graph and timeline in owned Chromium with real response/write faults."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, Relation
from tests.release_1_0_ui_support import (
    ALICE,
    ALICE_NAME,
    BORIS,
    KNOWLEDGE_TITLE,
    app_text,
    click_expect_response,
    dashboard_stat_map,
)
from tests.test_release_1_0_knowledge_read_oracles import _audits, _read_state

pytest_plugins = ["tests.release_1_0_ui_support"]

ROOT_NODE, PEER_NODE, ORG_NODE = "ent-ui-three-root", "ent-ui-three-peer", "ent-ui-three-org"
FOREIGN_NODE, FOREIGN_PEER = "ent-ui-three-foreign", "ent-ui-three-foreign-peer"
REL, FOREIGN_REL = "rel-ui-three-own", "rel-ui-three-foreign"
OWN_NAMES = {ROOT_NODE: "Первый узел", PEER_NODE: "Второй узел", ORG_NODE: "Склад графа"}
SINCE = "2030-07-25T00:00:00.000Z"
NEW_RAW, OLD_RAW, TEXT_RAW, FOREIGN_RAW = (
    "raw-ui-three-" + name for name in ("new", "old", "text", "foreign")
)
NEW_TEXT, TEXT = "Файл: приборы прибыли", "Сообщение: поверка склада"
OLD_KO, NOV_KO, UNDATED_KO, FOREIGN_KO = (
    "ko-ui-three-" + name for name in ("2023", "november", "undated", "foreign")
)


def _equal(actual, expected, code):
    assert actual == expected, (code, actual, expected)


def _snapshot(ui):
    state = _read_state({"storage": ui.storage})
    for table in ("messages", "conversations", "outbound_notifications"):
        state[table] = [
            dict(r) for r in ui.storage.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        ]
    return state


def _finish(ui, before, audit_before, wanted):
    current = _snapshot(ui)
    expected = deepcopy(before)
    # The legacy owner token calls ensure_user on every real HTTP request.
    # Its two activity timestamps may advance within the server-time interval;
    # every other owner field and every other person's whole row stays exact.
    owner = next(r for r in current["users"] if r["id"] == LEGACY_OWNER_USER_ID)
    old_owner = next(r for r in expected["users"] if r["id"] == LEGACY_OWNER_USER_ID)
    _equal(owner["updated_at"], owner["last_seen_at"], "ui_three_owner_auth_time_pair")
    for field in ("updated_at", "last_seen_at"):
        assert (
            datetime.fromisoformat(old_owner[field])
            <= datetime.fromisoformat(owner[field])
            <= datetime.now(UTC)
        ), "ui_three_owner_auth_time_range"
        old_owner[field] = owner[field]
    differences = {
        table: (current[table], expected[table]) for table in expected if current[table] != expected[table]
    }
    _equal(differences, {}, "ui_three_business")
    after = _audits({"storage": ui.storage})
    _equal(after[: len(audit_before)], audit_before, "ui_three_audit_history")
    relevant = {"admin.user.activity.read", "admin.graph.read", "admin.knowledge.read"}
    added = [r for r in after[len(audit_before) :] if r["action"] in relevant]
    _equal(Counter((r["action"], r["target_id"]) for r in added), Counter(wanted), "ui_three_audit")
    for row in added:
        _equal((row["user_id"], row["target_type"]), (LEGACY_OWNER_USER_ID, "user"), "ui_three_actor")
    # Content-bearing canaries do not belong in the read audit payload.
    audit_text = json.dumps(added, ensure_ascii=False)
    for canary in (*OWN_NAMES.values(), NEW_TEXT, TEXT, "ТАЙНА-ДРУГОГО-ЧЕЛОВЕКА"):
        assert canary not in audit_text, "ui_three_audit_content"


def _get(ui, locator, path, query=None):
    def matches(response):
        url = urlsplit(response.url)
        fields = parse_qs(url.query)
        return (
            unquote(url.path) == path
            and response.request.method == "GET"
            and all(fields.get(k) == v for k, v in (query or {}).items())
        )

    response = click_expect_response(ui.page, locator, matches, "ui_three_handler")
    _equal(response.status, 200, "ui_three_http_status")
    return response.json(), parse_qs(urlsplit(response.url).query)


@pytest.fixture
def three_ui(ui_owner):
    ui = ui_owner
    assert not ui.storage.settings.shared_archive, "fixture expects separate synthetic people"
    for raw_id, person, text, day, filename in (
        (OLD_RAW, ALICE, "До окна активности", "2030-07-24", ""),
        (TEXT_RAW, ALICE, TEXT, "2030-07-27", ""),
        (NEW_RAW, ALICE, NEW_TEXT, "2030-07-31", "приборы-2030.pdf"),
        (FOREIGN_RAW, BORIS, "ТАЙНА-ДРУГОГО-ЧЕЛОВЕКА", "2030-07-30", ""),
    ):
        ui.storage.store_raw_object(
            RawObject(
                raw_id,
                person,
                "upload" if filename else "telegram",
                raw_id,
                text,
                "file" if filename else "text",
                metadata_json={"filename": filename} if filename else {},
            )
        )
        ui.storage.execute(
            "UPDATE raw_objects SET received_at=? WHERE id=?", (day + "T12:00:00+00:00", raw_id)
        )
    for kid, person, title, day in (
        (OLD_KO, ALICE, "Документ-2023", "2023-05-04"),
        (NOV_KO, ALICE, "Документ-ноябрь", "2024-11-02"),
        (UNDATED_KO, ALICE, "БЕЗДАТНЫЙ-040", ""),
        (FOREIGN_KO, BORIS, "ТАЙНА-ДРУГОГО-ЧЕЛОВЕКА", "2024-04-01"),
    ):
        rid = "raw-" + kid
        ui.storage.store_raw_object(RawObject(rid, person, "test", rid, title, "text"))
        ui.storage.store_knowledge_object(
            KnowledgeObject(
                id=kid,
                user_id=person,
                raw_object_id=rid,
                content=title,
                title=title,
                metadata_json={"document_date": day} if day else {},
            )
        )
    for eid, person, name, kind in (
        (ROOT_NODE, ALICE, OWN_NAMES[ROOT_NODE], EntityType.PERSON),
        (PEER_NODE, ALICE, OWN_NAMES[PEER_NODE], EntityType.PERSON),
        (ORG_NODE, ALICE, OWN_NAMES[ORG_NODE], EntityType.ORGANIZATION),
        (FOREIGN_NODE, BORIS, "ТАЙНА-ДРУГОГО-ЧЕЛОВЕКА", EntityType.PERSON),
        (FOREIGN_PEER, BORIS, "Чужой сосед", EntityType.PERSON),
    ):
        ui.storage.create_entity(Entity(eid, person, name, kind))
    ui.storage.create_relation(Relation(REL, ALICE, ROOT_NODE, PEER_NODE, "related_to", weight=0.9))
    ui.storage.create_relation(
        Relation(FOREIGN_REL, BORIS, FOREIGN_NODE, FOREIGN_PEER, "related_to", weight=0.8)
    )
    for eid in (ROOT_NODE, ORG_NODE):
        ui.storage.link_knowledge_entity(ALICE, ui.ids["alice_ko"], eid, status="accepted")
    # The overview defaults to nodes with >=1 linked knowledge item. Give the
    # relation's peer its own document without creating another cooccurrence.
    ui.storage.link_knowledge_entity(ALICE, NOV_KO, PEER_NODE, status="accepted")
    ui.storage.commit()
    return ui


def _activity(ui):
    from playwright.sync_api import expect

    before, audit_before = _snapshot(ui), _audits({"storage": ui.storage})
    ui.page.clock.set_fixed_time(datetime(2030, 8, 1, tzinfo=UTC))
    path = f"/api/admin/users/{ALICE}/activity"
    body, _ = _get(ui, ui.page.locator("#nav button", has_text="Активность"), path)
    _equal(body["user_id"], ALICE, "ui_activity_initial_person")
    expect(ui.page.locator("#activityName")).to_be_visible()
    body, query = _get(ui, ui.page.get_by_role("button", name="7 дней", exact=True), path, {"since": [SINCE]})
    _equal(
        query,
        {
            "limit": ["50"],
            "offset": ["0"],
            "analysis": ["topics", "rhythm", "volume", "change"],
            "since": [SINCE],
        },
        "ui_activity_window_request",
    )
    _equal((body["user_id"], body["content"]), (ALICE, "full"), "ui_activity_person")
    _equal([r["raw_object_id"] for r in body["items"]], [NEW_RAW, TEXT_RAW], "ui_activity_members")
    _equal([r["preview"] for r in body["items"]], [NEW_TEXT, TEXT], "ui_activity_contents")
    summary = body["summary"]
    _equal(
        tuple(
            summary[k]
            for k in (
                "user_id",
                "since",
                "until",
                "arrivals",
                "knowledge_objects",
                "pending_inbox",
                "messages",
                "by_day_days",
                "by_day_truncated",
            )
        ),
        (ALICE, SINCE, None, 2, 0, 0, 0, 2, False),
        "ui_activity_summary",
    )
    _equal(
        sorted((r["source"], r["count"]) for r in summary["by_source"]),
        [("telegram", 1), ("upload", 1)],
        "ui_activity_sources",
    )
    _equal(
        summary["by_day"],
        [{"day": "2030-07-31", "count": 1}, {"day": "2030-07-27", "count": 1}],
        "ui_activity_days",
    )
    expect(ui.page.locator("#app h2").first).to_have_text(f"Активность: {ALICE_NAME}")
    expect(ui.page.locator("#app tbody tr")).to_have_count(2)
    _equal(
        ui.page.locator("#app tbody tr td:nth-child(3) b").all_text_contents(),
        ["приборы-2030.pdf", TEXT],
        "ui_activity_dom_rows",
    )
    _equal(
        dashboard_stat_map(ui.page),
        {"поступлений": "2", "знаний": "0", "в inbox": "0", "сообщений": "0"},
        "ui_activity_dom_counts",
    )
    ui.page.locator("#app tbody tr", has_text="приборы-2030.pdf").get_by_role(
        "button", name="Показать"
    ).click()
    expect(ui.page.locator("#modal[open]")).to_be_visible()
    expect(ui.page.locator("#modalBody .pre")).to_have_text(NEW_TEXT)
    assert "ТАЙНА-ДРУГОГО-ЧЕЛОВЕКА" not in app_text(ui.page), "ui_activity_foreign_dom"
    _finish(ui, before, audit_before, [("admin.user.activity.read", ALICE)] * 2)


def _edge_tuple(edge):
    kind = edge.get("kind", "relation")
    source, target = (
        edge.get("source", edge.get("source_entity_id")),
        edge.get("target", edge.get("target_entity_id")),
    )
    if kind == "cooccurrence":
        source, target = sorted((source, target))
    return kind, source, target


def _graph_body(body, ids, *, local=False):
    _equal(
        {r["id"]: r["name"] for r in body["nodes"]}, {eid: OWN_NAMES[eid] for eid in ids}, "ui_graph_members"
    )
    _equal(len(body["nodes"]), len(ids), "ui_graph_unique_nodes")
    expected_edges = [("relation", ROOT_NODE, PEER_NODE)]
    if ORG_NODE in ids:
        expected_edges.append(("cooccurrence", *sorted((ROOT_NODE, ORG_NODE))))
    _equal(sorted(_edge_tuple(e) for e in body["edges"]), sorted(expected_edges), "ui_graph_edges")
    relation = next(e for e in body["edges"] if e.get("kind", "relation") == "relation")
    _equal(
        (relation["id"], relation["relation_type"], relation["weight"]),
        (REL, "related_to", 0.9),
        "ui_graph_relation",
    )
    _equal(
        (
            body["nodes_matched_at_least"],
            body["edges_matched_at_least"],
            body["nodes_truncated"],
            body["edges_truncated"],
        ),
        (len(ids), len(expected_edges), False, False),
        "ui_graph_totals",
    )
    if local:
        _equal(body["root"], ROOT_NODE, "ui_graph_focus_root")
    else:
        # Current overview total counts the whole person's archive; filtered
        # matched-at-least and shown above describe the selected subset.
        _equal((body["shown"], body["total"]), (len(ids), 3), "ui_graph_shown")


def _graph_dom(ui, ids):
    from playwright.sync_api import expect

    nodes = ui.page.locator("#graphSvg .gnode")
    expect(nodes).to_have_count(len(ids))
    _equal(
        set(nodes.evaluate_all("nodes => nodes.map(n => n.dataset.node)")), set(ids), "ui_graph_dom_members"
    )


def _graph(ui):
    from playwright.sync_api import expect

    before, audit_before = _snapshot(ui), _audits({"storage": ui.storage})
    path, query = "/api/admin/graph", {"user_id": [ALICE]}
    body, _ = _get(ui, ui.page.locator("#nav button", has_text="Граф"), path, query)
    _graph_body(body, [ROOT_NODE, PEER_NODE, ORG_NODE])
    _graph_dom(ui, [ROOT_NODE, PEER_NODE, ORG_NODE])
    body, _ = _get(
        ui,
        ui.page.get_by_role("button", name="person", exact=True),
        path,
        {**query, "entity_types": ["person"]},
    )
    _graph_body(body, [ROOT_NODE, PEER_NODE])
    _graph_dom(ui, [ROOT_NODE, PEER_NODE])
    node = ui.page.locator(f'#graphSvg .gnode[data-node="{ROOT_NODE}"] > circle.gfill-person')
    # Identity is exact; force only avoids waiting for the animated layout to stop.
    with ui.page.expect_response(
        lambda r: unquote(urlsplit(r.url).path) == path + "/" + ROOT_NODE
    ) as pending:
        node.click(force=True)
    _equal(pending.value.status, 200, "ui_graph_inspect_status")
    _graph_body(pending.value.json(), [ROOT_NODE, PEER_NODE, ORG_NODE], local=True)
    expect(ui.page.locator("#modal[open]")).to_be_visible()
    expect(ui.page.locator("#modalBody")).to_contain_text(ROOT_NODE)
    body, fields = _get(
        ui,
        ui.page.locator("#modal").get_by_role("button", name="Показать окрестность", exact=True),
        path + "/" + ROOT_NODE,
        {**query, "entity_types": ["person"], "depth": ["2"], "include_cooccurrence": ["true"]},
    )
    _equal(fields["user_id"], [ALICE], "ui_graph_focus_person")
    _graph_body(body, [ROOT_NODE, PEER_NODE], local=True)
    _graph_dom(ui, [ROOT_NODE, PEER_NODE])
    expect(ui.page.locator("#app")).to_contain_text("фокус: Первый узел")
    assert "ТАЙНА-ДРУГОГО-ЧЕЛОВЕКА" not in app_text(ui.page), "ui_graph_foreign_dom"
    _finish(ui, before, audit_before, [("admin.graph.read", ALICE)] * 4 + [("admin.knowledge.read", ALICE)])


def _timeline_body(ui, body, *, zoom):
    _equal(
        (body["user_id"], body["granularity"], body["since"], body["until"], body["undated"], body["limit"]),
        (
            ALICE,
            "month" if zoom else "year",
            "2024-01-01" if zoom else None,
            "2024-12-31" if zoom else None,
            1,
            100,
        ),
        "ui_timeline_window",
    )
    expected = [
        (NOV_KO, "Документ-ноябрь", "2024-11-02"),
        (ui.ids["alice_ko"], KNOWLEDGE_TITLE, "2024-03-17"),
    ]
    if not zoom:
        expected.append((OLD_KO, "Документ-2023", "2023-05-04"))
    _equal(
        [(r["id"], r["title"], r["document_date"]) for r in body["items"]], expected, "ui_timeline_members"
    )
    buckets = [("2024-03", 1), ("2024-11", 1)] if zoom else [("2023", 1), ("2024", 2)]
    _equal([(r["bucket"], r["count"]) for r in body["buckets"]], buckets, "ui_timeline_buckets")


def _timeline_dom(ui, *, zoom):
    from playwright.sync_api import expect

    expect(ui.page.locator("#app .tl-bar")).to_have_count(2)
    expect(ui.page.locator("#app")).to_contain_text("по месяцам" if zoom else "по годам")
    _equal(
        ui.page.locator("#app .tl-count").all_text_contents(),
        ["1", "1"] if zoom else ["1", "2"],
        "ui_timeline_dom_counts",
    )
    expected = ["Документ-ноябрь", KNOWLEDGE_TITLE] + ([] if zoom else ["Документ-2023"])
    expect(ui.page.locator("#app tbody tr")).to_have_count(len(expected))
    _equal(
        ui.page.locator("#app tbody tr td:nth-child(2) b").all_text_contents(),
        expected,
        "ui_timeline_dom_members",
    )
    for text in ("ТАЙНА-ДРУГОГО-ЧЕЛОВЕКА", "БЕЗДАТНЫЙ-040"):
        assert text not in app_text(ui.page), "ui_timeline_unplaced_dom"
    expect(ui.page.locator("#app .notice")).to_contain_text("1 объектов собственной даты не имеют")


def _timeline(ui):
    before, audit_before = _snapshot(ui), _audits({"storage": ui.storage})
    path, query = (
        "/api/admin/knowledge/timeline",
        {"user_id": [ALICE], "granularity": ["auto"], "limit": ["100"]},
    )
    body, _ = _get(ui, ui.page.locator("#nav button", has_text="Хроника"), path, query)
    _timeline_body(ui, body, zoom=False)
    _timeline_dom(ui, zoom=False)
    body, _ = _get(
        ui,
        ui.page.locator('#app .tl-bar[title="2024: 2"]'),
        path,
        {**query, "since": ["2024-01-01"], "until": ["2024-12-31"]},
    )
    _timeline_body(ui, body, zoom=True)
    _timeline_dom(ui, zoom=True)
    body, fields = _get(ui, ui.page.get_by_role("button", name="Ко всему корпусу", exact=True), path, query)
    _equal(fields, query, "ui_timeline_reset_request")
    _timeline_body(ui, body, zoom=False)
    _timeline_dom(ui, zoom=False)
    _finish(ui, before, audit_before, [("admin.knowledge.read", ALICE)] * 3)


def test_ui_activity_selected_person_period_preview_is_exact_and_read_only(three_ui):
    _activity(three_ui)


def test_ui_graph_selected_person_filter_and_focus_are_exact_and_read_only(three_ui):
    _graph(three_ui)


def test_ui_timeline_selected_person_zoom_is_exact_and_read_only(three_ui):
    _timeline(three_ui)


@pytest.mark.parametrize("surface", ["activity", "graph", "timeline"])
@pytest.mark.parametrize("fault", ["response", "write", "audit"])
def test_ui_three_oracles_reject_actual_response_write_and_audit_faults(
    three_ui, monkeypatch, surface, fault
):
    ui = three_ui
    run = {"activity": _activity, "graph": _graph, "timeline": _timeline}[surface]
    code = (
        f"ui_{surface}_members"
        if fault == "response"
        else "ui_three_business"
        if fault == "write"
        else "ui_three_audit"
    )
    injected = []
    if fault == "audit":
        from friday.admin_api import _graph as graph_api
        from friday.admin_api import _knowledge, _users

        module = {"activity": _users, "graph": graph_api, "timeline": _knowledge}[surface]

        def omit(*args, **kwargs):
            injected.append(True)

        monkeypatch.setattr(module, "_audit_cross_tenant_read", omit)
    else:

        def corrupt(route):
            fields = parse_qs(urlsplit(route.request.url).query)
            target = surface == "graph" or (
                fields.get("since") == [SINCE]
                if surface == "activity"
                else fields.get("since") == ["2024-01-01"]
            )
            if not target or injected:
                route.continue_()
                return
            response = route.fetch()
            body = response.json()
            _equal(response.status, 200, "ui_three_fault_requires_success")
            if fault == "write":
                with ui.storage.transaction() as conn:
                    conn.execute(
                        "UPDATE relations SET weight=0.44 WHERE id=? AND user_id=?", (FOREIGN_REL, BORIS)
                    )
            elif surface == "activity":
                body["items"][0]["raw_object_id"] = FOREIGN_RAW
            elif surface == "graph":
                body["nodes"][0]["id"] = FOREIGN_NODE
            else:
                body["items"].append(
                    {
                        "id": OLD_KO,
                        "title": "Документ-2023",
                        "document_date": "2023-05-04",
                        "knowledge_kind": "note",
                    }
                )
            injected.append(True)
            route.fulfill(response=response, json=body)

        pattern = {
            "activity": "**/api/admin/users/*/activity?**",
            "graph": "**/api/admin/graph?**",
            "timeline": "**/api/admin/knowledge/timeline?**",
        }[surface]
        ui.page.route(pattern, corrupt)
    try:
        with pytest.raises(AssertionError, match=code):
            run(ui)
    finally:
        if fault != "audit":
            ui.page.unroute(pattern, corrupt)
    assert injected, "ui_three_fault_not_injected"
