"""API metadata HTTP: redirect, Swagger markup and selected schema contracts.

These are HTTP document assertions, not browser or complete OpenAPI coverage.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from friday import __version__
from friday.permissions import LEGACY_OWNER_USER_ID
from tests.test_api_tokens import _issue
from tests.test_release_1_0_knowledge_read_oracles import _ADMIN, _audits, _read_state
from tests.test_release_1_0_knowledge_read_oracles import knowledge_read_http as knowledge_read_http
from tests.test_release_1_0_knowledge_read_oracles import profile_http as profile_http
from tests.test_release_1_0_profile_oracles import _A, _B, _GUEST

DOCS, SCHEMA = "/api/docs", "/api/openapi.json"


def _equal(actual, expected, code):
    assert actual == expected, (code, actual, expected)


@pytest.fixture
def metadata_http(knowledge_read_http):
    ctx = knowledge_read_http
    token = "jrc_synthetic_metadata_owner"
    _issue(ctx["storage"], LEGACY_OWNER_USER_ID, "owner", token)
    ctx["owner"] = {"Authorization": "Bearer " + token}
    ctx["storage"].set_permission_override(_A, "admin.diagnostics", "deny")
    credentials = tuple(
        ctx[role]["Authorization"].removeprefix("Bearer ") for role in ("owner", "admin", _A, _B, _GUEST)
    )
    assert all(credentials) and len(set(credentials)) == 5, "metadata_credential_fixture"
    ctx["canaries"] = (
        *credentials,
        ctx["settings"].api_token,
        ctx["settings"].telegram_bridge_secret,
        "PRIVATE_STYLE_",
        "FOREIGN_PRIVATE",
        "Иванов и документ A1",
        "Архив A1",
    )
    yield ctx


def _privacy(ctx, response):
    encoded = response.text + json.dumps(_audits(ctx), ensure_ascii=False)
    assert all(value not in encoded for value in ctx["canaries"]), "metadata_private_content"


class _SwaggerHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.mounts, self.scripts, self.titles = [], [], []
        self._inline = self._title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id") == "swagger-ui":
            self.mounts.append(tag)
        if tag == "script":
            self._inline = "src" not in attrs and attrs.get("type", "text/javascript") in {
                "text/javascript",
                "application/javascript",
            }
        if tag == "title":
            self._title = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._inline = False
        if tag == "title":
            self._title = False

    def handle_data(self, data):
        if self._inline:
            self.scripts.append(data)
        if self._title:
            self.titles.append(data)


def _docs(response):
    assert response.headers.get("content-type", "").startswith("text/html"), "metadata_docs_type"
    parsed = _SwaggerHTML()
    parsed.feed(response.text)
    _equal(parsed.mounts, ["div"], "metadata_swagger_mount")
    _equal("".join(parsed.titles).strip(), "Friday API", "metadata_docs_title")
    script = "\n".join(parsed.scripts)
    assert re.search(r"\b(?:const|let|var)\s+ui\s*=\s*SwaggerUIBundle\s*\(", script), (
        "metadata_swagger_bootstrap"
    )
    _equal(re.findall(r"\burl\s*:\s*['\"]([^'\"]+)['\"]", script), [SCHEMA], "metadata_swagger_schema_url")
    _equal(
        re.findall(r"\bdom_id[\"']?\s*:\s*['\"]([^'\"]+)['\"]", script),
        ["#swagger-ui"],
        "metadata_swagger_selector",
    )


def _parameters(operation):
    parameters = operation.get("parameters", [])
    keys = [(p["name"], p["in"]) for p in parameters]
    assert len(set(keys)) == len(keys), "metadata_schema_parameter_uniqueness"
    return {
        (p["name"], p["in"]): (
            p["required"],
            p["schema"]["type"],
            p["schema"].get("default"),
            p["schema"].get("minimum"),
            p["schema"].get("maximum"),
        )
        for p in parameters
    }


def _schema(response):
    assert response.headers.get("content-type", "").startswith("application/json"), "metadata_schema_type"
    body = response.json()
    _equal(body["info"], {"title": "Friday API", "version": __version__}, "metadata_schema_identity")
    assert isinstance(body.get("openapi"), str) and body["openapi"].startswith("3."), (
        "metadata_schema_protocol"
    )
    paths = body["paths"]
    for path in ("/api/admin/quality", "/api/admin/diagnostics", "/api/health"):
        assert path in paths and "get" in paths[path], "metadata_schema_operation"
        assert "200" in paths[path]["get"]["responses"], "metadata_schema_success"
    assert all(path not in paths for path in ("/", "/health", DOCS, SCHEMA)), "metadata_schema_manual_routes"
    _equal(
        _parameters(paths["/api/admin/quality"]["get"]),
        {
            ("user_id", "query"): (True, "string", None, None, None),
            ("lifecycle_limit", "query"): (False, "integer", 50, 1, 500),
            ("lifecycle_offset", "query"): (False, "integer", 0, 0, None),
        },
        "metadata_schema_quality_parameters",
    )
    _equal(
        _parameters(paths["/api/admin/diagnostics"]["get"]),
        {
            ("check_llm", "query"): (False, "boolean", False, None, None),
        },
        "metadata_schema_diagnostics_parameters",
    )


def _success(ctx, path, role=None):
    state, history = _read_state(ctx), _audits(ctx)
    response = ctx["client"].get(path, headers=ctx[role] if role else {}, follow_redirects=False)
    _privacy(ctx, response)
    if path == "/":
        _equal(
            (response.status_code, response.headers.get("location"), response.content),
            (307, "/admin/", b""),
            "metadata_root_redirect",
        )
    else:
        _equal(response.status_code, 200, "metadata_success_status")
        {DOCS: _docs, SCHEMA: _schema}[path](response)
    _equal(_read_state(ctx), state, "metadata_read_state")
    _equal(_audits(ctx), history, "metadata_read_no_audit")


def _walk(ctx):
    _success(ctx, "/")
    for role in ("owner", "admin"):
        for path in (DOCS, SCHEMA):
            _success(ctx, path, role)
    ctx["storage"].set_permission_override(_A, "admin.diagnostics", "allow")
    for path in (DOCS, SCHEMA):
        _success(ctx, path, _A)


def test_metadata_http_binds_redirect_swagger_and_selected_openapi_contracts(metadata_http):
    _walk(metadata_http)


def _refused(ctx):
    ctx["storage"].set_permission_override(_ADMIN, "admin.diagnostics", "deny")
    for role, status in ((None, 401), (_A, 403), ("admin", 403)):
        for path in (DOCS, SCHEMA):
            state, history = _read_state(ctx), _audits(ctx)
            response = ctx["client"].get(path, headers=ctx[role] if role else {})
            _equal(response.status_code, status, "metadata_refusal_status")
            _privacy(ctx, response)
            assert "paths" not in response.json() and 'id="swagger-ui"' not in response.text, (
                "metadata_refusal_no_inventory"
            )
            _equal(_read_state(ctx), state, "metadata_refusal_state")
            after = _audits(ctx)
            _equal(after[: len(history)], history, "metadata_refusal_audit_prefix")
            added = after[len(history) :]
            if status == 401:
                _equal(
                    [(row["user_id"], row["action"], row["target_type"], row["target_id"]) for row in added],
                    [("anonymous", "auth.failed", "auth", "invalid_credentials")],
                    "metadata_anonymous_audit",
                )
            else:
                _equal(added, [], "metadata_denied_no_audit")


def test_metadata_http_refuses_anonymous_denied_and_revoked_without_content_or_writes(metadata_http):
    _refused(metadata_http)


_FAULTS = (
    ("redirect_target", "metadata_root_redirect", "walk"),
    ("swagger_mount", "metadata_swagger_mount", "walk"),
    ("swagger_url", "metadata_swagger_schema_url", "walk"),
    ("schema_operation", "metadata_schema_operation", "walk"),
    ("schema_required", "metadata_schema_quality_parameters", "walk"),
    ("schema_duplicate", "metadata_schema_parameter_uniqueness", "walk"),
    ("schema_limit", "metadata_schema_quality_parameters", "walk"),
    ("schema_diagnostics_default", "metadata_schema_diagnostics_parameters", "walk"),
    ("schema_version", "metadata_schema_identity", "walk"),
    ("success_content", "metadata_private_content", "walk"),
    ("refusal_content", "metadata_private_content", "refused"),
    ("schema_user_credential", "metadata_private_content", "refused"),
    ("schema_admin_credential", "metadata_private_content", "refused"),
    ("read_own_write", "metadata_read_state", "walk"),
    ("refusal_audit_delete", "metadata_refusal_audit_prefix", "refused"),
)


@pytest.mark.parametrize("fault,code,scenario", _FAULTS, ids=[r[0] for r in _FAULTS])
def test_metadata_oracles_detect_actual_http_and_persisted_faults(
    metadata_http, monkeypatch, fault, code, scenario
):
    ctx, hits = metadata_http, []
    original, storage = TestClient.request, metadata_http["storage"]

    def altered(self, method, url, **kwargs):
        response = original(self, method, url, **kwargs)
        if hits or method != "GET":
            return response
        if fault == "redirect_target" and url == "/":
            hits.append(True)
            return httpx.Response(
                307, headers={"location": "https://wrong.invalid/"}, request=response.request
            )
        if url not in {DOCS, SCHEMA}:
            return response
        if (
            url == SCHEMA
            and response.status_code == 403
            and fault in {"schema_user_credential", "schema_admin_credential"}
        ):
            role = _A if fault == "schema_user_credential" else "admin"
            submitted = kwargs.get("headers", {}).get("Authorization")
            if submitted == ctx[role]["Authorization"]:
                secret = submitted.removeprefix("Bearer ")
                assert secret, "metadata_fault_fixture"
                hits.append(True)
                return httpx.Response(403, json={"detail": secret}, request=response.request)
        if response.status_code == 403 and fault == "refusal_content":
            secret = storage.execute(
                "SELECT content FROM knowledge_objects WHERE id=?", (ctx["first"],)
            ).fetchone()[0]
            assert secret == "Иванов и документ A1", "metadata_fault_fixture"
            hits.append(True)
            return httpx.Response(403, json={"detail": secret}, request=response.request)
        if response.status_code == 403 and fault == "refusal_audit_delete":
            key = storage.execute("SELECT id FROM audit_log ORDER BY rowid LIMIT 1").fetchone()[0]
            with storage.transaction() as conn:
                # Deliberately corrupt only this synthetic database. Normal
                # product SQL is correctly blocked by its append-only trigger.
                # Restore that trigger before the positive oracle reads state.
                trigger = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='audit_log_no_delete'"
                ).fetchone()[0]
                conn.execute("DROP TRIGGER audit_log_no_delete")
                try:
                    conn.execute("DELETE FROM audit_log WHERE id=?", (key,))
                finally:
                    conn.execute(trigger)
            assert storage.execute("SELECT id FROM audit_log WHERE id=?", (key,)).fetchone() is None, (
                "metadata_fault_write_not_applied"
            )
            hits.append(True)
            return response
        if response.status_code != 200:
            return response
        if url == DOCS and fault in {"swagger_mount", "swagger_url", "success_content"}:
            text = response.text
            if fault == "swagger_mount":
                text = text.replace('id="swagger-ui"', 'id="wrong-mount"')
            elif fault == "swagger_url":
                text = text.replace(SCHEMA, "/wrong-schema.json")
            else:
                text += "Иванов и документ A1"
            assert text != response.text, "metadata_fault_not_applied"
            hits.append(True)
            return httpx.Response(
                200, text=text, headers={"content-type": "text/html"}, request=response.request
            )
        if fault == "read_own_write" and url == SCHEMA:
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_objects SET title='CORRUPTED_METADATA_READ' WHERE id=?", (ctx["first"],)
                )
            assert (
                storage.execute("SELECT title FROM knowledge_objects WHERE id=?", (ctx["first"],)).fetchone()[
                    0
                ]
                == "CORRUPTED_METADATA_READ"
            ), "metadata_fault_write_not_applied"
            hits.append(True)
            return response
        if url != SCHEMA:
            return response
        body = response.json()
        if fault == "schema_operation":
            body["paths"]["/api/admin/quality"].pop("get")
        elif fault == "schema_duplicate":
            params = body["paths"]["/api/admin/quality"]["get"]["parameters"]
            selected = next(p for p in params if (p["name"], p["in"]) == ("user_id", "query"))
            params.append(json.loads(json.dumps(selected)))
            assert sum((p["name"], p["in"]) == ("user_id", "query") for p in params) == 2
        elif fault in {"schema_required", "schema_limit"}:
            params = body["paths"]["/api/admin/quality"]["get"]["parameters"]
            selected = next(
                p
                for p in params
                if p["name"] == ("user_id" if fault == "schema_required" else "lifecycle_limit")
            )
            if fault == "schema_required":
                selected["required"] = False
            else:
                selected["schema"]["maximum"] = 501
        elif fault == "schema_diagnostics_default":
            body["paths"]["/api/admin/diagnostics"]["get"]["parameters"][0]["schema"]["default"] = True
        elif fault == "schema_version":
            body["info"]["version"] = "wrong-candidate"
        else:
            return response
        hits.append(True)
        return httpx.Response(200, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match=code):
        {"walk": _walk, "refused": _refused}[scenario](ctx)
    assert hits, "metadata_fault_not_injected"
