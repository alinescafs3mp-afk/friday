"""Real data-source argv paths over owned SQLite files and real Friday storage.

The CLI handler, provider and storage stay unmocked.  Only settings loading and
local-env loading are redirected to the pytest-owned runtime.  Literal output
oracles are constructed before every call.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from friday import cli, config
from friday.diagnostics.runtime_lease import process_owns_lease

ALICE = "cli089-source-alice"
BOB = "cli089-source-bob"
NAME = "hr"
ALICE_ENV = "CLI089_ALICE_HR_DSN"
ALICE_REPLICA_ENV = "CLI089_ALICE_HR_REPLICA_DSN"
BOB_ENV = "CLI089_BOB_HR_DSN"
MISSING_ENV = "CLI089_MISSING_HR_DSN"
BROKEN_ENV = "CLI089_BROKEN_KIND_DSN"
PRIVATE_DSN_CANARY = "sqlite:///tmp/CLI089_PRIVATE_DSN_CANARY"
TABLE_COLUMNS = (
    "name",
    "user_id",
    "kind",
    "dsn_env",
    "description",
    "created_at",
    "created_by",
    "last_used_at",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_rows(storage) -> list[dict]:
    columns = ",".join(TABLE_COLUMNS)
    return [
        dict(row)
        for row in storage.execute(f"SELECT {columns} FROM data_sources ORDER BY user_id,name").fetchall()
    ]


def _foreign_rows(storage) -> list[dict]:
    return [row for row in _source_rows(storage) if row["user_id"] == BOB]


def _make_external(path: Path, first: str, second: str, metric: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executescript(
            """
            CREATE TABLE staff(id INTEGER PRIMARY KEY, canary TEXT NOT NULL);
            CREATE TABLE metrics(value INTEGER NOT NULL);
            """
        )
        connection.executemany(
            "INSERT INTO staff(id,canary) VALUES(?,?)",
            [(1, first), (2, second)],
        )
        connection.execute("INSERT INTO metrics(value) VALUES(?)", (metric,))
        connection.commit()


def _lease_owned(state_dir: Path, name: str) -> bool:
    protocol = "friday.account-deletion.v1" if name == "account-deletion" else "friday.backend.v1"
    return process_owns_lease(state_dir / f"{name}.lock", protocol=protocol)


def _lease_metadata(state_dir: Path, name: str) -> dict:
    return json.loads((state_dir / f"{name}.lock").read_text())


@pytest.fixture
def source_cli(settings, storage, monkeypatch, tmp_path):
    assert not settings.llm_enabled and not settings.workers_enabled
    external = {
        "alice": tmp_path / "alice-private-source.sqlite3",
        "bob": tmp_path / "bob-private-source.sqlite3",
    }
    _make_external(external["alice"], "ALICE_PRIVATE", "ALICE_SECOND", 7)
    _make_external(external["bob"], "BOB_PRIVATE", "BOB_SECOND", 19)
    external_hashes = {key: _sha256(path) for key, path in external.items()}

    monkeypatch.setenv(ALICE_ENV, str(external["alice"]))
    monkeypatch.setenv(ALICE_REPLICA_ENV, str(external["alice"]))
    monkeypatch.setenv(BOB_ENV, str(external["bob"]))
    monkeypatch.setenv(BROKEN_ENV, str(external["alice"]))
    monkeypatch.delenv(MISSING_ENV, raising=False)

    storage.ensure_user(ALICE, source="fixture", external_id=ALICE)
    storage.ensure_user(BOB, source="fixture", external_id=BOB)
    storage.register_data_source(
        BOB,
        name=NAME,
        kind="sqlite",
        dsn_env=BOB_ENV,
        description="Чужой одноимённый источник",
        created_by=BOB,
    )

    observations: list[tuple[bool, bool]] = []
    armed = [False]
    state_dir = settings.state_dir

    class LeaseWitnessPath(type(Path())):
        """Observe the actual main-DB lstat without replacing storage/provider."""

        def lstat(self):
            if armed[0]:
                observations.append(
                    (
                        _lease_owned(state_dir, "account-deletion"),
                        _lease_owned(state_dir, "backend"),
                    )
                )
            return super().lstat()

    witnessed_database = LeaseWitnessPath(settings.database_path)
    cli_settings = replace(
        settings,
        database_path=witnessed_database,
        database_must_exist=True,
    )
    monkeypatch.setattr(config, "load_settings", lambda: cli_settings)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)

    return {
        "settings": settings,
        "cli_settings": cli_settings,
        "storage": storage,
        "external": external,
        "external_hashes": external_hashes,
        "observations": observations,
        "armed": armed,
    }


def _run(source_cli, monkeypatch, capsys, expected, *arguments, opens_storage=True):
    """Invoke real sys.argv/main and compare to an oracle frozen by the caller."""

    state_dir = source_cli["settings"].state_dir
    started = datetime.now(UTC)
    source_cli["observations"].clear()
    source_cli["armed"][0] = True
    capsys.readouterr()
    monkeypatch.setattr(
        sys,
        "argv",
        ["friday", "--log-level", "CRITICAL", "data-source", *arguments],
    )
    try:
        with pytest.raises(SystemExit) as stopped:
            cli.main()
    finally:
        source_cli["armed"][0] = False
    ended = datetime.now(UTC)
    assert type(stopped.value.code) is int
    captured = capsys.readouterr()
    actual = (stopped.value.code, captured.out, captured.err)
    assert actual == expected

    # Both real ProcessLease instances wrote their own evidence for this call.
    for name, protocol in (
        ("account-deletion", "friday.account-deletion.v1"),
        ("backend", "friday.backend.v1"),
    ):
        metadata = _lease_metadata(state_dir, name)
        assert metadata["protocol"] == protocol and metadata["pid"] == os.getpid()
        assert started <= datetime.fromisoformat(metadata["acquired_at"]) <= ended
        assert not _lease_owned(state_dir, name), "CLI returned without releasing its lease"

    if opens_storage:
        assert source_cli["observations"], "actual Friday database was never entered"
        assert set(source_cli["observations"]) == {(True, True)}, (
            "Friday storage was entered outside one of the two real CLI leases",
            source_cli["observations"],
        )
    else:
        assert source_cli["observations"] == []
    return actual


def _assert_external_and_foreign_unchanged(source_cli, foreign_before):
    storage = source_cli["storage"]
    assert _foreign_rows(storage) == foreign_before
    for key, path in source_cli["external"].items():
        # Ordinary reopen proves the canary is still readable from the same file.
        with sqlite3.connect(path) as connection:
            observed = connection.execute("SELECT canary FROM staff ORDER BY id").fetchall()
        expected = {
            "alice": [("ALICE_PRIVATE",), ("ALICE_SECOND",)],
            "bob": [("BOB_PRIVATE",), ("BOB_SECOND",)],
        }[key]
        assert observed == expected
        assert _sha256(path) == source_cli["external_hashes"][key]


def test_real_cli_covers_all_five_source_actions_and_owner_boundaries(source_cli, monkeypatch, capsys):
    storage = source_cli["storage"]
    foreign_before = _foreign_rows(storage)
    assert [row["name"] for row in foreign_before] == [NAME]
    assert foreign_before[0]["dsn_env"] == BOB_ENV

    expected_empty = (0, "Источников не объявлено.\n", "")
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_empty,
        "list",
        "--user",
        ALICE,
    )
    _assert_external_and_foreign_unchanged(source_cli, foreign_before)

    expected_add = (0, "Источник «hr» объявлен (sqlite).\n", "")
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_add,
        "add",
        "--user",
        ALICE,
        "--name",
        NAME,
        "--kind",
        "sqlite",
        "--dsn-env",
        ALICE_ENV,
        "--description",
        "Алиса",
    )
    added = storage.get_data_source(ALICE, NAME)
    assert added is not None and added["last_used_at"] is None
    original_creation = (added["created_at"], added["created_by"])
    _assert_external_and_foreign_unchanged(source_cli, foreign_before)

    # Redeclare is the same add action and must update only Alice's own row.
    expected_redeclare = expected_add
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_redeclare,
        "add",
        "--user",
        ALICE,
        "--name",
        NAME,
        "--kind",
        "sqlite",
        "--dsn-env",
        ALICE_REPLICA_ENV,
        "--description",
        "Алиса новая",
    )
    redeclared = storage.get_data_source(ALICE, NAME)
    assert redeclared is not None
    assert (redeclared["dsn_env"], redeclared["description"]) == (
        ALICE_REPLICA_ENV,
        "Алиса новая",
    )
    assert (redeclared["created_at"], redeclared["created_by"]) == original_creation
    assert redeclared["last_used_at"] is None
    expected_list_before_query = (
        0,
        f"{'hr':20s} {'sqlite':9s} {ALICE_REPLICA_ENV:28s} "
        "строка: задана; спрашивали: ни разу\n    Алиса новая\n",
        "",
    )
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_list_before_query,
        "list",
        "--user",
        ALICE,
    )
    _assert_external_and_foreign_unchanged(source_cli, foreign_before)

    expected_schema = (
        0,
        "Таблиц: 2\n  metrics: value INTEGER\n  staff: id INTEGER, canary TEXT\n",
        "",
    )
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_schema,
        "describe",
        "--user",
        ALICE,
        "--name",
        NAME,
    )
    assert storage.get_data_source(ALICE, NAME)["last_used_at"] is None
    _assert_external_and_foreign_unchanged(source_cli, foreign_before)

    expected_query = (
        0,
        "id | canary\n1 | ALICE_PRIVATE\n2 | ALICE_SECOND\nСтрок: 2\n",
        "",
    )
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_query,
        "query",
        "--user",
        ALICE,
        "--name",
        NAME,
        "--query",
        "select id, canary from staff order by id",
    )
    touched = storage.get_data_source(ALICE, NAME)
    assert touched is not None and touched["last_used_at"] is not None
    datetime.fromisoformat(touched["last_used_at"])
    _assert_external_and_foreign_unchanged(source_cli, foreign_before)

    # Freeze the exact list line after the successful query has produced its clock.
    expected_list_after_query = (
        0,
        f"{'hr':20s} {'sqlite':9s} {ALICE_REPLICA_ENV:28s} "
        f"строка: задана; спрашивали: {touched['last_used_at']}\n    Алиса новая\n",
        "",
    )
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_list_after_query,
        "list",
        "--user",
        ALICE,
    )

    expected_forget = (0, "Забыт.\n", "")
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_forget,
        "forget",
        "--user",
        ALICE,
        "--name",
        NAME,
    )
    assert storage.get_data_source(ALICE, NAME) is None
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_empty,
        "list",
        "--user",
        ALICE,
    )
    _assert_external_and_foreign_unchanged(source_cli, foreign_before)

    # Reopen the real registry independently: Bob's same-name source survived.
    with sqlite3.connect(source_cli["settings"].database_path) as connection:
        connection.row_factory = sqlite3.Row
        bob = dict(
            connection.execute(
                "SELECT name,user_id,kind,dsn_env,description,created_at,created_by,last_used_at "
                "FROM data_sources WHERE user_id=? AND name=?",
                (BOB, NAME),
            ).fetchone()
        )
    assert bob == foreign_before[0]


def test_real_cli_refusals_have_exact_codes_no_touch_or_secret_persistence(source_cli, monkeypatch, capsys):
    storage = source_cli["storage"]
    foreign_before = _foreign_rows(storage)
    storage.register_data_source(
        ALICE,
        name=NAME,
        kind="sqlite",
        dsn_env=ALICE_ENV,
        description="Алиса",
        created_by=ALICE,
    )
    storage.register_data_source(
        ALICE,
        name="offline",
        kind="sqlite",
        dsn_env=MISSING_ENV,
        description="Нет переменной",
        created_by=ALICE,
    )
    # A corrupt/legacy kind exercises SourceUnavailableError without importing a
    # SQL extra or attempting postgres/mysql/LAN access.
    storage.register_data_source(
        ALICE,
        name="broken",
        kind="oracle",
        dsn_env=BROKEN_ENV,
        description="Недоступный вид",
        created_by=ALICE,
    )
    baseline = _source_rows(storage)

    expected_missing = (1, "", "Источник «missing» не объявлен.\n")
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_missing,
        "query",
        "--user",
        ALICE,
        "--name",
        "missing",
        "--query",
        "select 1",
    )
    assert _source_rows(storage) == baseline

    expected_env = (
        2,
        "",
        f"Переменная {MISSING_ENV} не задана — подключаться нечем.\n",
    )
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_env,
        "describe",
        "--user",
        ALICE,
        "--name",
        "offline",
    )
    assert storage.get_data_source(ALICE, "offline")["last_used_at"] is None

    expected_unsafe = (
        2,
        "",
        "Разрешён ровно один запрос: точка с запятой внутри запрещена\n",
    )
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_unsafe,
        "query",
        "--user",
        ALICE,
        "--name",
        NAME,
        "--query",
        "select * from staff; drop table staff",
    )
    assert storage.get_data_source(ALICE, NAME)["last_used_at"] is None

    expected_unavailable = (2, "", "Неизвестный вид источника 'oracle'\n")
    _run(
        source_cli,
        monkeypatch,
        capsys,
        expected_unavailable,
        "query",
        "--user",
        ALICE,
        "--name",
        "broken",
        "--query",
        "select 1",
    )
    assert storage.get_data_source(ALICE, "broken")["last_used_at"] is None
    assert _source_rows(storage) == baseline
    _assert_external_and_foreign_unchanged(source_cli, foreign_before)

    for arguments in (
        (
            "add",
            "--user",
            ALICE,
            "--name",
            "ЛОМ",
            "--kind",
            "sqlite",
            "--dsn-env",
            ALICE_ENV,
        ),
        (
            "add",
            "--user",
            ALICE,
            "--name",
            "bad-dsn",
            "--kind",
            "sqlite",
            "--dsn-env",
            PRIVATE_DSN_CANARY,
        ),
    ):
        expected_invalid = (2, "", "")
        _run(
            source_cli,
            monkeypatch,
            capsys,
            expected_invalid,
            *arguments,
            # init_storage constructs a lazy handle. Invalid declarations fail
            # before the first connection, so the database lstat is not reached.
            opens_storage=False,
        )
        assert _source_rows(storage) == baseline
        combined = json.dumps(_source_rows(storage), ensure_ascii=False)
        assert PRIVATE_DSN_CANARY not in combined
    assert storage.get_data_source(ALICE, "ЛОМ") is None
    assert storage.get_data_source(ALICE, "bad-dsn") is None
    _assert_external_and_foreign_unchanged(source_cli, foreign_before)
