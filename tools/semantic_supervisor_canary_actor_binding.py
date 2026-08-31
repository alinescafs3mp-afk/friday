#!/usr/bin/env python3
"""Print one private, deployment-local semantic-supervisor canary actor binding."""

from __future__ import annotations

import argparse
import os
import sqlite3
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_INSTALLED_SITE_ENV = "FRIDAY_QUALITY_GATE_INSTALLED_SITE"
_UNAVAILABLE = "semantic supervisor canary actor binding unavailable\n"


def _is_friday_module(name: str) -> bool:
    return name == "friday" or name.startswith("friday.")


def _reject_preloaded_friday_modules() -> None:
    if any(_is_friday_module(name) for name in sys.modules):
        raise RuntimeError("Friday runtime was preloaded")


def _package_root() -> Path:
    raw = os.environ.get(_INSTALLED_SITE_ENV)
    if raw is None:
        return ROOT
    candidate = Path(raw)
    try:
        status = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("installed wheel runtime is not canonical") from exc
    if (
        not raw
        or raw != raw.strip()
        or not candidate.is_absolute()
        or candidate != resolved
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise RuntimeError("installed wheel runtime is not canonical")
    return candidate


def _attest_friday_origins(package_root: Path) -> None:
    package_directory = package_root / "friday"
    package_init = package_directory / "__init__.py"
    package_metadata = package_directory.lstat()
    init_metadata = package_init.lstat()
    if (
        package_directory.resolve(strict=True) != package_directory
        or not stat.S_ISDIR(package_metadata.st_mode)
        or package_init.resolve(strict=True) != package_init
        or not stat.S_ISREG(init_metadata.st_mode)
        or (
            package_root != ROOT
            and (
                package_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(package_metadata.st_mode) & 0o022
                or init_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(init_metadata.st_mode) & 0o022
            )
        )
    ):
        raise RuntimeError("Friday package root is not canonical")
    expected_package = package_init
    for name, module in tuple(sys.modules.items()):
        if not _is_friday_module(name):
            continue
        raw_origin = getattr(module, "__file__", None)
        spec = getattr(module, "__spec__", None)
        raw_spec_origin = getattr(spec, "origin", None)
        if not isinstance(raw_origin, str) or not isinstance(raw_spec_origin, str):
            raise RuntimeError("Friday module origin is not canonical")
        origin = Path(raw_origin).resolve(strict=True)
        spec_origin = Path(raw_spec_origin).resolve(strict=True)
        if (
            origin != spec_origin
            or not origin.is_relative_to(package_directory)
            or (name == "friday" and origin != expected_package)
        ):
            raise RuntimeError("Friday module escaped the selected package runtime")
        raw_locations = getattr(module, "__path__", None)
        raw_spec_locations = getattr(spec, "submodule_search_locations", None)
        if raw_locations is None and raw_spec_locations is None:
            continue
        if raw_locations is None or raw_spec_locations is None:
            raise RuntimeError("Friday package origin is not canonical")
        locations = tuple(Path(location).resolve(strict=True) for location in raw_locations)
        spec_locations = tuple(Path(location).resolve(strict=True) for location in raw_spec_locations)
        expected_directory = package_directory.joinpath(*name.split(".")[1:]).resolve(strict=True)
        if (
            not locations
            or locations != spec_locations
            or any(location != expected_directory for location in locations)
        ):
            raise RuntimeError("Friday package has split origins")


try:
    _reject_preloaded_friday_modules()
    PACKAGE_ROOT = _package_root()
    sys.path.insert(0, str(PACKAGE_ROOT))

    from friday.orchestration.supervisor_actor_binding import (  # noqa: E402
        SupervisorCanaryActorBindingError,
        parse_supervisor_canary_actor_projection,
        supervisor_canary_actor_binding_from_transaction,
    )

    _attest_friday_origins(PACKAGE_ROOT)
except Exception:  # noqa: BLE001 - bootstrap faults must not disclose marker paths or actor material
    sys.stderr.write(_UNAVAILABLE)
    raise SystemExit(2) from None

_MAX_STDIN_BYTES = 4_096


def _open_private_read_only_database(path: Path) -> sqlite3.Connection:
    if not isinstance(path, Path) or not path.is_absolute():
        raise OSError("database is unavailable")
    lexical = Path(os.path.abspath(path))
    if lexical != path or lexical.resolve(strict=True) != lexical:
        raise OSError("database is unavailable")
    status = os.stat(lexical, follow_symlinks=False)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise OSError("database is unavailable")
    connection = sqlite3.connect(
        f"{lexical.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=5.0,
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise OSError("database is unavailable")
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read an exact ActorContext JSON projection from stdin and print only its "
            "deployment-local canary binding digest."
        )
    )
    parser.add_argument(
        "--database", required=True, type=Path, help="canonical private Friday SQLite database"
    )
    args = parser.parse_args(argv)
    connection: sqlite3.Connection | None = None
    try:
        raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
        actor = parse_supervisor_canary_actor_projection(raw)
        connection = _open_private_read_only_database(args.database)
        connection.execute("BEGIN")
        digest = supervisor_canary_actor_binding_from_transaction(connection, actor)
        _attest_friday_origins(PACKAGE_ROOT)
        connection.rollback()
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        SupervisorCanaryActorBindingError,
        TypeError,
        ValueError,
    ):
        if connection is not None:
            connection.rollback()
        sys.stderr.write(_UNAVAILABLE)
        return 2
    finally:
        if connection is not None:
            connection.close()
    sys.stdout.write(digest + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
