"""Actual graph-path HTTP: independent paths, tenant resolution and refusals."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Entity, EntityType, Relation, RelationType

_ROUTE = "/api/kg/graph-path"
_PERSON = "ent-path-person"
_TEAM = "ent-path-team"
_PROJECT = "ent-path-project"
_ISOLATED = "ent-path-isolated"
_FOREIGN = "graph-path-foreign"
_FORWARD = [
    {
        "from": {"id": _PERSON, "name": "Иванов"},
        "to": {"id": _TEAM, "name": "Снабжение"},
        "relation_type": "member_of",
        "forward": True,
    },
    {
        "from": {"id": _TEAM, "name": "Снабжение"},
        "to": {"id": _PROJECT, "name": "Заря"},
        "relation_type": "related_to",
        "forward": False,
    },
]
_REVERSE = [
    {
        "from": {"id": _PROJECT, "name": "Заря"},
        "to": {"id": _TEAM, "name": "Снабжение"},
        "relation_type": "related_to",
        "forward": True,
    },
    {
        "from": {"id": _TEAM, "name": "Снабжение"},
        "to": {"id": _PERSON, "name": "Иванов"},
        "relation_type": "member_of",
        "forward": False,
    },
]


def _same(actual, expected):
    """Closed recursive equality distinguishes bool from int, including SQL rows."""
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _same(actual[key], expected[key])
    elif isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            _same(left, right)
    else:
        assert actual == expected


def _graph_rows(storage):
    """Only entities/relations in the two seeded tenants; no audit/all-table claim."""
    return {
        "entities": [
            tuple(row)
            for row in storage.execute(
                "SELECT * FROM entities WHERE user_id IN (?, ?) ORDER BY user_id, id",
                (LEGACY_OWNER_USER_ID, _FOREIGN),
            ).fetchall()
        ],
        "relations": [
            tuple(row)
            for row in storage.execute(
                "SELECT * FROM relations WHERE user_id IN (?, ?) ORDER BY user_id, id",
                (LEGACY_OWNER_USER_ID, _FOREIGN),
            ).fetchall()
        ],
    }


@pytest.fixture
def graph_path_http(settings):
    app = create_app(settings)
    # Keep assertions at the HTTP boundary even when a handler raises.
    with TestClient(app, raise_server_exceptions=False) as client:
        storage = app.state.storage
        storage.ensure_user(_FOREIGN, preset_key="user")
        for user_id, prefix in ((LEGACY_OWNER_USER_ID, ""), (_FOREIGN, "foreign-")):
            for entity_id, name, kind, aliases in (
                (_PERSON, "Иванов", EntityType.PERSON, ["Ваня"]),
                (_TEAM, "Снабжение", EntityType.ORGANIZATION, []),
                (_PROJECT, "Заря", EntityType.PROJECT, ["Аврора"]),
            ):
                storage.create_entity(
                    Entity(
                        id=prefix + entity_id,
                        user_id=user_id,
                        name=name,
                        entity_type=kind,
                        aliases_json=aliases,
                        metadata_json={"private": "GRAPH_PATH_PRIVATE_SENTINEL"},
                    )
                )
            for relation_id, left, right, kind in (
                ("rel-path-member", _PERSON, _TEAM, RelationType.MEMBER_OF),
                ("rel-path-project", _PROJECT, _TEAM, RelationType.RELATED_TO),
            ):
                storage.create_relation(
                    Relation(
                        id=prefix + relation_id,
                        user_id=user_id,
                        source_entity_id=prefix + left,
                        target_entity_id=prefix + right,
                        relation_type=kind,
                        metadata_json={"private": "GRAPH_PATH_PRIVATE_SENTINEL"},
                    )
                )
        storage.create_entity(Entity(id=_ISOLATED, user_id=LEGACY_OWNER_USER_ID, name="Остров"))
        # A no-path answer must not pass because fixture edges silently disappeared.
        relations = storage.execute(
            "SELECT id, user_id, source_entity_id, target_entity_id, relation_type "
            "FROM relations WHERE user_id IN (?, ?) ORDER BY id",
            (LEGACY_OWNER_USER_ID, _FOREIGN),
        ).fetchall()
        _same(
            [tuple(row) for row in relations],
            [
                ("foreign-rel-path-member", _FOREIGN, "foreign-" + _PERSON, "foreign-" + _TEAM, "member_of"),
                (
                    "foreign-rel-path-project",
                    _FOREIGN,
                    "foreign-" + _PROJECT,
                    "foreign-" + _TEAM,
                    "related_to",
                ),
                ("rel-path-member", LEGACY_OWNER_USER_ID, _PERSON, _TEAM, "member_of"),
                ("rel-path-project", LEGACY_OWNER_USER_ID, _PROJECT, _TEAM, "related_to"),
            ],
        )
        # Two real tenants deliberately share each endpoint's canonical name and alias.
        for name, own_id, alias in (("Иванов", _PERSON, "Ваня"), ("Заря", _PROJECT, "Аврора")):
            rows = storage.execute(
                "SELECT id, user_id, aliases_json FROM entities WHERE name=? ORDER BY id", (name,)
            ).fetchall()
            assert len(rows) == 2
            _same(
                {row[0]: [row[1], json.loads(row[2])] for row in rows},
                {own_id: [LEGACY_OWNER_USER_ID, [alias]], "foreign-" + own_id: [_FOREIGN, [alias]]},
            )
        denied_user = "graph-path-noread"
        denied_secret = "graph-path-denied-" + "N" * 32
        storage.ensure_user(denied_user, source="api-token", preset_key="user")
        storage.create_api_token(
            denied_user,
            hashlib.sha256(denied_secret.encode()).hexdigest(),
            label="graph-path-http",
            created_by="test",
        )
        storage.set_permission_override(denied_user, "kg.read", "deny")
        yield (
            client,
            storage,
            {"Authorization": f"Bearer {settings.api_token}"},
            {"Authorization": f"Bearer {denied_secret}"},
        )


@pytest.mark.parametrize(
    ("source", "target", "depth", "found", "path", "searched"),
    [
        (_PERSON, _PROJECT, 2, True, _FORWARD, 2),
        (_PROJECT, _PERSON, 2, True, _REVERSE, 2),
        (_PERSON, _PROJECT, 1, False, [], 1),
        (_PERSON, _ISOLATED, 5, False, [], 5),
        (_PERSON, _PROJECT, None, True, _FORWARD, 4),
    ],
    ids=["forward", "reverse", "depth-one", "disconnected", "default-depth"],
)
def test_graph_path_ids_pin_steps_depth_and_preserve_rows(
    graph_path_http, source, target, depth, found, path, searched
):
    client, storage, owner, _ = graph_path_http
    before = _graph_rows(storage)
    params = {"source": source, "target": target}
    if depth is not None:
        params["depth"] = depth
    response = client.get(_ROUTE, params=params, headers=owner)
    _same(_graph_rows(storage), before)
    assert response.status_code == 200
    _same(response.json(), {"found": found, "path": path, "depth_searched": searched})


@pytest.mark.parametrize(
    ("source", "target"),
    [("Иванов", _PROJECT), (_PERSON, "Заря"), ("Ваня", _PROJECT), (_PERSON, "Аврора")],
    ids=["source-name", "target-name", "source-alias", "target-alias"],
)
def test_graph_path_names_and_aliases_resolve_only_own_entities(graph_path_http, source, target):
    client, storage, owner, _ = graph_path_http
    before = _graph_rows(storage)
    response = client.get(_ROUTE, params={"source": source, "target": target, "depth": 2}, headers=owner)
    _same(_graph_rows(storage), before)
    assert response.status_code == 200
    _same(response.json(), {"found": True, "path": _FORWARD, "depth_searched": 2})


@pytest.mark.parametrize("match", ["name", "alias"])
def test_graph_path_ambiguous_names_require_an_exact_id_without_graph_writes(graph_path_http, match):
    client, storage, owner, _ = graph_path_http
    storage.create_entity(
        Entity(
            id="ent-path-ambiguous",
            user_id=LEGACY_OWNER_USER_ID,
            name="Иванов" if match == "name" else "Другой человек",
            entity_type=EntityType.PERSON,
            aliases_json=["Ваня"] if match == "alias" else [],
        )
    )
    before = _graph_rows(storage)
    params = {"source": "Иванов" if match == "name" else "Ваня", "target": _PROJECT, "depth": 2}
    response = client.get(_ROUTE, params=params, headers=owner)
    assert response.status_code == 400
    _same(response.json(), {"detail": "Неоднозначное имя сущности: укажите id"})
    _same(_graph_rows(storage), before)
    params["source"] = _PERSON
    response = client.get(_ROUTE, params=params, headers=owner)
    assert response.status_code == 200
    _same(response.json(), {"found": True, "path": _FORWARD, "depth_searched": 2})
    _same(_graph_rows(storage), before)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "нет-такого-узла"),
        ("target", "нет-такого-узла"),
        ("source", "foreign-" + _PERSON),
        ("target", "foreign-" + _PROJECT),
    ],
    ids=["missing-source", "missing-target", "foreign-source", "foreign-target"],
)
def test_graph_path_missing_and_foreign_endpoints_are_404(graph_path_http, field, value):
    client, storage, owner, _ = graph_path_http
    before = _graph_rows(storage)
    params = {"source": _PERSON, "target": _PROJECT, "depth": 2, field: value}
    response = client.get(_ROUTE, params=params, headers=owner)
    _same(_graph_rows(storage), before)
    assert response.status_code == 404
    _same(response.json(), {"detail": f"Объект не найден: {value}"})


@pytest.mark.parametrize("case", ["depth-low", "depth-high", "anonymous", "kg-read-denied"])
def test_graph_path_validates_depth_and_authority(graph_path_http, case):
    client, storage, owner, denied = graph_path_http
    before = _graph_rows(storage)
    params = {"source": _PERSON, "target": _PROJECT, "depth": 2}
    headers = owner
    if case == "depth-low":
        params["depth"] = 0
    elif case == "depth-high":
        params["depth"] = 6
    elif case == "anonymous":
        headers = {}
    else:
        headers = denied
    response = client.get(_ROUTE, params=params, headers=headers)
    _same(_graph_rows(storage), before)
    if case in {"depth-low", "depth-high"}:
        assert response.status_code == 422
        body = response.json()
        assert type(body) is dict and body.keys() == {"detail"}
        assert type(body["detail"]) is list and len(body["detail"]) == 1
        error = body["detail"][0]
        _same(error["loc"], ["query", "depth"])
        _same(error["type"], "greater_than_equal" if case == "depth-low" else "less_than_equal")
        _same(error["ctx"], {"ge": 1} if case == "depth-low" else {"le": 5})
    else:
        assert response.status_code == (401 if case == "anonymous" else 403)
        detail = (
            "Missing authentication" if case == "anonymous" else "Access denied for kg.read (explicit_deny)"
        )
        _same(response.json(), {"detail": detail})
