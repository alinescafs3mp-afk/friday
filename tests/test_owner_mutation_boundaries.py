"""Delegated admins must not mutate or export the owner account.

`_protect_owner_target` already guarded users, tokens, conversations, missions and
purge. Knowledge write, graph, lifecycle, conflicts, inbox and export accepted a
tenant `user_id` under ordinary delegated capabilities (`admin.all_data.manage`,
`admin.export`) and never called the guard — so an admin could rewrite the owner's
documents or download the whole archive. G11 closes every remaining write path and
pins them with an inventory walk so the next extracted module cannot quietly skip
the guard again.
"""

from __future__ import annotations

import hashlib
import inspect
import re

from fastapi.testclient import TestClient

from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app

# Capabilities that authorize cross-tenant mutation or export. `.read` is out of
# scope: owner deliberately allows delegated admins to *see* other accounts.
_MUTATING_CAPS = (
    "admin.all_data.manage",
    "admin.export",
    "admin.data.purge",
    "admin.users.manage",
    "admin.tokens.manage",
    "admin.presets.manage",
    "admin.backup.manage",
)

# Routes that take a tenant target only by looking up an object id (token, mission)
# rather than accepting `user_id` in body/query. Covered by dedicated tests; listed
# so silence cannot hide a new object-scoped writer.
_OBJECT_SCOPED_WRITERS = (
    "DELETE /api/admin/tokens/{token_id}",
    "POST /api/admin/missions/{mission_id}/cancel",
    "DELETE /api/admin/identities/{source}/{external_id}",
)


def _issue(storage, user_id: str, preset: str, secret: str) -> dict:
    storage.ensure_user(user_id, source="api-token", display_name=user_id, preset_key=preset)
    storage.update_user(user_id, preset_key=preset)
    return storage.create_api_token(
        user_id, hashlib.sha256(secret.encode()).hexdigest(), label="test", created_by="test"
    )


def _seed_owner_objects(storage, owner_id: str) -> dict[str, str]:
    """Real ids for every path placeholder the inventory walk may hit."""
    import pathlib

    from jericho.storage.models import (
        Entity,
        EntityType,
        KnowledgeObject,
        Mission,
        RawObject,
        new_id,
    )

    storage.ensure_user(owner_id, source="test", display_name="owner", preset_key="owner")
    storage.update_user(owner_id, preset_key="owner")

    raw = RawObject(
        id=new_id("raw"),
        user_id=owner_id,
        source="test",
        source_ref=new_id("src"),
        raw_content="Материал владельца",
        content_type="text",
        content_hash=hashlib.sha256(b"owner-probe").hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=owner_id,
        raw_object_id=raw.id,
        content="Материал владельца",
        content_type="text",
        title="Владелец",
    )
    storage.store_knowledge_object(knowledge)
    entity = Entity(
        id=new_id("ent"), user_id=owner_id, name="Владелец", entity_type=EntityType.CONCEPT
    )
    storage.create_entity(entity)
    conversation = storage.create_conversation(owner_id, "Владелец")
    mission = Mission(id=new_id("mis"), user_id=owner_id, goal="Цель владельца")
    storage.create_mission(mission)

    stored = pathlib.Path(storage.settings.files_dir) / owner_id / "owner.txt"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text("байты", encoding="utf-8")
    upload = RawObject(
        id=new_id("raw"),
        user_id=owner_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content="байты",
        content_type="file",
        content_hash=hashlib.sha256(b"owner-file").hexdigest(),
        metadata_json={"filename": "owner.txt", "stored_path": str(stored)},
    )
    storage.store_raw_object(upload)

    # Synthetic ids for routes that only need a path segment to reach the guard.
    return {
        "user_id": owner_id,
        "raw_id": upload.id,
        "knowledge_id": knowledge.id,
        "entity_id": entity.id,
        "mission_id": mission.id,
        "conversation_id": str(
            conversation["id"] if isinstance(conversation, dict) else conversation.id
        ),
        "link_id": "kel_missing",
        "conflict_id": "kc_missing",
        "candidate_id": "rc_missing",
        "merge_id": "em_missing",
        "inbox_id": "inb_missing",
        "case_id": "eval_missing",
        "token_id": "tok_missing",
        "filename": "missing.json",
        "source": "telegram",
        "external_id": "missing",
        "security_id": "chat.ask",
    }


def _admin_mutating_routes(app) -> list[tuple[str, str, object]]:
    found: list[tuple[str, str, object]] = []

    def walk(routes, prefix: str) -> None:
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                context = getattr(route, "include_context", None)
                walk(nested.routes, prefix + getattr(context, "prefix", ""))
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if path is None:
                continue
            full = prefix + path
            if not full.startswith("/api/admin"):
                continue
            for method in methods:
                if method in {"POST", "PATCH", "DELETE"}:
                    found.append((method, full, route.endpoint))

    walk(app.routes, "")
    return found


def _endpoint_source(endpoint) -> str:
    try:
        return inspect.getsource(endpoint)
    except (OSError, TypeError):
        return ""


def _accepts_tenant_user_id(endpoint) -> bool:
    """True when the route takes a tenant target from query or JSON body."""
    if "user_id" in inspect.signature(endpoint).parameters:
        return True
    src = _endpoint_source(endpoint)
    return 'body.get("user_id")' in src or "body.get('user_id')" in src


def _is_mutating_capability(endpoint) -> bool:
    src = _endpoint_source(endpoint)
    return any(cap in src for cap in _MUTATING_CAPS)


