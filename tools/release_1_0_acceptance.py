#!/usr/bin/env python3
"""Friday RC → 1.0 acceptance wrapper.

This is not a second quality-gate controller and cannot turn a red canonical
verdict green. It loads the capability matrix, classifies discovered surfaces,
audits sealed A/B inventories, runs harness self-tests, and prints the exact
diagnostic-baseline / final commands that still belong to the existing tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = Path(__file__).with_name("release_1_0_capability_matrix.json")
SCHEMA = "friday.release-1-0-acceptance.v1"
MATRIX_SCHEMA = "friday.release-1-0-capability-matrix.v1"
WRAPPER_RELATIVE = "tools/release_1_0_acceptance.py"
CANONICAL_TOOLS = (
    "tools/quality_gate.py",
    "tools/synthetic_live_acceptance.py",
    "tools/synthetic_live_battery.py",
    "tools/document_contour_live_battery.py",
)
OBLIGATIONS = frozenset(
    {"required", "optional_when_enabled", "beta", "out_of_scope"}
)
LAYERS = frozenset(
    {"deterministic", "isolated-live", "user-ui", "deployment-device", "harness"}
)
_API_DECORATOR = re.compile(
    r"@(?:application|router|admin_router|app)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)",
    re.M,
)
_CLI_PARSER = re.compile(r"add_parser\(\s*[\"']([a-z0-9-]+)")
_UI_VIEWS = re.compile(r"const views=\[(.*?)\];", re.S)
_UI_NAME = re.compile(r"\['([a-z0-9-]+)'")
_BOT_TUPLE = re.compile(r'\("([a-z_]+)",\s*"[^"]+"\)')
_BOT_COMMANDS_HEADER = "BOT_COMMANDS: tuple[tuple[str, str], ...] = ("
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_ROOT_METHOD_PATTERN = re.compile(r"^api:[A-Z]+ /$")


class AcceptanceError(RuntimeError):
    """Harness/oracle failure. Never a product PASS."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw or b"\0" in raw:
        raise AcceptanceError("matrix_unreadable")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("matrix_json_invalid") from exc
    if not isinstance(value, dict):
        raise AcceptanceError("matrix_not_object")
    if value.get("schema") != MATRIX_SCHEMA or value.get("schema_version") != 1:
        raise AcceptanceError("matrix_schema_mismatch")
    if value.get("not_a_backlog") is not True:
        raise AcceptanceError("matrix_must_not_be_a_backlog")
    capabilities = value.get("capabilities")
    cases = value.get("cases")
    rules = value.get("surface_rules")
    if not isinstance(capabilities, list) or not capabilities:
        raise AcceptanceError("matrix_capabilities_empty")
    if not isinstance(cases, list) or not cases:
        raise AcceptanceError("matrix_cases_empty")
    if not isinstance(rules, list) or not rules:
        raise AcceptanceError("matrix_surface_rules_empty")
    ids = [item.get("id") for item in capabilities if isinstance(item, dict)]
    if len(ids) != len(set(ids)) or any(not isinstance(item, str) or not item for item in ids):
        raise AcceptanceError("matrix_capability_ids_invalid")
    case_ids = [item.get("id") for item in cases if isinstance(item, dict)]
    if len(case_ids) != len(set(case_ids)):
        raise AcceptanceError("matrix_case_ids_duplicate")
    cap_set = set(ids)
    for case in cases:
        if not isinstance(case, dict):
            raise AcceptanceError("matrix_case_not_object")
        if case.get("capability_id") not in cap_set:
            raise AcceptanceError(f"matrix_case_unknown_capability:{case.get('id')}")
        if case.get("layer") not in LAYERS:
            raise AcceptanceError(f"matrix_case_layer_invalid:{case.get('id')}")
        if type(case.get("executable")) is not bool:
            raise AcceptanceError(f"matrix_case_executable_invalid:{case.get('id')}")
        if type(case.get("release_required")) is not bool:
            raise AcceptanceError(f"matrix_case_release_flag_invalid:{case.get('id')}")
    for rule in rules:
        if not isinstance(rule, dict) or not str(rule.get("pattern") or ""):
            raise AcceptanceError("matrix_surface_rule_invalid")
        if rule.get("capability_id") not in cap_set:
            raise AcceptanceError(f"matrix_rule_unknown_capability:{rule.get('pattern')}")
        if rule.get("obligation") not in OBLIGATIONS:
            raise AcceptanceError(f"matrix_rule_obligation_invalid:{rule.get('pattern')}")
    return value


