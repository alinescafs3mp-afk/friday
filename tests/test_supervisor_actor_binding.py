from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import friday as friday_package
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
_INSTALLED_SITE_ENV = "FRIDAY_QUALITY_GATE_INSTALLED_SITE"
_UNAVAILABLE = b"semantic supervisor canary actor binding unavailable\n"
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


def _run_tool(
    database: Path,
    projection: bytes,
    *,
    environment: dict[str, str] | None = None,
    preload_friday: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    command = [sys.executable, "-I", "-B", str(TOOL), "--database", str(database)]
    if preload_friday:
        wrapper = (
            "import runpy,sys,types;"
            "sys.modules['friday.preloaded']=types.ModuleType('friday.preloaded');"
            "tool=sys.argv.pop(1);"
            "runpy.run_path(tool,run_name='__main__')"
        )
        command = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            wrapper,
            str(TOOL),
            "--database",
            str(database),
        ]
    return subprocess.run(
        command,
        input=projection,
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=10.0,
    )


def _minimal_installed_site(tmp_path: Path) -> Path:
    source = Path(str(friday_package.__file__)).resolve(strict=True).parent
    site = tmp_path / "wheel-site"
    (site / "friday" / "orchestration").mkdir(parents=True, mode=0o700)
    (site / "friday" / "permissions").mkdir(mode=0o700)
    site.chmod(0o700)
    (site / "friday").chmod(0o755)
    for relative in (
        Path("__init__.py"),
        Path("audit_privacy.py"),
        Path("id_provenance.py"),
        Path("permissions/__init__.py"),
        Path("user_ids.py"),
        Path("orchestration/supervisor_actor_binding.py"),
    ):
        destination = site / "friday" / relative
        shutil.copy2(source / relative, destination)
        destination.chmod(0o644)
    orchestration_init = site / "friday" / "orchestration" / "__init__.py"
    orchestration_init.write_text("", encoding="utf-8")
    orchestration_init.chmod(0o644)
    return site.resolve(strict=True)


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

    site = _minimal_installed_site(tmp_path)
    environment = {
        **os.environ,
        _INSTALLED_SITE_ENV: str(site),
        "PYTHONPATH": str(ROOT),
    }
    result = _run_tool(database, raw, environment=environment)
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout.decode("ascii").strip() == expected
    assert result.args[:3] == [sys.executable, "-I", "-B"]
    assert not tuple(site.rglob("__pycache__"))

    binding = site / "friday" / "orchestration" / "supervisor_actor_binding.py"
    binding.unlink()
    binding.symlink_to(ROOT / "friday" / "orchestration" / binding.name)
    actor = replace(_actor(), identity_id="tok_hostile_origin_secret")
    hostile_raw = json.dumps(_projection(actor), sort_keys=True).encode("utf-8")
    result = _run_tool(
        database,
        hostile_raw,
        environment={
            **os.environ,
            _INSTALLED_SITE_ENV: str(site),
            "PYTHONPATH": str(ROOT),
        },
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == _UNAVAILABLE
    assert b"tok_hostile_origin_secret" not in result.stdout + result.stderr
    assert _KEY.hex().encode("ascii") not in result.stdout + result.stderr

    linked_package_site = tmp_path / "linked-package-site"
    linked_package_site.mkdir(mode=0o700)
    (linked_package_site / "friday").symlink_to(ROOT / "friday", target_is_directory=True)
    result = _run_tool(
        database,
        hostile_raw,
        environment={
            **os.environ,
            _INSTALLED_SITE_ENV: str(linked_package_site.resolve(strict=True)),
        },
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == _UNAVAILABLE
    assert b"linked-package-site" not in result.stdout + result.stderr

    empty_site = tmp_path / "empty-wheel-site"
    empty_site.mkdir(mode=0o700)
    actor = replace(_actor(), identity_id="tok_pythonpath_secret")
    hostile_raw = json.dumps(_projection(actor), sort_keys=True).encode("utf-8")
    result = _run_tool(
        database,
        hostile_raw,
        environment={
            **os.environ,
            _INSTALLED_SITE_ENV: str(empty_site.resolve(strict=True)),
            "PYTHONPATH": str(ROOT),
        },
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == _UNAVAILABLE
    assert b"tok_pythonpath_secret" not in result.stdout + result.stderr
    assert _KEY.hex().encode("ascii") not in result.stdout + result.stderr

    preloaded = _run_tool(database, hostile_raw, preload_friday=True)
    assert preloaded.returncode == 2
    assert preloaded.stdout == b""
    assert preloaded.stderr == _UNAVAILABLE
    assert b"tok_pythonpath_secret" not in preloaded.stdout + preloaded.stderr

    canonical = tmp_path / "canonical-marker-private-value"
    canonical.mkdir(mode=0o700)
    permissive = tmp_path / "permissive-marker-private-value"
    permissive.mkdir(mode=0o755)
    permissive.chmod(0o755)
    marker_file = tmp_path / "file-marker-private-value"
    marker_file.write_text("not a package root", encoding="utf-8")
    marker_link = tmp_path / "link-marker-private-value"
    marker_link.symlink_to(canonical, target_is_directory=True)
    markers = (
        "",
        "relative-marker-private-value",
        f" {canonical}",
        str(tmp_path / "missing-marker-private-value"),
        str(permissive),
        str(marker_file),
        str(marker_link),
    )
    actor = replace(_actor(), identity_id="tok_marker_body_secret")
    hostile_raw = json.dumps(_projection(actor), sort_keys=True).encode("utf-8")
    for marker in markers:
        result = _run_tool(
            database,
            hostile_raw,
            environment={
                **os.environ,
                _INSTALLED_SITE_ENV: marker,
                "PYTHONPATH": str(ROOT),
            },
        )

        assert result.returncode == 2
        assert result.stdout == b""
        assert result.stderr == _UNAVAILABLE
        combined = result.stdout + result.stderr
        assert b"tok_marker_body_secret" not in combined
        assert b"marker-private-value" not in combined
        assert _KEY.hex().encode("ascii") not in combined


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