def _probe_body(owner_id: str) -> dict:
    """Minimal body that carries the owner id and satisfies common validators."""
    return {
        "user_id": owner_id,
        "name": "probe",
        "title": "probe",
        "status": "accepted",
        "decision": "accept",
        "action": "keep",
        "version": 1,
        "entity_type": "concept",
        "entity_id": "ent_missing",
        "winner_id": "ent_missing",
        "knowledge_object_ids": ["ko_missing"],
        "knowledge_ids": ["ko_missing"],
        "conflict_ids": ["kc_missing"],
        "candidate_ids": ["rc_missing"],
        "inbox_ids": ["inb_missing"],
        "ids": ["ko_missing"],
        "expected_ids": ["ko_missing"],
        "query": "probe",
        "label": "probe",
        "preset_key": "user",
        "source": "telegram",
        "external_id": "probe",
        "id": owner_id,
    }


def test_delegated_admin_cannot_export_owner_archive(settings):
    """Heaviest case: full-account JSON export under admin.export."""
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner_id = LEGACY_OWNER_USER_ID
        storage.ensure_user(owner_id, preset_key="owner")
        storage.update_user(owner_id, preset_key="owner")
        _issue(storage, "adm_export", "admin", "jrc_admin_export")
        admin = {"Authorization": "Bearer jrc_admin_export"}

        refused = client.post("/api/admin/exports", headers=admin, json={"user_id": owner_id})
        assert refused.status_code == 403, refused.text
        assert "владел" in refused.json()["detail"].casefold()

        # Own archive still works for the delegated admin.
        own = client.post("/api/admin/exports", headers=admin, json={"user_id": "adm_export"})
        assert own.status_code == 200, own.text
        assert own.json()["export"]["filename"]

        # Owner may still export themselves.
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        ok = client.post("/api/admin/exports", headers=owner, json={})
        assert ok.status_code == 200, ok.text


def test_every_admin_mutation_against_owner_is_forbidden(settings):
    """Inventory walk: every manage/export route that takes user_id returns 403.

    A hand-maintained list of call sites drifts. Walking the live FastAPI routes
    and requiring 403 for `user_id=<owner>` catches the next module that forgets
    `_protect_owner_target`. Routes that cannot be reached report themselves —
    silence here is what let graph/{entity_id} sit unaudited for months.
    """
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner_id = LEGACY_OWNER_USER_ID
        placeholders = _seed_owner_objects(storage, owner_id)
        _issue(storage, "adm_inv", "admin", "jrc_admin_inventory")
        admin = {"Authorization": "Bearer jrc_admin_inventory"}

        checked: list[str] = []
        leaked: list[str] = []
        unreachable: list[str] = []
        object_scoped_seen: list[str] = []

        for method, path, endpoint in _admin_mutating_routes(app):
            label = f"{method} {path}"
            if not _is_mutating_capability(endpoint):
                continue
            if not _accepts_tenant_user_id(endpoint):
                if label in _OBJECT_SCOPED_WRITERS or any(
                    key in path for key in ("/tokens/", "/missions/", "/identities/")
                ):
                    object_scoped_seen.append(label)
                continue

            concrete = path
            missing = False
            for name in re.findall(r"\{(\w+)\}", path):
                if name not in placeholders:
                    missing = True
                    break
                concrete = concrete.replace("{" + name + "}", placeholders[name])
            if missing:
                unreachable.append(f"{label} (unseeded placeholders)")
                continue

            params = {"user_id": owner_id} if "user_id" in inspect.signature(endpoint).parameters else None
            body = _probe_body(owner_id)
            response = client.request(method, concrete, headers=admin, params=params, json=body)

            # 403 is the only success for this inventory. 401 means the actor was not
            # authenticated as admin; 404/400 after the guard would mean the guard did
            # not run and the handler reached storage.
            if response.status_code == 403:
                checked.append(label)
                continue
            if response.status_code >= 500:
                unreachable.append(f"{label} -> {response.status_code}")
                continue
            leaked.append(f"{label} -> {response.status_code} {response.text[:120]}")

        assert checked, "no admin mutation with a tenant user_id was exercised — the walk is broken"
        assert not leaked, (
            "delegated admin could act on the owner account through these routes "
            f"(expected 403): {leaked}"
        )
        assert not unreachable, (
            "these owner-mutation routes could not be exercised — seed placeholders "
            f"or list them with a reason: {unreachable}"
        )
        # Sanity: we actually covered the export and knowledge-write holes that
        # motivated G11, not only already-protected user routes.
        assert any(path.endswith("/exports") for path in checked), checked
        assert any("/knowledge/" in path for path in checked), checked


def test_mutation_removing_export_protect_is_caught(settings, monkeypatch):
    """If `_protect_owner_target` is a no-op on export, the probe fails open.

    The inventory test would report `POST /api/admin/exports -> 200` instead of
    403. Patching the name bound in `_maintenance` (not only `_deps`) is required:
    the handler imported the guard at module load time.
    """
    import jericho.admin_api._maintenance as maintenance

    monkeypatch.setattr(maintenance, "_protect_owner_target", lambda *_a, **_k: None)

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner_id = LEGACY_OWNER_USER_ID
        storage.ensure_user(owner_id, preset_key="owner")
        storage.update_user(owner_id, preset_key="owner")
        _issue(storage, "adm_mut", "admin", "jrc_admin_mut")
        admin = {"Authorization": "Bearer jrc_admin_mut"}

        leaked = client.post("/api/admin/exports", headers=admin, json={"user_id": owner_id})
        assert leaked.status_code == 200, (
            f"expected the unguarded export of the owner archive to succeed so the "
            f"inventory would catch it; got {leaked.status_code}: {leaked.text}"
        )
        assert leaked.json()["export"]["filename"]
