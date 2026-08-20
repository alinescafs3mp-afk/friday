"""Request-local audit correlation without ambient or forged provenance."""

from __future__ import annotations

import asyncio
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from friday.audit_privacy import (
    bind_audit_request_id,
    current_audit_request_id,
    server_audit_request_id,
)
from friday.storage.models import AuditEntry, new_id


def _entry(*, request_id: str = "") -> AuditEntry:
    return AuditEntry(
        id=new_id("audit"),
        user_id="alice",
        action="knowledge.import",
        target_type="import",
        target_id=None,
        request_id=request_id,
    )


def _stored_request_id(storage, audit_id: str) -> str:
    row = storage.execute("SELECT request_id FROM audit_log WHERE id=?", (audit_id,)).fetchone()
    assert row is not None
    return str(row["request_id"] or "")


def test_request_context_nests_and_resets_even_on_failure() -> None:
    server_id = server_audit_request_id("0123456789abcdef01234567")
    assert current_audit_request_id() == ""

    with bind_audit_request_id("client.outer"):
        assert current_audit_request_id() == "client.outer"
        with bind_audit_request_id(server_id):
            assert current_audit_request_id() == server_id
        assert current_audit_request_id() == "client.outer"
        with (
            pytest.raises(ValueError, match="invalid audit request"),
            bind_audit_request_id("0123456789abcdef01234567"),
        ):
            raise AssertionError("reserved client ID was admitted")
        assert current_audit_request_id() == "client.outer"

    assert current_audit_request_id() == ""
    with pytest.raises(RuntimeError, match="synthetic"), bind_audit_request_id("client.failure"):
        raise RuntimeError("synthetic")
    assert current_audit_request_id() == ""


def test_request_context_is_isolated_between_async_tasks() -> None:
    server_id = server_audit_request_id("89abcdef0123456701234567")

    async def scenario() -> tuple[str, str]:
        ready = (asyncio.Event(), asyncio.Event())
        release = asyncio.Event()

        async def observe(value: str, own_ready: asyncio.Event) -> str:
            with bind_audit_request_id(value):
                own_ready.set()
                await release.wait()
                return current_audit_request_id()

        tasks = (
            asyncio.create_task(observe("client.async", ready[0])),
            asyncio.create_task(observe(server_id, ready[1])),
        )
        await asyncio.gather(*(event.wait() for event in ready))
        assert current_audit_request_id() == ""
        release.set()
        observed = await asyncio.gather(*tasks)
        return observed[0], observed[1]

    assert asyncio.run(scenario()) == ("client.async", server_id)
    assert current_audit_request_id() == ""


def test_blank_entry_uses_client_context_as_opaque_reqref_without_mutation(storage) -> None:
    storage.ensure_user("alice")
    entry = _entry()
    source = deepcopy(entry)

    with bind_audit_request_id("client-turn-32"):
        returned = storage.log_audit(entry)

    stored = _stored_request_id(storage, entry.id)
    assert returned is entry
    assert entry == source
    assert stored.startswith("reqref_")
    assert stored != "client-turn-32"
    assert "client-turn-32" not in stored
    assert current_audit_request_id() == ""


def test_blank_entry_preserves_exact_server_marker(storage) -> None:
    storage.ensure_user("alice")
    server_id = server_audit_request_id("fedcba987654321001234567")
    entry = _entry()

    with bind_audit_request_id(server_id):
        storage.log_audit(entry)

    assert _stored_request_id(storage, entry.id) == server_id
    assert entry.request_id == ""


def test_explicit_entry_request_id_wins_over_context_and_keeps_sanitization(storage) -> None:
    storage.ensure_user("alice")
    server_id = server_audit_request_id("00112233445566778899aabb")
    entry = _entry(request_id="explicit.client")
    source = deepcopy(entry)

    with bind_audit_request_id(server_id):
        storage.log_audit(entry)

    stored = _stored_request_id(storage, entry.id)
    assert entry == source
    assert stored.startswith("reqref_")
    assert stored != server_id
    assert stored != entry.request_id