def discover_telegram_commands(root: Path = ROOT) -> tuple[str, ...]:
    text = (root / "friday" / "telegram_bridge" / "_base.py").read_text(encoding="utf-8")
    start = text.find(_BOT_COMMANDS_HEADER)
    if start < 0:
        raise AcceptanceError("telegram_commands_unreadable")
    end = text.find("\n)\n", start)
    if end < 0:
        raise AcceptanceError("telegram_commands_unreadable")
    names = _BOT_TUPLE.findall(text[start:end])
    if "chat" not in names or "help" not in names:
        raise AcceptanceError("telegram_commands_unreadable")
    return tuple(f"telegram:{name}" for name in names)


def discover_ui_views(root: Path = ROOT) -> tuple[str, ...]:
    text = (root / "friday" / "admin_ui" / "static" / "app.js").read_text(encoding="utf-8")
    match = _UI_VIEWS.search(text)
    if match is None:
        raise AcceptanceError("admin_ui_views_unreadable")
    names = _UI_NAME.findall(match.group(1))
    if "dashboard" not in names or "inbox" not in names:
        raise AcceptanceError("admin_ui_views_incomplete")
    return tuple(f"ui:{name}" for name in names)


def discover_cli_commands(root: Path = ROOT) -> tuple[str, ...]:
    text = (root / "friday" / "cli.py").read_text(encoding="utf-8")
    names = _CLI_PARSER.findall(text)
    if "backup" not in names or "server" not in names:
        raise AcceptanceError("cli_commands_unreadable")
    # Preserve first-seen order, drop duplicates from help re-binds.
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(f"cli:{name}")
    return tuple(ordered)


def _source_router_prefix(path: Path, text: str, root: Path) -> str:
    prefix_match = re.search(
        r"APIRouter\(\s*(?:prefix\s*=\s*)?[\"'](/api/[^\"']*)[\"']",
        text,
    )
    if prefix_match:
        return prefix_match.group(1)
    try:
        relative = path.relative_to(root / "friday" / "admin_api")
    except ValueError:
        return ""
    if relative.as_posix() != "__init__.py":
        return "/api/admin"
    return ""


