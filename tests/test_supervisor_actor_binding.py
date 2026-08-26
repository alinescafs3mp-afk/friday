from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.orchestration.supervisor_actor_binding import (
    SUPERVISOR_CANARY_ACTOR_BINDING_SCHEMA,
    SupervisorCanaryActorBindingError,
    parse_supervisor_canary_actor_projection,
    supervisor_canary_actor_binding_from_transaction,
    supervisor_canary_actor_binding_sha256,
)
from friday.permissions import ActorContext

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "semantic_supervisor_canary_actor_binding.py"
_KEY = bytes.fromhex("31" * 32)


def _actor() -> ActorContext:
    return ActorContext(
        user_id="tenant-7",
        preset_key="owner",
        source="api-token",
        identity_id="tok_abc123",
        session_id=None,
        shared_tenant=True,
        person_id="person-9",
    )


def _projection(actor: ActorContext | None = None) -> dict[str, object]:
    actor = actor or _actor()
    return {
        "schema": SUPERVISOR_CANARY_ACTOR_BINDING_SCHEMA,
        "user_id": actor.user_id,
        "preset_key": actor.preset_key,
        "source": actor.source,
        "identity_id": actor.identity_id,
        "session_id": actor.session_id,
        "shared_tenant": actor.shared_tenant,
        "person_id": actor.person_id,
    }


def _database(tmp_path: Path, *, key: bytes = _KEY) -> Path:
    path = (tmp_path / "friday.sqlite3").resolve()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('audit_privacy_hmac_key',?)",
            (key.hex(),),
        )
    path.chmod(0o600)
    return path


def _run_tool(database: Path, projection: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--database", str(database)],
        input=projection,
        capture_output=True,
        cwd=ROOT,
        check=False,
        timeout=10.0,
    )


def test_binding_is_restart_stable_keyed_and_contains_no_actor_material(tmp_path: Path) -> None:
    database = _database(tmp_path)
    raw = json.dumps(_projection(), sort_keys=True).encode("utf-8")

    first = _run_tool(database, raw)
    second = _run_tool(database, raw)

    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == b""
    assert first.stdout == second.stdout
    digest = first.stdout.decode("ascii").strip()
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    expected = supervisor_canary_actor_binding_sha256(_actor(), namespace_key=_KEY)
    assert digest == expected
    combined = first.stdout + first.stderr
    for private in (b"tenant-7", b"person-9", b"tok_abc123", _KEY.hex().encode("ascii")):
        assert private not in combined
    assert str(TOOL).encode() not in raw
    assert b"tenant-7" not in " ".join(first.args).encode()


@pytest.mark.parametrize(
    "changed",
    [
        {"user_id": "tenant-8"},
        {"preset_key": "admin"},
        {"source": "telegram-bridge"},
        {"identity_id": "tok_other"},
        {"session_id": "conv_1"},
        {"shared_tenant": False, "person_id": ""},
        {"person_id": "person-10"},
    ],
)
def test_every_exact_actor_field_drift_changes_the_binding(changed: dict[str, Any]) -> None:
    actor = _actor()
    drifted = replace(actor, **changed)

    assert supervisor_canary_actor_binding_sha256(
        actor,
        namespace_key=_KEY,
    ) != supervisor_canary_actor_binding_sha256(drifted, namespace_key=_KEY)


def test_namespace_key_drift_changes_the_binding() -> None:
    actor = _actor()
    assert supervisor_canary_actor_binding_sha256(
        actor,
        namespace_key=_KEY,
    ) != supervisor_canary_actor_binding_sha256(actor, namespace_key=b"2" * 32)


def test_transaction_helper_uses_the_single_durable_audit_privacy_key(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        actual = supervisor_canary_actor_binding_from_transaction(connection, _actor())

    assert actual == supervisor_canary_actor_binding_sha256(_actor(), namespace_key=_KEY)
    with sqlite3.connect(":memory:") as missing:
        missing.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        with pytest.raises(SupervisorCanaryActorBindingError, match="unavailable"):
            supervisor_canary_actor_binding_from_transaction(missing, _actor())


def test_operator_is_read_only_and_never_migrates_the_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    before = hashlib.sha256(database.read_bytes()).digest()
    raw = json.dumps(_projection()).encode("utf-8")

    result = _run_tool(database, raw)

    assert result.returncode == 0
    assert hashlib.sha256(database.read_bytes()).digest() == before
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT group_concat(name, ',') FROM sqlite_master").fetchone()[0] == (
            "schema_meta,sqlite_autoindex_schema_meta_1"
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"schema":"friday.supervisor-canary-actor-binding.v1","schema":"duplicate"}',
        json.dumps({**_projection(), "extra": "private-value"}).encode("utf-8"),
        json.dumps({**_projection(), "shared_tenant": 1}).encode("utf-8"),
        json.dumps({**_projection(), "person_id": ""}).encode("utf-8"),
        b"x" * 4_097,
    ],
)
def test_operator_rejects_malformed_nonexact_stdin_without_echo(tmp_path: Path, raw: bytes) -> None:
    result = _run_tool(_database(tmp_path), raw)

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"semantic supervisor canary actor binding unavailable\n"
    assert b"private-value" not in result.stderr


def test_projection_parser_and_programmer_type_errors_are_closed() -> None:
    raw = json.dumps(_projection()).encode("utf-8")
    assert parse_supervisor_canary_actor_projection(raw) == _actor()
    with pytest.raises(TypeError):
        parse_supervisor_canary_actor_projection("{}")  # type: ignore[arg-type]
    with pytest.raises(SupervisorCanaryActorBindingError):
        supervisor_canary_actor_binding_sha256(_actor(), namespace_key=b"short")
    with pytest.raises(TypeError):
        supervisor_canary_actor_binding_from_transaction(object(), _actor())
