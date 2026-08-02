"""Временный воспроизводитель — удалить после прогона."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from friday.organs import ServiceContext
from friday.organs.monitors import scan_monitors
from friday.security import sign_bridge_request
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _document(storage, user_id: str, text: str, title: str = "") -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}{text}{uuid.uuid4()}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title or text[:40],
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _ctx(settings, storage) -> ServiceContext:
    awake = replace(settings, quiet_hours_start=0, quiet_hours_end=0)
    return ServiceContext(settings=awake, storage=storage, kg=None, ingestion=None)


def _dump(storage, label: str, monitor_id: str, user_id: str) -> None:
    mon = storage.get_monitor(monitor_id, user_id)
    rows = storage.execute(
        "SELECT id, status, attempts, dedup_key, kind FROM outbound_notifications WHERE user_id=?",
        (user_id,),
    ).fetchall()
    print(f"\n--- {label}")
    print(
        "  monitor: last_seen_rowid=%s matches_reported=%s"
        % (mon["last_seen_rowid"], mon["matches_reported"])
    )
    for row in rows:
        print("  notif: %s" % dict(row))
    print("  pending for bridge: %s" % storage.list_pending_notifications(limit=10))


@pytest.mark.asyncio
async def test_repro_a_delivery_failure_loses_the_match_forever(settings, storage):
    storage.ensure_user("alice")
    storage.update_user("alice", metadata_json={"chat_id": "5001"})
    monitor = storage.create_monitor("alice", "поверка весов", chat_id="5001")
    _document(storage, "alice", "Поверка весов назначена на завтра", "Акт поверки")

    await scan_monitors(_ctx(settings, storage))
    _dump(storage, "после первого прохода (уведомление в очереди)", monitor["id"], "alice")

    # Мост пять раз не смог отправить (Telegram лежал ~75 секунд).
    for _ in range(5):
        pending = storage.list_pending_notifications(limit=10)
        if not pending:
            break
        storage.mark_notifications([], [row["id"] for row in pending])
    _dump(storage, "после пяти неудачных попыток", monitor["id"], "alice")

    # Три следующих прохода монитора.
    for _ in range(3):
        await scan_monitors(_ctx(settings, storage))
    _dump(storage, "после трёх следующих проходов", monitor["id"], "alice")

    delivered = storage.execute(
        "SELECT COUNT(*) AS c FROM outbound_notifications "
        "WHERE user_id='alice' AND kind='monitor' AND status='sent'"
    ).fetchone()["c"]
    still_pending = storage.list_pending_notifications(limit=10)
    mon = storage.get_monitor(monitor["id"], "alice")
    print(f"\nИТОГ (а): доставлено={delivered} в очереди={len(still_pending)} "
          f"matches_reported={mon['matches_reported']} last_seen_rowid={mon['last_seen_rowid']}")


@pytest.mark.asyncio
async def test_repro_b_open_registration_off_loses_the_match(settings):
    tuned = replace(
        settings,
        telegram_allowed_chat_ids=[5001],
        telegram_open_registration=False,
        quiet_hours_start=0,
        quiet_hours_end=0,
    )
    with TestClient(create_app(tuned)) as client:
        storage = client.app.state.storage
        storage.ensure_user("bob")
        storage.update_user("bob", metadata_json={"chat_id": "6001", "self_registered": True})
        monitor = storage.create_monitor("bob", "поверка весов", chat_id="6001")
        _document(storage, "bob", "Поверка весов назначена на завтра", "Акт поверки")

        await scan_monitors(_ctx(tuned, storage))
        _dump(storage, "(б) после прохода: строка в очереди", monitor["id"], "bob")

        # Мост тянет очередь — маршрут гасит недоставляемую строку.
        path = "/api/notifications/pending?limit=20"
        timestamp = int(time.time())
        nonce = uuid.uuid4().hex
        signer = "5001"
        response = client.get(
            path,
            headers={
                "X-Friday-Timestamp": str(timestamp),
                "X-Friday-User": signer,
                "X-Friday-Chat": signer,
                "X-Friday-Nonce": nonce,
                "X-Friday-Signature": sign_bridge_request(
                    tuned.telegram_bridge_secret,
                    timestamp=timestamp,
                    method="GET",
                    path=path,
                    external_user_id=signer,
                    chat_id=signer,
                    nonce=nonce,
                    body=b"",
                ),
            },
        )
        assert response.status_code == 200, response.text
        print("\n(б) очередь для моста:", response.json())
        _dump(storage, "(б) после выдачи очереди", monitor["id"], "bob")

        # Владелец включает открытую регистрацию обратно.
        back = replace(tuned, telegram_open_registration=True)
        for _ in range(3):
            await scan_monitors(_ctx(back, storage))
        _dump(storage, "(б) после включения регистрации и трёх проходов", monitor["id"], "bob")

        mon = storage.get_monitor(monitor["id"], "bob")
        print(f"\nИТОГ (б): в очереди={len(storage.list_pending_notifications(limit=10))} "
              f"matches_reported={mon['matches_reported']} last_seen_rowid={mon['last_seen_rowid']}")