def discover_api_from_source(root: Path = ROOT) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for path in sorted((root / "friday").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        router_prefix = _source_router_prefix(path, text, root)
        for method, route in _API_DECORATOR.findall(text):
            if route.startswith("/api/") or route in {"/", "/health"} or route.startswith("/obsidian"):
                surface = f"api:{method.upper()} {route}"
            elif route.startswith("/"):
                surface = f"api:{method.upper()} {router_prefix}{route}"
            else:
                joined = f"{router_prefix}/{route}" if router_prefix else f"/{route}"
                surface = f"api:{method.upper()} {joined}"
            surface = surface.replace("//", "/")
            if surface not in seen:
                seen.add(surface)
                found.append(surface)
    if len(found) < 40:
        raise AcceptanceError("api_source_scan_too_small")
    return tuple(found)


def discover_api_from_openapi(settings: Any) -> tuple[str, ...]:
    from friday.server import create_app

    schema = create_app(settings).openapi()
    items = [
        f"api:{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in _HTTP_METHODS
    ]
    if len(items) < 100:
        raise AcceptanceError("openapi_surface_too_small")
    return tuple(sorted(items))


def discover_surfaces(*, settings: Any | None = None, root: Path = ROOT) -> dict[str, tuple[str, ...]]:
    api = discover_api_from_openapi(settings) if settings is not None else discover_api_from_source(root)
    return {
        "api": api,
        "telegram": discover_telegram_commands(root),
        "ui": discover_ui_views(root),
        "cli": discover_cli_commands(root),
    }


def _classification_keys(surface: str) -> tuple[str, ...]:
    keys = [surface]
    if surface.startswith("api:"):
        rest = surface[4:]
        if " " in rest:
            method, path = rest.split(" ", 1)
            if method in _HTTP_METHODS and path.startswith("/"):
                keys.append(f"api:{path}")
    return tuple(keys)


def _classify_one(surface: str, rules: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    ranked = sorted(rules, key=lambda item: len(str(item.get("pattern") or "")), reverse=True)
    keys = _classification_keys(surface)
    for rule in ranked:
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        root_only = _ROOT_METHOD_PATTERN.fullmatch(pattern) is not None
        for key in keys:
            if key == pattern:
                return rule
            if root_only:
                continue
            if key.startswith(pattern):
                return rule
    return None


def classify_surfaces(
    surfaces: Mapping[str, Sequence[str]],
    matrix: Mapping[str, Any],
) -> dict[str, Any]:
    rules = matrix["surface_rules"]
    classified: list[dict[str, Any]] = []
    unknown: list[str] = []
    for group in surfaces.values():
        for surface in group:
            rule = _classify_one(surface, rules)
            if rule is None:
                unknown.append(surface)
                continue
            classified.append(
                {
                    "surface": surface,
                    "capability_id": rule["capability_id"],
                    "obligation": rule["obligation"],
                }
            )
    required_unknown = unknown[:]  # any unclassified reachable surface is a coverage gap
    return {
        "classified": len(classified),
        "unknown": unknown,
        "unknown_count": len(unknown),
        "required_gap": required_unknown,
        "by_obligation": _count_by(classified, "obligation"),
        "by_capability": _count_by(classified, "capability_id"),
        "rows": classified,
    }


def _count_by(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row[key])] = counts.get(str(row[key]), 0) + 1
    return counts


def audit_sealed_batteries() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "tools"))
    import synthetic_live_acceptance as acceptance  # noqa: E402
    import synthetic_live_battery as battery  # noqa: E402

    pair = battery.audit_frozen_manifests()
    focused = acceptance.inventory_for_suite("focused")
    p06 = acceptance.inventory_for_suite("p06")
    combined = acceptance.inventory_for_suite("all")
    complaints: list[str] = []
    if pair.get("valid") is not True or pair.get("cases") != 400:
        complaints.append("sealed_pair_invalid")
    if combined.get("cases") != 160 or focused.get("cases") != 120 or p06.get("cases") != 40:
        complaints.append("sealed_acceptance_counts_invalid")
    if set(focused["pass_ids"]) & set(p06["pass_ids"]):
        complaints.append("sealed_acceptance_overlap")
    return {
        "valid": not complaints,
        "complaints": complaints,
        "pair": {"valid": pair.get("valid"), "cases": pair.get("cases"), "passes": pair.get("passes")},
        "acceptance": {
            "all": combined.get("cases"),
            "focused": focused.get("cases"),
            "p06": p06.get("cases"),
            "pass_ids": combined.get("pass_ids"),
        },
    }