def test_background_blank_entry_stays_blank(storage) -> None:
    storage.ensure_user("alice")
    entry = _entry()

    storage.log_audit(entry)

    assert current_audit_request_id() == ""
    assert _stored_request_id(storage, entry.id) == ""


def test_binding_copies_only_the_validated_text_and_does_not_retain_source() -> None:
    class CorrelationSource(str):
        pass

    source = CorrelationSource("client.source")
    source.private_payload = "SOURCE-NONRETENTION-CANARY"  # type: ignore[attr-defined]
    source_state = dict(source.__dict__)
    source_ref = weakref.ref(source)

    with bind_audit_request_id(source):
        assert current_audit_request_id() == "client.source"
        assert type(current_audit_request_id()) is not CorrelationSource
        assert source.__dict__ == source_state
        del source
        gc.collect()
        assert source_ref() is None

    assert current_audit_request_id() == ""


@pytest.mark.parametrize(
    "value",
    [None, "", "contains spaces", "a" * 65, object()],
)
def test_invalid_context_fails_closed_without_changing_outer_context(value: object) -> None:
    with bind_audit_request_id("client.valid"):
        with pytest.raises(ValueError), bind_audit_request_id(value):
            raise AssertionError("invalid context was admitted")
        assert current_audit_request_id() == "client.valid"

    assert current_audit_request_id() == ""


def test_create_app_correlates_tool_audits_and_isolates_concurrent_requests(settings: Any) -> None:
    """Authenticated HTTP context reaches multiple real kernel audit writes."""

    from friday.server import create_app

    app = create_app(replace(settings, verify_answers=False))

    @app.post("/_test/audit-correlation")
    async def audit_correlation(request: Request) -> dict[str, bool]:
        actor = request.state.actor
        for _ in range(2):
            await request.app.state.kernel.execute("kg_stats", {}, actor=actor)
            await asyncio.sleep(0.01)
        return {"ok": True}

    base_headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        before = int(
            app.state.storage.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='tool.invoke' AND target_id='kg_stats'"
            ).fetchone()[0]
        )

        server_issued = client.post("/_test/audit-correlation", headers=base_headers)
        assert server_issued.status_code == 200, server_issued.text
        server_request_id = server_issued.headers["X-Request-ID"]
        assert len(server_request_id) == 24
        assert server_request_id.isascii() and server_request_id.isalnum()

        server_rows = app.state.storage.execute(
            """SELECT request_id FROM audit_log
                 WHERE action='tool.invoke' AND target_id='kg_stats'
                 ORDER BY rowid LIMIT 2 OFFSET ?""",
            (before,),
        ).fetchall()
        assert [str(row["request_id"] or "") for row in server_rows] == [
            server_request_id,
            server_request_id,
        ]

        def issue(client_request_id: str) -> Any:
            return client.post(
                "/_test/audit-correlation",
                headers={**base_headers, "X-Request-ID": client_request_id},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(issue, ("client.concurrent.a", "client.concurrent.b")))
        assert [response.status_code for response in responses] == [200, 200]
        assert {response.headers["X-Request-ID"] for response in responses} == {
            "client.concurrent.a",
            "client.concurrent.b",
        }

        concurrent_rows = app.state.storage.execute(
            """SELECT request_id FROM audit_log
                 WHERE action='tool.invoke' AND target_id='kg_stats'
                 ORDER BY rowid LIMIT 4 OFFSET ?""",
            (before + 2,),
        ).fetchall()
        correlations = [str(row["request_id"] or "") for row in concurrent_rows]
        assert len(correlations) == 4
        assert all(value.startswith("reqref_") for value in correlations)
        counts = {value: correlations.count(value) for value in set(correlations)}
        assert sorted(counts.values()) == [2, 2]

        background = _entry()
        app.state.storage.log_audit(background)
        assert _stored_request_id(app.state.storage, background.id) == ""
