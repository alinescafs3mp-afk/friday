#!/usr/bin/env python3
"""Emit canonical, body-free synthetic P1 semantic-supervisor replay evidence."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_INSTALLED_SITE_ENV = "FRIDAY_QUALITY_GATE_INSTALLED_SITE"
_UNAVAILABLE = "semantic supervisor offline evaluation unavailable\n"


def _is_friday_module(name: str) -> bool:
    return name == "friday" or name.startswith("friday.")


def _reject_preloaded_friday_modules() -> None:
    if any(_is_friday_module(name) for name in sys.modules):
        raise RuntimeError("semantic supervisor runtime was preloaded")


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
        or status.st_mode & 0o077
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
        raise RuntimeError("semantic supervisor package root is not canonical")
    expected_package = package_init
    for name, module in tuple(sys.modules.items()):
        if not _is_friday_module(name):
            continue
        raw_origin = getattr(module, "__file__", None)
        spec = getattr(module, "__spec__", None)
        raw_spec_origin = getattr(spec, "origin", None)
        if not isinstance(raw_origin, str) or not isinstance(raw_spec_origin, str):
            raise RuntimeError("semantic supervisor module origin is not canonical")
        try:
            origin = Path(raw_origin).resolve(strict=True)
            spec_origin = Path(raw_spec_origin).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("semantic supervisor module origin is not canonical") from exc
        if (
            origin != spec_origin
            or not origin.is_relative_to(package_directory)
            or (name == "friday" and origin != expected_package)
        ):
            raise RuntimeError("semantic supervisor escaped the selected package runtime")

        raw_locations = getattr(module, "__path__", None)
        raw_spec_locations = getattr(spec, "submodule_search_locations", None)
        if raw_locations is None and raw_spec_locations is None:
            continue
        if raw_locations is None or raw_spec_locations is None:
            raise RuntimeError("semantic supervisor package origin is not canonical")
        try:
            locations = tuple(Path(location).resolve(strict=True) for location in raw_locations)
            spec_locations = tuple(Path(location).resolve(strict=True) for location in raw_spec_locations)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError("semantic supervisor package origin is not canonical") from exc
        expected_directory = package_directory.joinpath(*name.split(".")[1:]).resolve(strict=True)
        if (
            not locations
            or locations != spec_locations
            or any(location != expected_directory for location in locations)
        ):
            raise RuntimeError("semantic supervisor package has split origins")


try:
    _reject_preloaded_friday_modules()
    PACKAGE_ROOT = _package_root()
    sys.path.insert(0, str(PACKAGE_ROOT))

    import friday as _friday_package  # noqa: E402

    if sys.modules.get("friday") is not _friday_package:
        raise RuntimeError("semantic supervisor package binding is not canonical")
    _attest_friday_origins(PACKAGE_ROOT)

    from friday.orchestration.supervisor_contracts import canonical_dumps  # noqa: E402
    from friday.orchestration.supervisor_offline_evaluation import (  # noqa: E402
        OfflineEvaluationError,
        evaluate_offline_fixture_set,
    )

    _attest_friday_origins(PACKAGE_ROOT)
except Exception:  # noqa: BLE001 - bootstrap diagnostics must not disclose private paths
    sys.stderr.write(_UNAVAILABLE)
    raise SystemExit(2) from None

DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "semantic_supervisor_offline_v1.json"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OfflineEvaluationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise OfflineEvaluationError(f"non-finite JSON number is forbidden: {value}")


def load_fixture_file(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Replay synthetic fixtures offline; output is not live shadow or canary acceptance.")
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help="closed synthetic fixture set (default: repository P1 fixture set)",
    )
    args = parser.parse_args(argv)
    try:
        report = evaluate_offline_fixture_set(load_fixture_file(args.fixtures))
    except (OSError, UnicodeError, json.JSONDecodeError, OfflineEvaluationError) as error:
        parser.error(str(error))
    try:
        _attest_friday_origins(PACKAGE_ROOT)
    except Exception:  # noqa: BLE001 - authority diagnostics must remain path-free
        sys.stderr.write(_UNAVAILABLE)
        return 2
    sys.stdout.write(canonical_dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
