from __future__ import annotations
import json
from fastapi.testclient import TestClient


def _client(settings):
    from friday.server import create_app
    return TestClient(create_app(settings))


def _mk(c, owner, uid, name, preset):
    r = c.post("/api/admin/users", json={"id": uid, "display_name": name, "preset_key": preset},
               headers=owner)
    assert r.status_code == 200, r.text
    r = c.post("/api/admin/tokens", json={"user_id": uid}, headers=owner)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_peer_admin_lockout_and_impersonation(settings):
    with _client(settings) as c:
        owner = {"Authorization": "Bearer " + "A" * 48}
        b1 = _mk(c, owner, "local:boss1", "Первый", "admin")
        b2 = _mk(c, owner, "local:boss2", "Второй", "admin")
        s1 = _mk(c, owner, "local:sub1", "Иван", "user")

        # boss2 can chat right now
        print("boss2 /api/me before:", c.get("/api/me", headers=b2).status_code)

        # boss1 denies boss2 chat.use
        r = c.put("/api/admin/users/local:boss2/permissions/chat.use",
                  json={"effect": "deny"}, headers=b1)
        print("boss1 denies boss2 chat.use ->", r.status_code, r.text[:200])

        r = c.post("/api/chat", json={"message": "привет"}, headers=b2)
        print("boss2 /api/chat after deny ->", r.status_code, r.text[:200])

        # boss1 disables boss2 entirely
        r = c.patch("/api/admin/users/local:boss2", json={"status": "disabled"}, headers=b1)
        print("boss1 disables boss2 ->", r.status_code, r.text[:200])
        r = c.get("/api/me", headers=b2)
        print("boss2 /api/me after disable ->", r.status_code, r.text[:200])

        # boss1 demotes boss2's preset
        r = c.post("/api/admin/users/local:boss2/preset", json={"preset_key": "guest"}, headers=b1)
        print("boss1 demotes boss2 -> guest:", r.status_code, r.text[:200])

        # boss1 mints a token for the subordinate and acts AS them
        r = c.post("/api/admin/tokens", json={"user_id": "local:sub1", "label": "x"}, headers=b1)
        print("boss1 mints token for sub1 ->", r.status_code)
        if r.status_code == 200:
            imp = {"Authorization": "Bearer " + r.json()["token"]}
            me = c.get("/api/me", headers=imp)
            print("boss1 acting as sub1, /api/me ->", me.status_code, me.json()["actor"])

        # boss1 mints a token for the OTHER boss
        r = c.post("/api/admin/tokens", json={"user_id": "local:boss2", "label": "y"}, headers=b1)
        print("boss1 mints token for boss2 ->", r.status_code, r.text[:200])