def evaluate_oracle(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    """Code-owned comparison. Expected is frozen before the run."""

    failure_codes: list[str] = []
    if not observed:
        failure_codes.append("empty_observed")
    if expected.get("status_code") is not None and observed.get("status_code") != expected["status_code"]:
        failure_codes.append("status_code_mismatch")
    if expected.get("file_sha256"):
        got = str(observed.get("file_sha256") or "")
        if got != expected["file_sha256"]:
            failure_codes.append("digest_mismatch")
        if not got:
            failure_codes.append("missing_required_artifact")
    if expected.get("must_contain"):
        body = str(observed.get("body") or "")
        for token in expected["must_contain"]:
            if str(token) not in body:
                failure_codes.append("missing_required_token")
                break
    foreign = expected.get("foreign_canaries") or observed.get("foreign_canaries") or []
    body = str(observed.get("body") or "")
    for marker in foreign:
        if marker and str(marker) in body:
            failure_codes.append("privacy_canary_exposed")
            break
    if expected.get("effect_forbidden") is True and observed.get("effect") is True:
        failure_codes.append("forbidden_effect")
    if expected.get("min_count") is not None:
        if int(observed.get("count") or 0) < int(expected["min_count"]):
            failure_codes.append("count_below_minimum")
    if not observed.get("collected", True):
        raise AcceptanceError("zero_collected_cases")
    status = "FAIL" if failure_codes else "PASS"
    return {
        "status": status,
        "failure_codes": failure_codes,
        "expected_outcome": expected.get("expected_outcome"),
        "observed_keys": sorted(observed),
    }


def negative_controls() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    digest = evaluate_oracle(
        {"file_sha256": "a" * 64, "expected_outcome": "pass"},
        {"file_sha256": "b" * 64, "collected": True},
    )
    if digest["status"] != "FAIL" or "digest_mismatch" not in digest["failure_codes"]:
        raise AcceptanceError("negative_control_digest_did_not_fail")
    results.append({"id": "R10-NEG-CTRL-WRONG-DIGEST", "status": "PASS", "proved": "oracle_goes_red"})

    try:
        evaluate_oracle({"expected_outcome": "pass"}, {"collected": False})
    except AcceptanceError as exc:
        if str(exc) != "zero_collected_cases":
            raise
    else:
        raise AcceptanceError("negative_control_empty_collection_did_not_fail")
    results.append(
        {"id": "R10-NEG-CTRL-EMPTY-COLLECTION", "status": "PASS", "proved": "harness_error"}
    )

    canary = evaluate_oracle(
        {"expected_outcome": "pass", "foreign_canaries": ["SYN-FOREIGN-DEADBEEF"]},
        {"body": "ok SYN-FOREIGN-DEADBEEF", "collected": True},
    )
    if canary["status"] != "FAIL" or "privacy_canary_exposed" not in canary["failure_codes"]:
        raise AcceptanceError("negative_control_canary_did_not_fail")
    results.append({"id": "R10-NEG-CTRL-FOREIGN-CANARY", "status": "PASS", "proved": "oracle_goes_red"})
    return {"valid": True, "results": results}


def preflight(*, root: Path = ROOT) -> dict[str, Any]:
    complaints: list[str] = []
    python = sys.version_info
    if (python.major, python.minor) < (3, 13):
        complaints.append("python_too_old")
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.is_file() or not os.access(bwrap, os.X_OK):
        complaints.append("bwrap_missing")
    scratch = Path("/var/tmp")
    try:
        usage = shutil.disk_usage(scratch)
        free_gi = usage.free / (1024 ** 3)
    except OSError:
        free_gi = 0.0
        complaints.append("var_tmp_unreadable")
    cpus = os.cpu_count() or 0
    for relative in (*CANONICAL_TOOLS, WRAPPER_RELATIVE, "tools/release_1_0_capability_matrix.json"):
        path = root / relative
        if not path.is_file():
            complaints.append(f"missing:{relative}")
    return {
        "valid": not complaints,
        "complaints": complaints,
        "python": f"{python.major}.{python.minor}.{python.micro}",
        "cpus": cpus,
        "var_tmp_free_gi": round(free_gi, 2),
        "bwrap": str(bwrap) if bwrap.is_file() else None,
        "secrets_emitted": False,
        "note": "exact-release still requires 24 CPU and 32 GiB free /var/tmp; this preflight does not lower that floor",
    }


def exclusive_slot_busy() -> list[str]:
    busy: list[str] = []
    try:
        result = subprocess.run(
            ("ps", "-eo", "args="),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["ps_unavailable"]
    text = result.stdout or ""
    needles = (
        "tools/quality_gate.py --tier",
        "tools/synthetic_live_acceptance.py --suite",
        "tools/synthetic_live_battery.py --both",
        "tools/document_contour_live_battery.py",
    )
    for needle in needles:
        if needle in text:
            busy.append(needle)
    return busy


def plan_commands(mode: str) -> dict[str, Any]:
    if mode not in {"diagnostic-baseline", "final"}:
        raise AcceptanceError("unknown_mode")
    common = [
        "umask 077",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --audit-only",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --preflight",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_acceptance.py --negative-control",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py --audit-only",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py --suite all --audit-only",
        (
            ".venv/bin/python -I -B -m pytest -q "
            "tests/test_release_1_0_acceptance.py tests/test_release_1_0_journeys.py"
        ),
    ]
    exact = [
        "candidate_sha=\"$(git rev-parse --verify 'HEAD^{commit}')\"",
        "base_sha=\"$(git rev-parse --verify \"${QUALITY_GATE_BASE_SHA:?set accepted base}^{commit}\")\"",
        "evidence_dir=\"$(mktemp -d -p /var/tmp friday-exact-evidence.XXXXXXXX)\"",
        ".venv/bin/python -I -B tools/quality_gate.py --tier exact-release "
        "--candidate-sha \"$candidate_sha\" --base-sha \"$base_sha\" --evidence-dir \"$evidence_dir\"",
    ]
    live = [
        "test -n \"${FRIDAY_ENV_FILE:-}\"",
        "umask 077",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_acceptance.py "
        "--env-file \"$FRIDAY_ENV_FILE\" --suite all --concurrency 4",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/synthetic_live_battery.py "
        "--env-file \"$FRIDAY_ENV_FILE\" --both --concurrency 4",
        "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/release_1_0_live_journeys.py "
        "--env-file \"$FRIDAY_ENV_FILE\" --run-live",
    ]
    return {
        "mode": mode,
        "order": [
            "harness and matrix audit",
            "exact-release quality_gate (canonical, no imported receipt)",
            "canonical 160-case live acceptance",
            "official A then B; B only if A is green",
            "additional R10 live journeys on the same frozen candidate",
        ],
        "commands": {
            "harness": common,
            "exact_release": exact,
            "live": live,
        },
        "cannot": [
            "emit GO",
            "start B after a red A",
            "skip required cases because of budget",
            "rewrite sealed A/B manifests",
            "use the live production home or live Telegram singleton",
            "overlap another full native/UI/model-heavy gate",
        ],
        "go_rule": "conjunction of required cases, no open high/critical defects, no required gaps, exact identities; this wrapper never prints GO",
    }


def matrix_summary(matrix: Mapping[str, Any], classified: Mapping[str, Any], sealed: Mapping[str, Any]) -> dict[str, Any]:
    cases = [case for case in matrix["cases"] if isinstance(case, dict)]
    executable = [case for case in cases if case.get("executable") is True]
    required = [case for case in executable if case.get("release_required") is True]
    live = [case for case in required if case.get("layer") == "isolated-live"]
    blocked = [case for case in cases if case.get("executable") is False]
    return {
        "schema": SCHEMA,
        "revision": matrix.get("revision"),
        "matrix_sha256": _sha256_file(MATRIX_PATH),
        "wrapper_sha256": _sha256_file(Path(__file__).resolve()),
        "capabilities": len(matrix["capabilities"]),
        "additional_cases": len(cases),
        "additional_executable": len(executable),
        "additional_required_executable": len(required),
        "additional_required_live": len(live),
        "additional_not_executable": [
            {"id": case["id"], "reason": case.get("blocked_reason")} for case in blocked
        ],
        "sealed_unique_cases": 400,
        "sealed_acceptance_executions": 160,
        "surface_unknown": classified.get("unknown"),
        "sealed_audit_valid": sealed.get("valid"),
        "go_emitted": False,
        "product_accepted_1_0": False,
    }


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Friday 1.0 acceptance wrapper (not a second gate)")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--negative-control", action="store_true")
    parser.add_argument("--plan", choices=("diagnostic-baseline", "final"))
    args = parser.parse_args(argv)
    selected = [bool(args.audit_only), bool(args.preflight), bool(args.negative_control), bool(args.plan)]
    if sum(selected) != 1:
        parser.error("choose exactly one of --audit-only, --preflight, --negative-control, --plan")
    try:
        matrix = load_matrix()
        if args.preflight:
            report = preflight()
            _print(report)
            return 0 if report["valid"] else 2
        if args.negative_control:
            _print(negative_controls())
            return 0
        if args.plan:
            busy = exclusive_slot_busy()
            payload = plan_commands(args.plan)
            payload["exclusive_slot_busy"] = busy
            payload["go_emitted"] = False
            _print(payload)
            return 0
        surfaces = discover_surfaces()
        classified = classify_surfaces(surfaces, matrix)
        sealed = audit_sealed_batteries()
        summary = matrix_summary(matrix, classified, sealed)
        summary["surface_counts"] = {key: len(value) for key, value in surfaces.items()}
        summary["classified"] = classified["classified"]
        summary["complaints"] = list(classified["unknown"])
        if sealed.get("valid") is not True:
            summary["complaints"] = list(summary["complaints"]) + list(sealed.get("complaints") or [])
        valid = sealed.get("valid") is True
        summary["valid"] = valid
        _print(summary)
        return 0 if valid else 2
    except AcceptanceError as exc:
        _print({"schema": SCHEMA, "valid": False, "error": str(exc), "go_emitted": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
