#!/usr/bin/env python3
"""A small, isolated live battery for Friday's document contour.

This runner is deliberately separate from ``synthetic_live_battery.py``.  It
executes exactly ten document scenarios twice, sequentially, against real local
LLM/embedding/reranker services while every database, uploaded byte, MCP file,
prompt and model response lives below a fresh private temporary directory.

The controller refuses to start without an explicit frozen commit and an
operator assertion that the Telegram bridge is stopped.  ``--self-test`` is
offline: it never imports ``friday.server`` and never contacts a sidecar.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
_root_import_path = str(ROOT)
if not sys.path or sys.path[0] != _root_import_path:
    sys.path.insert(0, _root_import_path)

# An editable virtualenv can otherwise resolve ``friday`` from a different,
# dirty checkout even when this controller itself lives in an immutable release
# worktree.  Pin and attest the package origin before any Friday submodule is
# imported by a worker scenario.
import friday as _friday_package  # noqa: E402

_friday_origin = Path(str(_friday_package.__file__ or "")).resolve()
if not _friday_origin.is_relative_to(ROOT):
    raise RuntimeError("Friday package origin is outside the frozen release root")

RUNS = 2
CASES = 10
WORKER_TIMEOUT_SEC = 1_800
SCHEMA = "friday.document-contour-live-battery.v1"
WORKER_SCHEMA = "friday.document-contour-live-battery.worker.v1"
REPORT_SCHEMA = "friday.document-contour-live-battery.report.v1"
_RUN_ID_ENV = "FRIDAY_DOCUMENT_BATTERY_RUN_ID"
_RUN_ID_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Scenario:
    case_id: str
    title: str
    contract: tuple[str, ...]


@dataclass(frozen=True)
class CaseIdentity:
    """Private invocation namespace for one run/case fixture universe."""

    run_id: str = field(repr=False)
    run_index: int
    case_id: str

    def token(self, purpose: str, *, length: int = 16) -> str:
        if not purpose or not 8 <= length <= 32:
            raise BatteryFailure("case_identity_request_invalid")
        payload = f"{self.run_index}\0{self.case_id}\0{purpose}".encode()
        return hashlib.sha256(bytes.fromhex(self.run_id) + b"\0" + payload).hexdigest()[:length]

    @property
    def cache_prefix(self) -> str:
        return f"docbat-{self.case_id.casefold()}-{self.token('cache-prefix')}"

    def marker(self, label: str) -> str:
        return f"{label}-{self.token('marker:' + label, length=12).upper()}"

    def source_ref(self, label: str) -> str:
        return f"telegram-file:{label}-{self.token('source-ref:' + label)}"

    def filename(self, stem: str, extension: str) -> str:
        suffix = self.token(f"filename:{stem}:{extension}", length=12)
        return f"{stem}-{suffix}.{extension.lstrip('.')}"

    def prompt_variant(self, key: str, count: int) -> int:
        if not key or not 1 <= count <= 2:
            raise BatteryFailure("prompt_variant_contract_invalid")
        return int(self.token("prompt-variant:" + key, length=8), 16) % count


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "D01",
        "dedup re-upload reply pointer",
        (
            "same ODT bytes under two Telegram file refs resolve to one canonical Raw",
            "reply to the second ref selects only that canonical file, never a newer decoy",
        ),
    ),
    Scenario(
        "D02",
        "reply to prior assistant restores exact source",
        (
            "the prior assistant row owns exactly the source it used",
            "reply_source_message_id restores it and cannot drift to a newer/deleted/foreign file",
        ),
    ),
    Scenario(
        "D03",
        "fuzzy filename navigation",
        (
            "approximate stem/abbreviation/typo selects the intended document",
            "a newer differently named spreadsheet is not substituted",
        ),
    ),
    Scenario(
        "D04",
        "semantic XLSX heading lookup",
        (
            "real object/chunk embeddings are current before the query",
            "query-time embeddings and reranker both run and preserve canonical evidence",
            "the heading-bound target wins without a false absence",
        ),
    ),
    Scenario(
        "D05",
        "uploader and received-at aggregation",
        (
            "unique short typo GBL resolves to JBL",
            "arrival-date range returns only JBL files in exact descending order",
        ),
    ),
    Scenario(
        "D06",
        "small ODT fit-first summary",
        (
            "bare small-file summary uses complete current-turn text",
            "no false partial-material warning and no outside-deed refusal",
        ),
    ),
    Scenario(
        "D07",
        "multipage scan OCR beyond page four",
        (
            "OCR reads the fifth page and returns its marker",
            "coverage is explicit and advisory evidence is never called verified",
        ),
    ),
    Scenario(
        "D08",
        "larger-than-context hierarchy",
        (
            "whole-document hierarchy reaches head, middle and tail",
            "tail lookup and global summary share complete parser-owned coverage",
        ),
    ),
    Scenario(
        "D09",
        "encrypted archive exact password",
        (
            "missing password persists nothing",
            "leading/trailing Unicode password opens the nested ODT exact-first",
            "the password and normalization variants never persist",
        ),
    ),
    Scenario(
        "D10",
        "technical metadata, visible requisites and exports",
        (
            "container headers and visible number/grif/date/signatory remain distinct",
            "regular make_file export is delivered",
            "a separate owner-only workspace_create reaches the real MCP server create-only",
        ),
    ),
)


_MODEL_ENV_ALLOWLIST = frozenset(
    {
        "FRIDAY_PROFILE",
        "FRIDAY_LLM_BASE_URL",
        "FRIDAY_LLM_MODEL",
        "FRIDAY_LLM_API_KEY",
        "FRIDAY_LLM_TIMEOUT_SEC",
        "FRIDAY_LLM_MAX_TOKENS",
        "FRIDAY_LLM_FOREGROUND_SLOTS",
        "FRIDAY_EMBEDDINGS_ENABLED",
        "FRIDAY_EMBEDDINGS_BASE_URL",
        "FRIDAY_EMBEDDINGS_API_KEY",
        "FRIDAY_EMBEDDINGS_MODEL",
        "FRIDAY_EMBEDDINGS_INDEX_BATCH",
        "FRIDAY_EMBEDDINGS_CHUNK_CHARS",
        "FRIDAY_EMBEDDINGS_CHUNK_OVERLAP_CHARS",
        "FRIDAY_EMBEDDINGS_CHUNK_MAX_PER_OBJECT",
        "FRIDAY_EMBEDDINGS_CHUNK_BLEND",
        "FRIDAY_EMBEDDINGS_CHUNK_SCAN_MULTIPLIER",
        "FRIDAY_EMBEDDINGS_MAX_INPUTS_PER_REQUEST",
        "FRIDAY_RETRIEVAL_DENSE_QUERY_BUDGET_SEC",
        "FRIDAY_RETRIEVAL_DENSE_EVIDENCE_MIN",
        "FRIDAY_RETRIEVAL_POOL_MAX",
        "FRIDAY_RERANK_BASE_URL",
        "FRIDAY_RERANK_MODEL",
        "FRIDAY_RERANK_API_KEY",
        "FRIDAY_RERANK_TIMEOUT_SEC",
        "FRIDAY_RERANK_TOP",
        "FRIDAY_RERANK_CONFIDENT_MIN",
    }
)
_PROCESS_ENV_ALLOWLIST = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "VIRTUAL_ENV",
    }
)
_SCRATCH_PATHS = {
    "FRIDAY_HOME": ".",
    "FRIDAY_DATA_DIR": "data",
    "FRIDAY_CACHE_DIR": "cache",
    "FRIDAY_LOG_DIR": "logs",
    "FRIDAY_MODEL_ROOT": "models",
    "FRIDAY_STATE_DIR": "data/state",
    "FRIDAY_DATABASE_PATH": "data/state/friday.sqlite3",
    "FRIDAY_FILES_DIR": "data/files",
    "FRIDAY_MEMORY_VAULT_DIR": "data/memory-vault",
    "FRIDAY_BACKUPS_DIR": "data/backups",
    "FRIDAY_EXPORTS_DIR": "data/exports",
    "FRIDAY_WHISPER_DOWNLOAD_ROOT": "models/whisper",
    "FRIDAY_TTS_DOWNLOAD_ROOT": "models/tts",
    "FRIDAY_MCP_WORKSPACE_INBOX_DIR": "mcp/inbox",
    "FRIDAY_MCP_WORKSPACE_OUTBOX_DIR": "mcp/outbox",
    "HOME": "process/home",
    "XDG_CONFIG_HOME": "process/xdg/config",
    "XDG_CACHE_HOME": "process/xdg/cache",
    "XDG_DATA_HOME": "process/xdg/data",
    "XDG_STATE_HOME": "process/xdg/state",
    "XDG_RUNTIME_DIR": "process/xdg/runtime",
    "PYTHONPYCACHEPREFIX": "process/pycache",
    "TMPDIR": "process/tmp",
}
_SAFE_OVERRIDES = {
    "FRIDAY_ENV_FILE": "config/no-live-env-file",
    "FRIDAY_DATABASE_MUST_EXIST": "0",
    "FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK": "1",
    "FRIDAY_API_USER_RATE_LIMIT_PER_MINUTE": "1000",
    "FRIDAY_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE": "1000",
    "FRIDAY_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE": "5000",
    "FRIDAY_TELEGRAM_OPEN_REGISTRATION": "0",
    "FRIDAY_SHARED_ARCHIVE": "1",
    "FRIDAY_OPEN_REGISTRATION_GRANTS_FULL_ACCESS": "0",
    "FRIDAY_NEW_ACCOUNT_PRESET": "",
    "FRIDAY_WORKERS_ENABLED": "0",
    "FRIDAY_AUTONOMY_ENABLED": "0",
    "FRIDAY_COGNITION_ENABLED": "0",
    "FRIDAY_REMINDERS_ENABLED": "0",
    "FRIDAY_MONITORS_ENABLED": "0",
    "FRIDAY_REFLECTION_ENABLED": "0",
    "FRIDAY_CHRONICLE_ENABLED": "0",
    "FRIDAY_SENTINEL_ENABLED": "0",
    "FRIDAY_CODE_EXECUTION_ENABLED": "0",
    "FRIDAY_WEB_DAILY_QUOTA": "0",
    "FRIDAY_WHISPER_ENABLED": "0",
    "FRIDAY_TTS_ENABLED": "0",
    "FRIDAY_MCP_ENABLED": "1",
    "FRIDAY_MCP_STARTUP_TIMEOUT_SEC": "15",
    "FRIDAY_MCP_CALL_TIMEOUT_SEC": "20",
    "FRIDAY_MCP_RESULT_CHARS": "7000",
    "FRIDAY_INGESTION_REVIEW_POLICY": "assessed",
    "FRIDAY_EMBEDDINGS_INDEX_REST_RATIO": "0",
    "FRIDAY_BACKUP_MIRROR_DIR": "",
    "FRIDAY_BACKUP_ENCRYPTION_KEY_FILE": "",
    "PYTHONUNBUFFERED": "1",
}
_LOCAL_SIDECAR_URL_KEYS = (
    "FRIDAY_LLM_BASE_URL",
    "FRIDAY_EMBEDDINGS_BASE_URL",
    "FRIDAY_RERANK_BASE_URL",
)
_LOCAL_SIDECAR_V4_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_LOCAL_SIDECAR_V6_NETWORKS = (ipaddress.ip_network("fc00::/7"),)


class BatteryFailure(RuntimeError):
    """Closed-code battery failure; its message must never contain source text."""


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _new_run_id() -> str:
    return secrets.token_hex(32)


def _validated_run_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _RUN_ID_RE.fullmatch(normalized) is None:
        raise BatteryFailure("battery_run_id_invalid")
    return normalized


def _run_id_hash(run_id: str) -> str:
    return _sha256(bytes.fromhex(_validated_run_id(run_id)))


def _run_token(run_id: str, run_index: int, purpose: str, *, length: int = 16) -> str:
    if not 1 <= run_index <= RUNS or not purpose or not 8 <= length <= 32:
        raise BatteryFailure("run_identity_request_invalid")
    payload = f"{run_index}\0{purpose}".encode()
    return hashlib.sha256(bytes.fromhex(_validated_run_id(run_id)) + b"\0" + payload).hexdigest()[:length]


def _run_owner_chats(run_id: str, run_index: int) -> tuple[int, ...]:
    # Telegram identifiers are signed 64-bit integers.  Reserving two decimal
    # digits for the role makes the eleven identities collision-free per run.
    base = 1_000_000_000 + int(_run_token(run_id, run_index, "telegram-chats", length=10), 16) * 100
    return tuple(base + role for role in range(1, 12))


def _case_identity(run_id: str, run_index: int, case_id: str) -> CaseIdentity:
    if case_id not in {item.case_id for item in SCENARIOS}:
        raise BatteryFailure("unknown_case_identity")
    return CaseIdentity(_validated_run_id(run_id), run_index, case_id)


def _marker(harness: Any, label: str, *, fallback: str = "") -> str:
    identity = getattr(harness, "identity", None)
    if isinstance(identity, CaseIdentity):
        return identity.marker(label)
    return fallback or f"{label}-{int(harness.run_index)}"


def _source_ref(harness: Any, label: str, *, fallback: str = "") -> str:
    identity = getattr(harness, "identity", None)
    if isinstance(identity, CaseIdentity):
        return identity.source_ref(label)
    return fallback or f"telegram-file:{label}-{int(harness.run_index)}"


def _filename(harness: Any, stem: str, extension: str, *, fallback: str = "") -> str:
    identity = getattr(harness, "identity", None)
    if isinstance(identity, CaseIdentity):
        return identity.filename(stem, extension)
    return fallback or f"{stem}.{extension.lstrip('.')}"


def _scoped_prompt(harness: Any, key: str, message: str) -> str:
    """Give non-empty live prompts one of two natural equivalent forms.

    Cache/run identity belongs to the isolated chat, source refs, filenames and
    fixture facts.  It must never become an artificial body-search term in the
    user-visible request itself.
    """

    identity = getattr(harness, "identity", None)
    if not message or not isinstance(identity, CaseIdentity):
        return message
    if identity.prompt_variant(key, 2) == 0:
        return message
    return f"Пожалуйста.\n{message}"


def _load_env_file_values(path: Path) -> dict[str, str]:
    """Read only allowlisted sidecar values without mutating the controller env."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in _MODEL_ENV_ALLOWLIST:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _require_local_sidecar_url(name: str, value: str) -> None:
    """Refuse a battery configuration that could export private fixtures."""

    parsed = urlsplit(value)
    host = str(parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise BatteryFailure(f"{name.casefold()}_not_local")
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise BatteryFailure(f"{name.casefold()}_not_local") from exc
    private_networks = _LOCAL_SIDECAR_V4_NETWORKS if address.version == 4 else _LOCAL_SIDECAR_V6_NETWORKS
    if not (address.is_loopback or any(address in network for network in private_networks)):
        raise BatteryFailure(f"{name.casefold()}_not_local")


def build_worker_environment(
    run_dir: Path,
    *,
    owner_chats: Sequence[int],
    source_env_file: Path | None = None,
    run_id: str | None = None,
) -> dict[str, str]:
    run_dir = run_dir.resolve()
    if run_dir == Path(run_dir.anchor) or not run_dir.is_dir():
        raise BatteryFailure("unsafe_run_directory")
    source_path = source_env_file.resolve() if source_env_file is not None else ROOT / ".env.local"
    source = _load_env_file_values(source_path)
    for key in _MODEL_ENV_ALLOWLIST:
        if key in os.environ:
            source[key] = os.environ[key]
    for key in _LOCAL_SIDECAR_URL_KEYS:
        if value := source.get(key):
            _require_local_sidecar_url(key, value)
    environment = {key: value for key, value in os.environ.items() if key in _PROCESS_ENV_ALLOWLIST}
    environment.update(source)
    environment.update(_SAFE_OVERRIDES)
    for key, relative in _SCRATCH_PATHS.items():
        destination = (run_dir / relative).resolve()
        if not _inside(destination, run_dir):
            raise BatteryFailure("scratch_path_escape")
        environment[key] = str(destination)
    environment["FRIDAY_ENV_FILE"] = str((run_dir / "config/no-live-env-file").resolve())
    environment["FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS"] = ",".join(str(value) for value in owner_chats)
    environment["FRIDAY_TELEGRAM_OWNER_CHAT_IDS"] = ",".join(str(value) for value in owner_chats[:-1])
    environment["FRIDAY_TELEGRAM_BRIDGE_SECRET"] = secrets.token_urlsafe(48)
    environment["FRIDAY_API_TOKEN"] = secrets.token_urlsafe(48)
    environment["FRIDAY_DOCUMENT_BATTERY_RUN_DIR"] = str(run_dir)
    environment["FRIDAY_DOCUMENT_BATTERY_EVIDENCE"] = str(run_dir / "private-evidence.json")
    environment[_RUN_ID_ENV] = _validated_run_id(run_id or _new_run_id())
    for relative in set(_SCRATCH_PATHS.values()) | {"fixtures", "private"}:
        _private_dir((run_dir / relative).resolve())
    return environment


def _scenario_manifest() -> list[dict[str, Any]]:
    return [
        {"case_id": item.case_id, "title": item.title, "contract": list(item.contract)} for item in SCENARIOS
    ]


def case_state_paths(
    run_dir: Path,
    case_id: str,
    identity: CaseIdentity | None = None,
) -> dict[str, Path]:
    """Closed per-case mutable roots; scenarios never share a DB or file tree."""

    if case_id not in {item.case_id for item in SCENARIOS}:
        raise BatteryFailure("unknown_case_state")
    suffix = f"-{identity.token('state-path')}" if identity is not None else ""
    case_root = (run_dir.resolve() / f"case-{case_id.casefold()}{suffix}").resolve()
    if not _inside(case_root, run_dir):
        raise BatteryFailure("case_state_escape")
    return {
        "root": case_root,
        "data": case_root / "data",
        "cache": case_root / "cache",
        "logs": case_root / "logs",
        "models": case_root / "models",
        "state": case_root / "data/state",
        "database": case_root / "data/state/friday.sqlite3",
        "files": case_root / "data/files",
        "memory": case_root / "data/memory-vault",
        "backups": case_root / "data/backups",
        "exports": case_root / "data/exports",
        "mcp_inbox": case_root / "mcp/inbox",
        "mcp_outbox": case_root / "mcp/outbox",
        "evidence": case_root / "private-evidence.json",
    }


def offline_self_test() -> dict[str, Any]:
    ids = [item.case_id for item in SCENARIOS]
    if len(SCENARIOS) != CASES or ids != [f"D{index:02d}" for index in range(1, CASES + 1)]:
        raise BatteryFailure("scenario_manifest_invalid")
    if len(set(ids)) != CASES or any(not item.contract for item in SCENARIOS):
        raise BatteryFailure("scenario_contract_invalid")
    with tempfile.TemporaryDirectory(prefix="friday-document-battery-selftest-") as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        run_ids = (_new_run_id(), _new_run_id())
        if run_ids[0] == run_ids[1]:
            raise BatteryFailure("invocation_identity_not_fresh")
        chats = _run_owner_chats(run_ids[0], 1)
        environment = build_worker_environment(root, owner_chats=chats, run_id=run_ids[0])
        for key, relative in _SCRATCH_PATHS.items():
            expected = (root / relative).resolve()
            if Path(environment[key]).resolve() != expected or not _inside(expected, root):
                raise BatteryFailure("scratch_isolation_invalid")
        if environment["FRIDAY_ENV_FILE"] != str((root / "config/no-live-env-file").resolve()):
            raise BatteryFailure("live_env_not_blocked")
        if environment.get("FRIDAY_WORKERS_ENABLED") != "0":
            raise BatteryFailure("workers_not_blocked")
        private = root / "private" / "mode-check.bin"
        _private_write(private, b"closed")
        if stat.S_IMODE(private.stat().st_mode) != 0o600:
            raise BatteryFailure("private_file_mode_invalid")
        identities = [
            _case_identity(run_id, run_index, scenario.case_id)
            for run_id in run_ids
            for run_index in range(1, RUNS + 1)
            for scenario in SCENARIOS
        ]
        databases = {
            case_state_paths(root, identity.case_id, identity)["database"] for identity in identities
        }
        if len(databases) != len(identities) or any(not _inside(path, root) for path in databases):
            raise BatteryFailure("case_database_isolation_invalid")
        identity_sets: dict[str, set[Any]] = {
            "cache": {identity.cache_prefix for identity in identities},
            "marker": {identity.marker("SELFTEST") for identity in identities},
            "ref": {identity.source_ref("SELFTEST") for identity in identities},
            "filename": {identity.filename("selftest", "odt") for identity in identities},
            "chat_ref": {f"document-live:{identity.token('chat-ref:1')}" for identity in identities},
            "message": {int(identity.token("message:1", length=15), 16) for identity in identities},
        }
        if any(len(values) != len(identities) for values in identity_sets.values()):
            raise BatteryFailure("fixture_identity_not_disjoint")
        prompt_forms = {
            _scoped_prompt(
                type("PromptProbe", (), {"identity": identity})(),
                "selftest",
                "Обобщи документ.",
            )
            for identity in identities
        }
        if not prompt_forms or not prompt_forms.issubset(
            {"Обобщи документ.", "Пожалуйста.\nОбобщи документ."}
        ):
            raise BatteryFailure("prompt_variant_not_natural")
        # A model cache sees the whole conversation, including the isolated
        # document fact/name, not only the final natural instruction.  Assert
        # that this real identity surface remains disjoint without teaching the
        # product to ignore a synthetic token in user text.
        conversation_prompts = {
            "\n".join(
                (
                    identity.filename("selftest", "odt"),
                    identity.marker("SELFTEST"),
                    _scoped_prompt(
                        type("PromptProbe", (), {"identity": identity})(),
                        "selftest",
                        "Обобщи документ.",
                    ),
                )
            )
            for identity in identities
        }
        if len(conversation_prompts) != len(identities):
            raise BatteryFailure("conversation_prompt_identity_not_disjoint")
    return {
        "schema": SCHEMA,
        "self_test": "passed",
        "runs": RUNS,
        "cases_per_run": CASES,
        "scenario_ids": ids,
        "identity_count": len(identities),
        "identity_disjoint": True,
        "prompt_variants": 2,
    }


def _odt_bytes(
    paragraphs: Sequence[str],
    *,
    title: str = "",
    creator: str = "Synthetic Friday Battery",
    creation_date: str = "2025-01-02T03:04:05+00:00",
    modified_date: str = "2025-01-03T04:05:06+00:00",
) -> bytes:
    body = "".join(f"<text:p>{html.escape(value)}</text:p>" for value in paragraphs)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text>{body}</office:text></office:body>
</office:document-content>"""
    meta = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0">
 <office:meta>
  <dc:title>{html.escape(title)}</dc:title>
  <dc:creator>{html.escape(creator)}</dc:creator>
  <meta:creation-date>{creation_date}</meta:creation-date>
  <dc:date>{modified_date}</dc:date>
  <meta:editing-cycles>7</meta:editing-cycles>
  <meta:document-statistic meta:page-count="3" meta:paragraph-count="8" meta:word-count="44"/>
 </office:meta>
</office:document-meta>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("content.xml", content)
        archive.writestr("meta.xml", meta)
    return output.getvalue()


def _xlsx_bytes(rows: Sequence[Sequence[str]]) -> bytes:
    from openpyxl import Workbook  # type: ignore[import-untyped]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Штат"
    for row in rows:
        sheet.append(list(row))
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


_SCAN_FIXTURE_WIDTH = 1_600
_SCAN_FIXTURE_HEIGHT = 2_200
_SCAN_FIXTURE_TEXT_X = 110
_SCAN_FIXTURE_RIGHT_MARGIN = 110
_SCAN_FIXTURE_FONT_SIZE = 76
_SCAN_SECRET_LABEL_Y = 800
_SCAN_SECRET_VALUE_Y = 940


def _scan_fixture_font(text: str, font_path: Path, *, max_width: int) -> Any:
    """Return the largest fixture font whose complete text fits the page."""

    from PIL import ImageFont

    if font_path.is_file():
        for size in range(_SCAN_FIXTURE_FONT_SIZE, 11, -1):
            font = ImageFont.truetype(str(font_path), size)
            left, _top, right, _bottom = font.getbbox(text)
            if right - left <= max_width:
                return font
    font = ImageFont.load_default()
    left, _top, right, _bottom = font.getbbox(text)
    if right - left <= max_width:
        return font
    raise BatteryFailure("scan_fixture_text_does_not_fit")


def _scan_pdf(marker: str, *, pages: int = 5, fixture_scope: str = "") -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]
    from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font: Any = (
        ImageFont.truetype(str(font_path), _SCAN_FIXTURE_FONT_SIZE)
        if font_path.is_file()
        else ImageFont.load_default()
    )
    secret_width = _SCAN_FIXTURE_WIDTH - _SCAN_FIXTURE_TEXT_X - _SCAN_FIXTURE_RIGHT_MARGIN
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(800, 1100), pageCompression=1)
    for page in range(1, pages + 1):
        image = Image.new("RGB", (_SCAN_FIXTURE_WIDTH, _SCAN_FIXTURE_HEIGHT), "white")
        draw = ImageDraw.Draw(image)
        draw.text((110, 180), f"SYNTHETIC SCAN PAGE {page}", fill="black", font=font)
        draw.text((110, 480), f"CONTROL PAGE NUMBER {page}", fill="black", font=font)
        if fixture_scope:
            draw.text((110, 650), f"FIXTURE SCOPE {fixture_scope} PAGE {page}", fill="black", font=font)
        if page == pages:
            draw.text(
                (_SCAN_FIXTURE_TEXT_X, _SCAN_SECRET_LABEL_Y),
                "SECRET CODE",
                fill="black",
                font=font,
            )
            secret_font = _scan_fixture_font(marker, font_path, max_width=secret_width)
            draw.text(
                (_SCAN_FIXTURE_TEXT_X, _SCAN_SECRET_VALUE_Y),
                marker,
                fill="black",
                font=secret_font,
            )
        encoded = io.BytesIO()
        image.save(encoded, format="PNG", optimize=True)
        encoded.seek(0)
        pdf.drawImage(ImageReader(encoded), 0, 0, width=800, height=1100)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def _encrypted_zip(inner_name: str, inner_bytes: bytes, password: str) -> bytes:
    import pyzipper  # type: ignore[import-untyped]

    output = io.BytesIO()
    with pyzipper.AESZipFile(
        output,
        mode="w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(password.encode("utf-8"))
        archive.writestr(inner_name, inner_bytes)
    return output.getvalue()


def _json_metadata(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    value = row.get("metadata_json")
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalized(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _contains_all(value: str, expected: Sequence[str]) -> bool:
    normalized = _normalized(value)
    return all(_normalized(item) in normalized for item in expected)


def _docx_non_title_lines(payload: bytes) -> tuple[str, ...]:
    """Return normalized non-empty DOCX paragraphs, excluding its title."""

    if not payload:
        return ()
    try:
        from docx import Document

        document = Document(io.BytesIO(payload))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return ()
    lines: list[str] = []
    for paragraph in document.paragraphs:
        line = _normalized(paragraph.text)
        if not line:
            continue
        style = getattr(paragraph, "style", None)
        style_name = _normalized(str(getattr(style, "name", "") or ""))
        if style_name == "title":
            continue
        lines.append(line)
    return tuple(lines)


class LiveProbes:
    """Content-free counters plus the last closed source-search projection."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.counts = {
            "llm_chat_attempts": 0,
            "embedding_calls": 0,
            "embedding_successes": 0,
            "reranker_calls": 0,
            "reranker_successes": 0,
            "embedding_http": 0,
            "reranker_http": 0,
            "source_search_calls": 0,
            "source_search_successes": 0,
            "hierarchy_calls": 0,
            "hierarchy_complete": 0,
            "late_make_file_attempts": 0,
            "workspace_create_kernel_attempts": 0,
            "workspace_create_kernel": 0,
            "workspace_create_mcp_attempts": 0,
            "workspace_create_mcp": 0,
            "forbidden_web_calls": 0,
        }
        self.last_source_search: dict[str, Any] = {}
        self._restore: list[Callable[[], None]] = []

    def snapshot(self) -> dict[str, int]:
        return dict(self.counts)

    def delta(self, before: Mapping[str, int]) -> dict[str, int]:
        return {key: int(value) - int(before.get(key, 0)) for key, value in self.counts.items()}

    def install(self) -> None:
        import httpx

        embeddings = self.app.state.embeddings
        original_embed = embeddings.embed

        async def embed(texts: list[str], **kwargs: Any):
            self.counts["embedding_calls"] += 1
            result = await original_embed(texts, **kwargs)
            if (
                isinstance(result, list)
                and len(result) == len(texts)
                and result
                and all(isinstance(row, list) and row for row in result)
            ):
                self.counts["embedding_successes"] += 1
            return result

        embeddings.embed = embed
        self._restore.append(lambda: setattr(embeddings, "embed", original_embed))

        searcher = self.app.state.hybrid_searcher
        original_reranker = searcher._reranker
        if original_reranker is not None:

            async def reranker(query: str, rows: list[dict[str, Any]]):
                self.counts["reranker_calls"] += 1
                before_ids = {str(item.get("id") or "") for item in rows}
                result = await original_reranker(query, rows)
                after_ids = (
                    {str(item.get("id") or "") for item in result if isinstance(item, Mapping)}
                    if isinstance(result, list)
                    else set()
                )
                valid_scores = bool(
                    isinstance(result, list)
                    and before_ids == after_ids
                    and result
                    and all(
                        isinstance(item.get("_rerank_score"), (int, float))
                        for item in result
                        if isinstance(item, Mapping)
                    )
                )
                if valid_scores:
                    self.counts["reranker_successes"] += 1
                return result

            searcher._reranker = reranker
            self._restore.append(lambda: setattr(searcher, "_reranker", original_reranker))

        kernel = self.app.state.kernel
        original_execute = kernel.execute

        async def execute(name: str, arguments: dict[str, Any], **kwargs: Any):
            if name in {"web_search", "web_fetch", "web_research"}:
                # In Friday, a zero daily quota means "quota disabled", not
                # "network disabled".  Keep the requested settings sentinel,
                # but independently make any ordinary web-tool attempt a
                # content-free battery failure after the turn finishes.
                self.counts["forbidden_web_calls"] += 1
                raise BatteryFailure("external_web_tool_attempted")
            if name == "workspace_create":
                self.counts["workspace_create_kernel_attempts"] += 1
            result = await original_execute(name, arguments, **kwargs)
            if name == "source_search":
                self.counts["source_search_calls"] += 1
                if result.success:
                    self.counts["source_search_successes"] += 1
                data = _mapping(result.data)
                raw_rows = data.get("results")
                rows: list[Any] = raw_rows if isinstance(raw_rows, list) else []
                coverage = _mapping(data.get("coverage"))
                first = _mapping(rows[0]) if rows else {}
                self.last_source_search = {
                    "success": bool(result.success),
                    "raw_ids": [
                        str(item.get("raw_object_id") or "") for item in rows if isinstance(item, Mapping)
                    ],
                    "first_excerpt": str(first.get("excerpt") or ""),
                    "first_match_kind": str(first.get("retrieval_match_kind") or ""),
                    "coverage": {
                        key: coverage.get(key)
                        for key in (
                            "complete",
                            "semantic_recall",
                            "semantic_reranked",
                            "uploader_scoped",
                        )
                    },
                }
            elif name == "workspace_create" and result.success:
                self.counts["workspace_create_kernel"] += 1
            return result

        kernel.execute = execute
        self._restore.append(lambda: setattr(kernel, "execute", original_execute))

        agent = self.app.state.agent
        llm: Any = getattr(agent, "llm", None)
        original_llm_chat = getattr(llm, "chat", None)
        if callable(original_llm_chat):

            async def llm_chat(*args: Any, **kwargs: Any):
                self.counts["llm_chat_attempts"] += 1
                return await original_llm_chat(*args, **kwargs)

            llm.chat = llm_chat
            self._restore.append(lambda: setattr(llm, "chat", original_llm_chat))

        original_hierarchy = agent._build_attachment_hierarchy_bundle

        async def hierarchy(*args: Any, **kwargs: Any):
            self.counts["hierarchy_calls"] += 1
            result = await original_hierarchy(*args, **kwargs)
            if isinstance(result, tuple) and len(result) == 2 and result[1] is True:
                self.counts["hierarchy_complete"] += 1
            return result

        agent._build_attachment_hierarchy_bundle = hierarchy
        self._restore.append(lambda: setattr(agent, "_build_attachment_hierarchy_bundle", original_hierarchy))

        original_late_make_file = agent._file_for_a_request_that_wanted_one

        async def late_make_file(*args: Any, **kwargs: Any):
            self.counts["late_make_file_attempts"] += 1
            return await original_late_make_file(*args, **kwargs)

        agent._file_for_a_request_that_wanted_one = late_make_file
        self._restore.append(
            lambda: setattr(agent, "_file_for_a_request_that_wanted_one", original_late_make_file)
        )

        original_send = httpx.AsyncClient.send
        embedding_base = str(self.app.state.settings.embeddings_base_url).rstrip("/")
        rerank_base = str(self.app.state.settings.rerank_base_url).rstrip("/")

        async def send(client: Any, request: Any, *args: Any, **kwargs: Any):
            url = str(request.url)
            if embedding_base and url.startswith(embedding_base) and request.url.path.endswith("/embeddings"):
                self.counts["embedding_http"] += 1
            if rerank_base and url.startswith(rerank_base) and request.url.path.endswith("/rerank"):
                self.counts["reranker_http"] += 1
            return await original_send(client, request, *args, **kwargs)

        httpx.AsyncClient.send = send
        self._restore.append(lambda: setattr(httpx.AsyncClient, "send", original_send))

        manager = getattr(self.app.state, "mcp", None)
        if manager is not None:
            original_call = manager.call_tool

            async def call_tool(alias: str, name: str, arguments: dict[str, Any]):
                if alias == "workspace" and name == "exchange_create":
                    self.counts["workspace_create_mcp_attempts"] += 1
                result = await original_call(alias, name, arguments)
                if alias == "workspace" and name == "exchange_create":
                    self.counts["workspace_create_mcp"] += 1
                return result

            manager.call_tool = call_tool
            self._restore.append(lambda: setattr(manager, "call_tool", original_call))

    def close(self) -> None:
        for restore in reversed(self._restore):
            restore()
        self._restore.clear()


class Harness:
    def __init__(
        self,
        app: Any,
        client: Any,
        settings: Any,
        run_dir: Path,
        run_index: int,
        identity: CaseIdentity | None = None,
    ) -> None:
        self.app = app
        self.client = client
        self.settings = settings
        self.storage = app.state.storage
        self.run_dir = run_dir
        self.run_index = run_index
        self.identity = identity
        chats = (
            _run_owner_chats(identity.run_id, run_index)
            if identity is not None
            else tuple(9911000 + index for index in range(1, 12))
        )
        self.owner_chats = {item.case_id: chats[index] for index, item in enumerate(SCENARIOS)}
        self.jbl_chat = chats[-1]
        self.sequence = 0
        self.raw_evidence: list[dict[str, Any]] = []
        self.probes = LiveProbes(app)
        self.probes.install()
        owner_case = identity.case_id if identity is not None else "D01"
        self.owner_id = self._me(self.owner_chats[owner_case])["actor"]["user_id"]
        self.jbl_id = self._me(self.jbl_chat)["actor"]["user_id"]
        self.storage.update_user(self.jbl_id, display_name="JBL", username="jbl", preset_key="user")

    def close(self) -> None:
        self.probes.close()

    def _headers(self, method: str, path: str, body: bytes, chat: int) -> dict[str, str]:
        from friday.security import sign_bridge_request

        timestamp = int(time.time())
        nonce = secrets.token_hex(16)
        secret = str(self.settings.telegram_bridge_secret)
        return {
            "Content-Type": "application/json",
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": str(chat),
            "X-Friday-Chat": str(chat),
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                secret,
                timestamp=timestamp,
                method=method,
                path=path,
                external_user_id=str(chat),
                chat_id=str(chat),
                nonce=nonce,
                body=body,
            ),
        }

    def _call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        chat: int,
        case_id: str,
    ) -> dict[str, Any]:
        body = _canonical_json(payload) if payload is not None else b""
        response = self.client.request(
            method,
            path,
            content=body or None,
            headers=self._headers(method, path, body, chat),
        )
        try:
            parsed = response.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        self.raw_evidence.append(
            {
                "case_id": case_id,
                "request": payload,
                "status": response.status_code,
                "response": parsed,
            }
        )
        if response.status_code != 200 or not isinstance(parsed, dict):
            raise BatteryFailure(f"{case_id}_http_failure")
        return parsed

    def _me(self, chat: int) -> dict[str, Any]:
        return self._call("GET", "/api/me", None, chat=chat, case_id="BOOT")

    def chat(
        self,
        case_id: str,
        message: str,
        *,
        chat: int | None = None,
        document: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        if self.identity is not None and case_id != self.identity.case_id:
            raise BatteryFailure("case_harness_identity_mismatch")
        self.sequence += 1
        active_chat = chat if chat is not None else self.owner_chats[case_id]
        if self.identity is not None:
            top_source_ref = f"document-live:{self.identity.token(f'chat-ref:{self.sequence}')}"
            telegram_message_id = int(self.identity.token(f"message:{self.sequence}", length=15), 16)
            username = f"synthetic_{case_id.casefold()}_{self.identity.token('telegram-user', length=8)}"
        else:
            top_source_ref = f"document-live:{self.run_index}:{case_id}:{self.sequence}"
            telegram_message_id = self.run_index * 100_000 + self.sequence
            username = f"synthetic_{case_id.casefold()}"
        if active_chat == self.jbl_chat:
            telegram_user = {
                "id": active_chat,
                "first_name": "JBL",
                "last_name": "",
                "username": "jbl",
                "language_code": "ru",
            }
        else:
            telegram_user = {
                "id": active_chat,
                "first_name": "Synthetic",
                "last_name": case_id,
                "username": username,
                "language_code": "ru",
            }
        payload: dict[str, Any] = {
            "message": _scoped_prompt(self, f"{case_id}:{self.sequence}", message),
            "source_ref": top_source_ref,
            "telegram_message_id": telegram_message_id,
            "telegram_user": telegram_user,
            "enable_tools": True,
            **fields,
        }
        if document is not None:
            payload["document"] = document
        return self._call("POST", "/api/chat", payload, chat=active_chat, case_id=case_id)

    @staticmethod
    def document(filename: str, mime_type: str, payload: bytes, source_ref: str) -> dict[str, Any]:
        return {
            "filename": filename,
            "mime_type": mime_type,
            "media_kind": "document",
            "source_ref": source_ref,
            "content_base64": base64.b64encode(payload).decode("ascii"),
        }

    def message_row(self, response: Mapping[str, Any]) -> dict[str, Any]:
        message_id = str(response.get("message_id") or "")
        row = self.storage.get_message(message_id, self.owner_id) if message_id else None
        return dict(row or {})

    def last_user_metadata(self, response: Mapping[str, Any]) -> dict[str, Any]:
        conversation_id = str(response.get("conversation_id") or "")
        rows = self.storage.get_conversation_messages(
            conversation_id,
            user_id=self.owner_id,
            limit=100,
        )
        users = [row for row in rows if row.get("role") == "user"]
        return _json_metadata(users[-1] if users else None)

    def resolve_ref(self, source_ref: str, *, uploader: str | None = None) -> str:
        raw_id = self.storage.resolve_owned_file_source_ref(
            self.owner_id,
            uploader or self.owner_id,
            source_ref,
        )
        return str(raw_id or "")

    def ingest(
        self,
        case_id: str,
        payload: bytes,
        filename: str,
        *,
        uploader: str | None = None,
        source_ref: str = "",
        archive_password: str | None = None,
    ) -> dict[str, Any]:
        owner = uploader or self.owner_id
        channel = "document-live-battery"
        fallback_ref = f"battery-seed:{self.run_index}:{case_id}:{secrets.token_hex(6)}"
        if self.identity is not None:
            channel = self.identity.cache_prefix
            fallback_ref = self.identity.source_ref(f"seed-{self.sequence}-{secrets.token_hex(4)}")
        result = asyncio.run(
            self.app.state.ingestion.ingest_file(
                self.owner_id,
                None,
                payload,
                filename=filename,
                metadata={"uploaded_by": owner, "channel": channel},
                source_ref=source_ref or fallback_ref,
                archive_password=archive_password,
            )
        )
        self.raw_evidence.append({"case_id": case_id, "ingestion": result})
        return result

    def require_promoted(self, case_id: str, result: Mapping[str, Any]) -> tuple[str, str]:
        raw_id = str(result.get("raw_object_id") or "")
        knowledge = result.get("knowledge_object")
        knowledge_id = str(knowledge.get("id") or "") if isinstance(knowledge, Mapping) else ""
        if result.get("promoted") is not True or not raw_id or not knowledge_id:
            raise BatteryFailure(f"{case_id}_seed_not_promoted")
        return raw_id, knowledge_id

    def case_result(
        self,
        case_id: str,
        started: float,
        checks: Mapping[str, bool],
        counters: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        failed = sorted(key for key, value in checks.items() if value is not True)
        return {
            "case_id": case_id,
            "status": "passed" if not failed else "failed",
            "failure_codes": [f"{case_id}_{name}" for name in failed],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "checks": {key: bool(value) for key, value in checks.items()},
            "counters": {key: int(value) for key, value in (counters or {}).items()},
        }


def _case_01(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "ALIAS-ORBIT")
    target = _odt_bytes([f"Контрольный код документа: {marker}."], title="Canonical alias")
    decoy_marker = _marker(h, "DECOY-NEWEST")
    decoy = _odt_bytes([f"Контрольный код: {decoy_marker}."], title="Wrong newest")
    refs = {label: _source_ref(h, label) for label in ("ALIAS-A", "ALIAS-B", "DECOY")}
    for label in ("ALIAS-A", "ALIAS-B"):
        h.ingest(
            "D01",
            target,
            _filename(h, "канонический отчёт", "odt", fallback="канонический отчёт.odt"),
            source_ref=refs[label],
        )
    h.ingest(
        "D01",
        decoy,
        _filename(h, "другой новый отчёт", "odt", fallback="другой новый отчёт.odt"),
        source_ref=refs["DECOY"],
    )
    first = h.resolve_ref(refs["ALIAS-A"])
    second = h.resolve_ref(refs["ALIAS-B"])
    decoy_id = h.resolve_ref(refs["DECOY"])
    answer = h.chat(
        "D01",
        "Какой контрольный код указан именно в этом документе?",
        reply_document_source_ref=refs["ALIAS-B"],
        reply_to="Прими файл.",
    )
    metadata = h.last_user_metadata(answer)
    attached = list(metadata.get("conversation_attachment_raw_ids") or [])
    checks = {
        "dedup_alias_same_raw": bool(first and first == second and first != decoy_id),
        "reply_origin_exact": metadata.get("attachment_origin") == "reply_reference",
        "reply_raw_exact": attached == [first],
        "answer_target": marker.casefold() in str(answer.get("message") or "").casefold(),
        "answer_no_decoy": decoy_marker.casefold() not in str(answer.get("message") or "").casefold(),
    }
    return h.case_result("D01", started, checks)


def _case_02(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    chat = h.owner_chats["D02"]
    marker = _marker(h, "LINEAGE-TARGET")
    decoy_marker = _marker(h, "LINEAGE-DECOY")
    deleted_marker = _marker(h, "LINEAGE-DELETED")
    foreign_marker = _marker(h, "LINEAGE-FOREIGN")
    target_ref = _source_ref(h, "LINEAGE-T")
    decoy_ref = _source_ref(h, "LINEAGE-D")
    deleted_ref = _source_ref(h, "LINEAGE-X")
    foreign_ref = _source_ref(h, "LINEAGE-F")
    target_upload = h.chat(
        "D02",
        "Назови контрольный код из этого документа.",
        chat=chat,
        document=h.document(
            _filename(h, "старый источник", "odt", fallback="старый источник.odt"),
            "application/vnd.oasis.opendocument.text",
            _odt_bytes([f"Контрольный код: {marker}."], title="Older exact source"),
            target_ref,
        ),
    )
    target_id = h.resolve_ref(target_ref)
    assistant_metadata = _json_metadata(h.message_row(target_upload))
    h.chat(
        "D02",
        "Прими новый файл.",
        chat=chat,
        document=h.document(
            _filename(
                h,
                "новейший ложный источник",
                "odt",
                fallback="новейший ложный источник.odt",
            ),
            "application/vnd.oasis.opendocument.text",
            _odt_bytes([f"Контрольный код: {decoy_marker}."], title="Newer decoy"),
            decoy_ref,
        ),
    )
    decoy_id = h.resolve_ref(decoy_ref)
    deleted_seed = h.ingest(
        "D02",
        _odt_bytes([f"Контрольный код: {deleted_marker}."], title="Deleted control"),
        _filename(h, "удалённый контроль", "odt", fallback="удалённый контроль.odt"),
        source_ref=deleted_ref,
    )
    deleted_id = str(deleted_seed.get("raw_object_id") or "")
    h.storage.execute(
        "UPDATE raw_objects SET deleted_at=? WHERE id=? AND user_id=?",
        ("2026-08-11T00:00:00+00:00", deleted_id, h.owner_id),
    )
    h.storage.commit()
    foreign_seed = h.ingest(
        "D02",
        _odt_bytes([f"Контрольный код: {foreign_marker}."], title="Foreign control"),
        _filename(h, "чужой контроль", "odt", fallback="чужой контроль.odt"),
        uploader=h.jbl_id,
        source_ref=foreign_ref,
    )
    foreign_id = str(foreign_seed.get("raw_object_id") or "")
    reply = h.chat(
        "D02",
        "Повтори контрольный код именно из источника процитированного ответа.",
        chat=chat,
        reply_source_message_id=str(target_upload.get("message_id") or ""),
        reply_to=str(target_upload.get("message") or "")[:1000],
    )
    user_metadata = h.last_user_metadata(reply)
    attached = list(user_metadata.get("conversation_attachment_raw_ids") or [])
    source_lineage = list(assistant_metadata.get("conversation_attachment_raw_ids") or [])
    checks = {
        "assistant_owned_target": bool(
            assistant_metadata.get("attachment_context_used") is True and source_lineage == [target_id]
        ),
        "reply_origin_exact": user_metadata.get("attachment_origin") == "reply_assistant",
        "controls_distinct": bool(
            deleted_id and foreign_id and len({target_id, decoy_id, deleted_id, foreign_id}) == 4
        ),
        "deleted_control_closed": bool(h.resolve_ref(deleted_ref) == ""),
        "foreign_control_scoped": bool(
            h.resolve_ref(foreign_ref, uploader=h.jbl_id) == foreign_id and h.resolve_ref(foreign_ref) == ""
        ),
        "reply_raw_exact": bool(
            attached == [target_id] and not {decoy_id, deleted_id, foreign_id}.intersection(attached)
        ),
        "answer_target": marker.casefold() in str(reply.get("message") or "").casefold(),
        "answer_no_decoy": decoy_marker.casefold() not in str(reply.get("message") or "").casefold(),
        "answer_no_deleted": deleted_marker.casefold() not in str(reply.get("message") or "").casefold(),
        "answer_no_foreign": foreign_marker.casefold() not in str(reply.get("message") or "").casefold(),
    }
    return h.case_result("D02", started, checks)


_D03_PROMPT = "В ранее загруженном файле «список камендатур ЛНР» найди отдел в Молодогвардейске и его код."


def _case_03(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "ОТДЕЛ-МОЛОДОГВАРДЕЙСК")
    target = h.ingest(
        "D03",
        _odt_bytes(
            [
                "Список комендатур Луганской Народной Республики.",
                f"Молодогвардейск — отдел координации, код {marker}.",
            ],
            title="Список комендатур ЛНР 2026",
        ),
        _filename(
            h,
            "Список комендатур Луганской Народной Республики 2026",
            "odt",
            fallback="Список комендатур Луганской Народной Республики 2026.odt",
        ),
        source_ref=_source_ref(h, "COMMANDANTS"),
    )
    decoy_scope = _marker(h, "SUV-CONTROL")
    decoy = h.ingest(
        "D03",
        _xlsx_bytes((("СУВ", "Отдел"), ("5_222", "Совсем другой город"), ("Контроль", decoy_scope))),
        _filename(h, "СУВ 5_222", "xlsx", fallback="СУВ 5_222.xlsx"),
        source_ref=_source_ref(h, "SUV-DECOY"),
    )
    answer = h.chat(
        "D03",
        _D03_PROMPT,
    )
    metadata = h.last_user_metadata(answer)
    attached = list(metadata.get("conversation_attachment_raw_ids") or [])
    checks = {
        "target_ingested": bool(target.get("raw_object_id")),
        "decoy_ingested": bool(decoy.get("raw_object_id")),
        "fuzzy_target_selected": attached == [str(target.get("raw_object_id") or "")],
        "decoy_not_selected": str(decoy.get("raw_object_id") or "") not in attached,
        "answer_target": marker.casefold() in str(answer.get("message") or "").casefold(),
    }
    return h.case_result("D03", started, checks)


def _index_barrier(h: Harness, case_id: str, knowledge_ids: Sequence[str]) -> None:
    from friday.retrieval import chunk_scheme

    deadline = time.monotonic() + 240
    attempts = 0
    while attempts < 24 and time.monotonic() < deadline:
        attempts += 1
        missing = h.storage.count_knowledge_missing_embedding(
            h.settings.embeddings_model,
            chunk_scheme=chunk_scheme(h.settings),
            chunk_threshold=h.settings.embeddings_chunk_chars,
        )
        if missing == 0:
            break
        indexed = asyncio.run(h.app.state.workers._embeddings_index_pass())
        if indexed == 0:
            raise BatteryFailure(f"{case_id}_embedding_index_stalled")
    else:
        raise BatteryFailure(f"{case_id}_embedding_index_timeout")
    placeholders = ",".join("?" for _ in knowledge_ids)
    rows = h.storage.execute(
        f"""SELECT knowledge_object_id, COUNT(*) AS count
              FROM knowledge_embeddings
             WHERE knowledge_object_id IN ({placeholders}) AND model=?
             GROUP BY knowledge_object_id""",  # nosec B608 - closed placeholders only
        (*knowledge_ids, h.settings.embeddings_model),
    ).fetchall()
    embedded = {str(row["knowledge_object_id"]) for row in rows if int(row["count"]) > 0}
    if embedded != set(knowledge_ids):
        raise BatteryFailure(f"{case_id}_target_vectors_missing")


def _case_04(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "КАПИТАН-ОРЛОВ")
    target_result = h.ingest(
        "D04",
        _xlsx_bytes(
            (
                ("Подразделение РЭБ", "", ""),
                ("Командир взвода", f"капитан Орлов {marker}", "узел Северный"),
                ("", "", ""),
                ("Тыловое обеспечение", "кладовщик", "узел Южный"),
            )
        ),
        _filename(h, "штатное расписание", "xlsx", fallback="штатное расписание.xlsx"),
        source_ref=_source_ref(h, "SEM-XLSX"),
    )
    seeds = [h.require_promoted("D04", target_result)]
    for index, text in enumerate(
        (
            "Подразделение связи. Командир аппаратной — капитан Соколов.",
            "Радиоэлектронная защита оборудования выполняется дежурной группой.",
            "Штатное расписание тыловой службы и командиры отделений.",
        ),
        start=1,
    ):
        scoped_text = f"{text} Контроль выборки: {_marker(h, f'SEM-DECOY-{index}')}"
        result = h.ingest(
            "D04",
            _odt_bytes([scoped_text], title=f"Semantic decoy {index}"),
            _filename(
                h,
                f"семантический кандидат {index}",
                "odt",
                fallback=f"семантический кандидат {index}.odt",
            ),
            source_ref=_source_ref(
                h,
                f"SEM-DECOY-{index}",
                fallback=f"telegram-file:SEM-DECOY-{h.run_index}-{index}",
            ),
        )
        seeds.append(h.require_promoted("D04", result))
    _index_barrier(h, "D04", [knowledge_id for _raw_id, knowledge_id in seeds])
    before = h.probes.snapshot()
    prompt = "Посмотри в ранее загруженной штатке: кто командиром взвода РЭБ числится?"
    from friday.agent_runtime import _archived_source_search_focus, _archived_source_search_query

    routed_query = _archived_source_search_query(prompt)
    routed_focus = _archived_source_search_focus(prompt, routed_query)
    if not routed_query or not routed_focus:
        raise BatteryFailure("D04_source_search_route_not_recognized")
    answer = h.chat(
        "D04",
        prompt,
    )
    delta = h.probes.delta(before)
    target_raw = seeds[0][0]
    source = h.probes.last_source_search
    coverage = _mapping(source.get("coverage"))
    checks = {
        "query_embedding_real": delta["embedding_successes"] >= 1 and delta["embedding_http"] >= 1,
        "query_reranker_real": delta["reranker_successes"] >= 1 and delta["reranker_http"] >= 1,
        "source_search_real": delta["source_search_successes"] >= 1,
        "semantic_coverage": coverage.get("semantic_recall") is True,
        "semantic_reranked": coverage.get("semantic_reranked") is True,
        "semantic_not_exhaustive": coverage.get("complete") is False,
        "target_first": bool(source.get("raw_ids") and source["raw_ids"][0] == target_raw),
        "canonical_excerpt": _contains_all(
            str(source.get("first_excerpt") or ""),
            ("Подразделение РЭБ", "Командир взвода", "капитан Орлов"),
        ),
        "answer_target": marker.casefold() in str(answer.get("message") or "").casefold(),
        "no_false_absence": "не найден" not in _normalized(str(answer.get("message") or "")),
    }
    return h.case_result("D04", started, checks, delta)


def _case_05(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    expected: list[str] = []
    dated_ids: list[tuple[int, str]] = []
    expected_markers = tuple(_marker(h, label) for label in ("JBL-FIRST", "JBL-SECOND", "JBL-THIRD"))
    for index, (day, marker) in enumerate(zip((7, 9, 11), expected_markers, strict=True), 1):
        nested_source_ref = _source_ref(
            h,
            f"JBL-{index}",
            fallback=f"telegram-file:JBL-{h.run_index}-{index}",
        )
        h.chat(
            "D05",
            "Прими документ.",
            chat=h.jbl_chat,
            document=h.document(
                _filename(h, f"jbl-{index}", "odt", fallback=f"jbl-{index}.odt"),
                "application/vnd.oasis.opendocument.text",
                _odt_bytes([marker], title=f"JBL {index}"),
                nested_source_ref,
            ),
        )
        raw_id = h.resolve_ref(nested_source_ref, uploader=h.jbl_id)
        if not raw_id or raw_id in expected:
            raise BatteryFailure("D05_fixture_source_ref_resolution_failed")
        expected.append(raw_id)
        dated_ids.append((day, raw_id))
    foreign = h.ingest(
        "D05",
        _odt_bytes([_marker(h, "FOREIGN-DECOY")], title="Foreign owner decoy"),
        _filename(h, "foreign-decoy", "odt", fallback="foreign-decoy.odt"),
        source_ref=_source_ref(h, "FOREIGN"),
    )
    # Seed every upload before opening the direct fixture transaction.  Leaving
    # the first UPDATE pending while the next TestClient/ingestion request starts
    # its own transaction on the shared Storage connection raises SQLite's
    # "cannot start a transaction within a transaction" before product code is
    # exercised at all.
    for day, raw_id in dated_ids:
        h.storage.execute(
            "UPDATE raw_objects SET received_at=? WHERE id=?",
            (f"2026-08-{day:02d}T09:00:00+00:00", raw_id),
        )
    h.storage.execute(
        "UPDATE raw_objects SET received_at=? WHERE id=?",
        ("2026-08-08T09:00:00+00:00", str(foreign.get("raw_object_id") or "")),
    )
    h.storage.commit()
    answer = h.chat(
        "D05",
        "Обобщи данные, которые приходили от пользователя GBL с 7 по 11 августа 2026 года; назови все три маркера.",
    )
    metadata = h.last_user_metadata(answer)
    selected = list(metadata.get("conversation_attachment_raw_ids") or [])
    authorized = h.storage.get_searchable_file_sources(
        h.owner_id,
        selected,
        uploaded_by=h.jbl_id,
        include_content=False,
        limit=max(1, len(selected)),
    )
    answer_text = str(answer.get("message") or "")
    checks = {
        "all_expected_ids": bool(
            len(expected) == len(set(expected)) == 3 and selected == list(reversed(expected))
        ),
        "uploader_reauthorized": bool(
            len(selected) == len(authorized) == 3
            and [str(row.get("id") or "") for row in authorized] == selected
        ),
        "foreign_excluded": str(foreign.get("raw_object_id") or "") not in selected,
        "answer_all_markers": _contains_all(
            answer_text,
            expected_markers,
        ),
        "answer_no_foreign": _marker(h, "FOREIGN-DECOY").casefold() not in answer_text.casefold(),
    }
    return h.case_result("D05", started, checks)


def _case_06(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    markers = (
        _marker(h, "SMALL-ALPHA"),
        _marker(h, "SMALL-BETA"),
        _marker(h, "SMALL-GAMMA"),
    )
    paragraphs = [
        f"Краткий служебный материал. Первый факт {markers[0]}.",
        f"Второй факт {markers[1]}. Третий факт {markers[2]}.",
        "Материал предназначен только для обобщения; никаких внешних действий не требуется.",
    ]
    before = h.probes.snapshot()
    answer = h.chat(
        "D06",
        "",
        document=h.document(
            _filename(h, "малый материал", "odt", fallback="малый материал.odt"),
            "application/vnd.oasis.opendocument.text",
            _odt_bytes(paragraphs, title="Small fit first"),
            _source_ref(h, "SMALL"),
        ),
    )
    delta = h.probes.delta(before)
    text = str(answer.get("message") or "")
    file_ingestion = _mapping(answer.get("file_ingestion"))
    extraction = _mapping(file_ingestion.get("extraction"))
    metadata = _json_metadata(h.message_row(answer))
    partial_words = ("не весь", "частичн", "не удалось разобрать", "не удалось обработать")
    checks = {
        "summary_has_all_facts": _contains_all(text, markers),
        "no_false_partial": not any(word in _normalized(text) for word in partial_words),
        "source_complete": not any(
            extraction.get(key) is True
            for key in (
                "text_truncated",
                "parse_deadline_reached",
                "parse_pages_truncated",
                "archive_truncated",
                "source_truncated_for_parse",
            )
        ),
        "fit_first_no_hierarchy": delta["hierarchy_calls"] == 0,
        "attachment_owned": metadata.get("attachment_context_used") is True,
        "no_deed_guard": metadata.get("fabricated_outside_deed_request") is not True,
    }
    return h.case_result("D06", started, checks, delta)


def _case_07(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "SCAN-PAGE-FIVE")
    fixture_scope = _marker(h, "SCAN-SCOPE")
    answer = h.chat(
        "D07",
        "Прочитай все страницы скана. Какой SECRET CODE расположен на пятой странице?",
        document=h.document(
            _filename(h, "пятистраничный скан", "pdf", fallback="пятистраничный скан.pdf"),
            "application/pdf",
            _scan_pdf(marker, fixture_scope=fixture_scope),
            _source_ref(h, "SCAN"),
        ),
    )
    file_ingestion = _mapping(answer.get("file_ingestion"))
    extraction = _mapping(file_ingestion.get("extraction"))
    text = str(answer.get("message") or "")
    checks = {
        "ocr_beyond_page_four": int(extraction.get("vision_pages_read") or 0) >= 5,
        "ocr_total_known": int(extraction.get("vision_pages_total") or 0) == 5,
        "ocr_not_truncated": extraction.get("parse_pages_truncated") is not True,
        "answer_target": marker.casefold() in text.casefold(),
        "advisory_not_verified": answer.get("verified") is not True,
        "advisory_visible": bool(answer.get("verification_caution") or answer.get("grounding_warning")),
    }
    return h.case_result("D07", started, checks)


def _case_08(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    markers = (
        _marker(h, "LONG-HEAD"),
        _marker(h, "LONG-MIDDLE"),
        _marker(h, "LONG-TAIL"),
    )
    fixture_scope = _marker(h, "LONG-SCOPE")
    filler = f"Синтетический нейтральный абзац описывает порядок учёта и проверки в выборке {fixture_scope}. "
    parts = [f"Начало документа. Контрольный код {markers[0]}.\n"]
    parts.append((filler * 1500) + f"\nСередина документа. Контрольный код {markers[1]}.\n")
    parts.append((filler * 1500) + f"\nКонец документа. Контрольный код {markers[2]}.\n")
    payload = "".join(parts).encode("utf-8")
    before = h.probes.snapshot()
    answer = h.chat(
        "D08",
        "Обобщи весь документ целиком и отдельно перечисли контрольные коды из начала, середины и хвоста.",
        document=h.document(
            _filename(h, "большой документ", "txt", fallback="большой документ.txt"),
            "text/plain",
            payload,
            _source_ref(h, "LONG"),
        ),
    )
    delta = h.probes.delta(before)
    extraction = _mapping(_mapping(answer.get("file_ingestion")).get("extraction"))
    checks = {
        "fixture_larger_than_model_context": len(payload.decode("utf-8"))
        > int(h.settings.profile.max_model_len) * 4,
        "hierarchy_used": delta["hierarchy_calls"] >= 1,
        "hierarchy_complete": delta["hierarchy_complete"] >= 1,
        "answer_head_middle_tail": _contains_all(str(answer.get("message") or ""), markers),
        "parser_source_complete": not any(
            extraction.get(key) is True
            for key in (
                "text_truncated",
                "parse_deadline_reached",
                "parse_pages_truncated",
                "source_truncated_for_parse",
            )
        ),
    }
    return h.case_result("D08", started, checks, delta)


def _secret_variants(secret: str) -> tuple[str, ...]:
    values = [secret, unicodedata.normalize("NFC", secret), unicodedata.normalize("NFD", secret)]
    return tuple(dict.fromkeys(values))


def _tree_contains_any(root: Path, needles: Sequence[bytes]) -> bool:
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "private-evidence.json":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(needle and needle in data for needle in needles):
            return True
    return False


def _case_09(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    identity = getattr(h, "identity", None)
    password_token = (
        identity.token("archive-password", length=12)
        if isinstance(identity, CaseIdentity)
        else secrets.token_hex(6)
    )
    password = f"  Cafe\u0301-{password_token}-🔐  "
    marker = _marker(h, "ARCHIVE-NESTED")
    inner_name = _filename(h, "nested/document", "odt", fallback="nested/document.odt")
    archive = _encrypted_zip(
        inner_name,
        _odt_bytes([f"Вложенный контрольный код: {marker}."], title="Nested protected"),
        password,
    )
    source_ref = _source_ref(h, "ENCRYPTED")
    archive_name = _filename(h, "защищённый", "zip", fallback="защищённый.zip")
    initial_row = h.storage.execute(
        "SELECT COUNT(*) AS count FROM raw_objects WHERE user_id=? AND content_type='file'",
        (h.owner_id,),
    ).fetchone()
    initial_count = int(initial_row["count"] if initial_row else 0)
    missing = h.chat(
        "D09",
        "Какой код находится во вложенном документе?",
        document=h.document(archive_name, "application/zip", archive, source_ref),
    )
    after_missing_row = h.storage.execute(
        "SELECT COUNT(*) AS count FROM raw_objects WHERE user_id=? AND content_type='file'",
        (h.owner_id,),
    ).fetchone()
    after_missing_count = int(after_missing_row["count"] if after_missing_row else 0)
    missing_raw_id = h.resolve_ref(source_ref)
    success = h.chat(
        "D09",
        "Какой код находится во вложенном документе?",
        document=h.document(archive_name, "application/zip", archive, source_ref),
        archive_password=password,
    )
    after_row = h.storage.execute(
        "SELECT COUNT(*) AS count FROM raw_objects WHERE user_id=? AND content_type='file'",
        (h.owner_id,),
    ).fetchone()
    after_count = int(after_row["count"] if after_row else 0)
    success_ingestion = _mapping(success.get("file_ingestion"))
    persisted_raw_id = h.resolve_ref(source_ref)
    variants = tuple(value.encode("utf-8") for value in _secret_variants(password))
    checks = {
        "challenge_required": bool(
            missing.get("archive_password_required") is True
            and (missing.get("file_ingestion") or {}).get("persisted") is False
        ),
        "missing_not_persisted": bool(after_missing_count == initial_count and not missing_raw_id),
        "success_persisted_once": bool(
            success_ingestion.get("persisted") is True
            and persisted_raw_id
            and after_count == initial_count + 1
        ),
        "answer_nested_marker": marker.casefold() in str(success.get("message") or "").casefold(),
        "secret_not_in_state": not _tree_contains_any(h.run_dir, variants),
    }
    # The private evidence intentionally held the request during the turn.  Erase
    # this synthetic secret before the evidence file is written, without reporting
    # which normalized candidate the extractor accepted.
    for record in h.raw_evidence:
        if record.get("case_id") == "D09" and isinstance(record.get("request"), dict):
            record["request"].pop("archive_password", None)
    return h.case_result("D09", started, checks)


_D10_ATTEMPT_COUNTERS = (
    "llm_chat_attempts",
    "late_make_file_attempts",
    "workspace_create_kernel_attempts",
    "workspace_create_mcp_attempts",
)


def _closed_d10_subturn(
    response: Mapping[str, Any],
    started: float,
    counters: Mapping[str, int],
    *,
    reply_ref_bound: bool | None = None,
) -> dict[str, Any]:
    """Content-free routing evidence for one returned D10 HTTP turn."""

    raw_files = response.get("files")
    raw_tools = response.get("tools_used")
    context = _mapping(response.get("context"))
    closed = {
        "duration_ms": round((time.monotonic() - started) * 1000),
        # Harness._call raises on every non-200/non-object response, so reaching
        # this projection is itself the closed HTTP-success signal.
        "http_returned": True,
        "llm_failed": context.get("llm_failed") is True,
        "files_count": len(raw_files) if isinstance(raw_files, list) else 0,
        "tools_count": len(raw_tools) if isinstance(raw_tools, list) else 0,
        "attempts": {name: int(counters.get(name, 0)) for name in _D10_ATTEMPT_COUNTERS},
    }
    if reply_ref_bound is not None:
        closed["reply_ref_bound_before"] = bool(reply_ref_bound)
    return closed


def _case_10(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "META-EXPORT")
    identity = getattr(h, "identity", None)
    number = (
        f"17-ДСП/{identity.token('document-number', length=8).upper()}"
        if isinstance(identity, CaseIdentity)
        else f"17-ДСП/{h.run_index}"
    )
    body_date = "10 августа 2026 года"
    body = (
        "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ",
        f"ПРИКАЗ № {number}",
        f"Дата документа: {body_date}",
        f"Контрольный маркер: {marker}",
        "Подписант: начальник отдела Иван Иванович Иванов",
    )
    source_ref = _source_ref(h, "METADATA")
    regular_name = _filename(h, "metadata-export", "docx", fallback="metadata-export.docx")
    mcp_name = _filename(h, "mcp-metadata", "txt", fallback="mcp-metadata.txt")
    metadata_before = h.probes.snapshot()
    metadata_started = time.monotonic()
    metadata = h.chat(
        "D10",
        "Покажи все технические метаданные контейнера и все видимые реквизиты этого документа.",
        document=h.document(
            _filename(h, "приказ с реквизитами", "odt", fallback="приказ с реквизитами.odt"),
            "application/vnd.oasis.opendocument.text",
            _odt_bytes(
                body,
                title="Технический заголовок контейнера",
                creator="Редактор Контейнера",
                creation_date="2022-02-03T04:05:06+00:00",
                modified_date="2022-02-04T05:06:07+00:00",
            ),
            source_ref,
        ),
    )
    metadata_diagnostic = _closed_d10_subturn(
        metadata,
        metadata_started,
        h.probes.delta(metadata_before),
    )
    text = str(metadata.get("message") or "")
    regular_reply_ref_bound = bool(h.resolve_ref(source_ref))
    regular_before = h.probes.snapshot()
    regular_started = time.monotonic()
    regular = h.chat(
        "D10",
        f"Создай обычный Word-файл {regular_name} по процитированному документу. "
        "Включи ровно четыре строки: гриф, номер документа, видимую дату документа "
        "и подписанта из предыдущего ответа.",
        reply_document_source_ref=source_ref,
        reply_to=text[:1000],
    )
    regular_diagnostic = _closed_d10_subturn(
        regular,
        regular_started,
        h.probes.delta(regular_before),
        reply_ref_bound=regular_reply_ref_bound,
    )
    raw_files = regular.get("files")
    files: list[Any] = raw_files if isinstance(raw_files, list) else []
    regular_payload = b""
    if files and isinstance(files[0], Mapping):
        try:
            regular_payload = base64.b64decode(str(files[0].get("content_base64") or ""), validate=True)
        except (TypeError, ValueError):
            regular_payload = b""
    regular_text = ""
    regular_extraction_success = False
    if regular_payload and files and isinstance(files[0], Mapping):
        from friday.documents import DocumentExtractor

        extracted = DocumentExtractor(secret_values=()).extract(
            regular_payload,
            str(files[0].get("filename") or regular_name),
            str(files[0].get("mime_type") or ""),
        )
        regular_extraction_success = extracted.success
        regular_text = extracted.text if extracted.success else ""
    regular_lines = _docx_non_title_lines(regular_payload)
    regular_expected_fields = tuple(
        _normalized(value)
        for value in (
            "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ",
            number,
            body_date,
            "Иван Иванович Иванов",
        )
    )
    regular_exact_four_lines = bool(
        len(regular_lines) == len(regular_expected_fields)
        and all(
            expected in regular_lines[index]
            and all(
                other not in regular_lines[index]
                for other_index, other in enumerate(regular_expected_fields)
                if other_index != index
            )
            for index, expected in enumerate(regular_expected_fields)
        )
    )
    mcp_reply_ref_bound = bool(h.resolve_ref(source_ref))
    before = h.probes.snapshot()
    mcp_started = time.monotonic()
    mcp = h.chat(
        "D10",
        f"Используй именно workspace_create и создай в MCP outbox файл {mcp_name}. "
        "Первая строка — только значение номера документа без подписи. Вторая строка — "
        "только значение контрольного маркера без подписи. Никаких других строк.",
        reply_document_source_ref=source_ref,
        reply_to=text[:1000],
    )
    delta = h.probes.delta(before)
    mcp_diagnostic = _closed_d10_subturn(
        mcp,
        mcp_started,
        delta,
        reply_ref_bound=mcp_reply_ref_bound,
    )
    outbox = Path(str(h.settings.mcp_workspace_outbox_dir)) / mcp_name
    outbox_bytes = outbox.read_bytes() if outbox.is_file() else b""
    outbox_lines = tuple(
        _normalized(line)
        for line in outbox_bytes[:8_193].decode("utf-8", "ignore").splitlines()
        if line.strip()
    )
    before_overwrite = _sha256(outbox_bytes) if outbox_bytes else ""
    overwrite_refused = False
    if outbox.is_file():
        from friday.permissions import ActorContext

        owner = ActorContext(
            user_id=h.owner_id,
            person_id=h.owner_id,
            preset_key="owner",
            source="document-live-battery",
        )

        async def repeat_workspace_create() -> Any:
            return await h.app.state.kernel.execute(
                "workspace_create",
                {"filename": mcp_name, "content": "must-not-overwrite"},
                actor=owner,
            )

        repeated = h.client.portal.call(repeat_workspace_create)
        overwrite_refused = bool(
            repeated.success is False
            and outbox.is_file()
            and _sha256(outbox.read_bytes()) == before_overwrite
        )
    checks = {
        "technical_title": "Технический заголовок контейнера".casefold() in text.casefold(),
        "technical_creator": "Редактор Контейнера".casefold() in text.casefold(),
        "technical_all_stored_fields": _contains_all(
            text,
            (
                "Дата создания в свойствах контейнера: 2022-02-03",
                "Дата изменения в свойствах контейнера: 2022-02-04",
                "Циклы редактирования: 7",
                "Страницы: 3",
                "Абзацы: 8",
                "Слова: 44",
            ),
        ),
        "technical_dates_distinct": "2022-02-03" in text and body_date.casefold() in text.casefold(),
        "visible_requisites": _contains_all(text, (number, "служебного пользования", "Иван Иванович Иванов")),
        "regular_file_delivered": bool(
            len(files) == 1
            and isinstance(files[0], Mapping)
            and str(files[0].get("filename") or "") == regular_name
            and regular_payload
            and regular_extraction_success
        ),
        "regular_file_grounded": _contains_all(
            regular_text,
            (number, body_date, "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ", "Иван Иванович Иванов"),
        ),
        "regular_file_exact_four_lines": regular_exact_four_lines,
        "mcp_kernel_real": delta["workspace_create_kernel"] == 1,
        "mcp_transport_real": delta["workspace_create_mcp"] == 1,
        "mcp_exact_content": bool(
            len(outbox_bytes) <= 8_192 and outbox_lines == (_normalized(number), _normalized(marker))
        ),
        "mcp_private_mode": bool(outbox.is_file() and stat.S_IMODE(outbox.stat().st_mode) == 0o600),
        "mcp_create_only": overwrite_refused,
        "mcp_reported_tool": "workspace_create" in list(mcp.get("tools_used") or []),
        "mcp_no_duplicate_chat_file": not list(mcp.get("files") or []),
    }
    result = h.case_result("D10", started, checks, delta)
    result["diagnostics"] = {
        "subturns": {
            "metadata": metadata_diagnostic,
            "regular": regular_diagnostic,
            "mcp": mcp_diagnostic,
        }
    }
    return result


_CASE_RUNNERS: tuple[Callable[[Harness], dict[str, Any]], ...] = (
    _case_01,
    _case_02,
    _case_03,
    _case_04,
    _case_05,
    _case_06,
    _case_07,
    _case_08,
    _case_09,
    _case_10,
)


def _assert_worker_settings(settings: Any, run_dir: Path, *, require_mcp: bool) -> None:
    for path in (
        settings.home,
        settings.data_dir,
        settings.cache_dir,
        settings.log_dir,
        settings.state_dir,
        settings.database_path,
        settings.files_dir,
        settings.memory_vault_dir,
        settings.backups_dir,
        settings.exports_dir,
        settings.mcp_workspace_inbox_dir,
        settings.mcp_workspace_outbox_dir,
    ):
        if path is None or not _inside(Path(path), run_dir):
            raise BatteryFailure("worker_path_not_isolated")
    if settings.workers_enabled or settings.code_execution_enabled:
        raise BatteryFailure("unsafe_worker_feature_enabled")
    if settings.web_daily_quota != 0:
        raise BatteryFailure("web_access_not_disabled")
    if not settings.llm_enabled:
        raise BatteryFailure("llm_not_enabled")
    if not settings.embeddings_enabled or not settings.embeddings_model:
        raise BatteryFailure("embeddings_not_enabled")
    if settings.rerank_top <= 0 or not settings.rerank_base_url or not settings.rerank_model:
        raise BatteryFailure("reranker_not_enabled")
    if require_mcp and not settings.mcp_enabled:
        raise BatteryFailure("mcp_not_enabled")


def _settings_for_case(
    base: Any,
    run_dir: Path,
    case_id: str,
    identity: CaseIdentity | None = None,
) -> tuple[Any, Path, Path]:
    paths = case_state_paths(run_dir, case_id, identity)
    for key, path in paths.items():
        if key not in {"database", "evidence"}:
            _private_dir(path)
    mcp_enabled = case_id == "D10"
    settings = replace(
        base,
        home=paths["root"],
        data_dir=paths["data"],
        cache_dir=paths["cache"],
        log_dir=paths["logs"],
        model_root=paths["models"],
        model_dir=paths["models"] / base.profile.model_dir_name,
        state_dir=paths["state"],
        database_path=paths["database"],
        database_must_exist=False,
        files_dir=paths["files"],
        memory_vault_dir=paths["memory"],
        backups_dir=paths["backups"],
        exports_dir=paths["exports"],
        backup_mirror_dir=None,
        backup_encryption_key_file=None,
        whisper_download_root=str(paths["models"] / "whisper"),
        tts_download_root=str(paths["models"] / "tts"),
        mcp_enabled=mcp_enabled,
        mcp_workspace_inbox_dir=paths["mcp_inbox"],
        mcp_workspace_outbox_dir=paths["mcp_outbox"],
    )
    return settings, paths["root"], paths["evidence"]


def execute_worker(run_index: int) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    run_dir = Path(os.environ["FRIDAY_DOCUMENT_BATTERY_RUN_DIR"]).resolve()
    evidence_path = Path(os.environ["FRIDAY_DOCUMENT_BATTERY_EVIDENCE"]).resolve()
    run_id = _validated_run_id(os.environ.get(_RUN_ID_ENV, ""))
    run_hash = _run_id_hash(run_id)
    if not 1 <= run_index <= RUNS or not _inside(evidence_path, run_dir):
        raise BatteryFailure("worker_request_invalid")
    from friday.config import ensure_runtime_dirs, load_settings, validate_settings

    base_settings = load_settings()
    _assert_worker_settings(base_settings, run_dir, require_mcp=True)
    problems = [item for item in validate_settings(base_settings) if not item.startswith("warning:")]
    if problems:
        raise BatteryFailure("isolated_settings_invalid")
    from friday.server import create_app

    results: list[dict[str, Any]] = []
    started = time.monotonic()
    for scenario, runner in zip(SCENARIOS, _CASE_RUNNERS, strict=True):
        identity = _case_identity(run_id, run_index, scenario.case_id)
        state = case_state_paths(run_dir, scenario.case_id, identity)
        case_dir = _private_dir(state["root"])
        case_evidence_path = state["evidence"]
        raw_evidence: list[dict[str, Any]] = []
        result: dict[str, Any]
        try:
            settings, case_dir, case_evidence_path = _settings_for_case(
                base_settings,
                run_dir,
                scenario.case_id,
                identity,
            )
            _assert_worker_settings(
                settings,
                case_dir,
                require_mcp=scenario.case_id == "D10",
            )
            case_problems = [item for item in validate_settings(settings) if not item.startswith("warning:")]
            if case_problems:
                raise BatteryFailure("isolated_case_settings_invalid")
            ensure_runtime_dirs(settings)
            app = create_app(settings)
            with TestClient(app) as client:
                manager = getattr(app.state, "mcp", None)
                if scenario.case_id == "D10" and (manager is None or not manager.is_available("workspace")):
                    raise BatteryFailure("mcp_workspace_unavailable")
                harness = Harness(app, client, settings, case_dir, run_index, identity)
                try:
                    result = runner(harness)
                    if harness.probes.counts["forbidden_web_calls"]:
                        raise BatteryFailure("external_web_tool_attempted")
                    raw_evidence = harness.raw_evidence
                finally:
                    harness.close()
        except BatteryFailure as exc:
            result = {
                "case_id": scenario.case_id,
                "status": "failed",
                "failure_codes": [str(exc)],
                "duration_ms": 0,
                "checks": {},
                "counters": {},
            }
        except Exception as exc:  # noqa: BLE001 - private trace stays in worker log
            result = {
                "case_id": scenario.case_id,
                "status": "failed",
                "failure_codes": [f"{scenario.case_id}_exception_{type(exc).__name__}"],
                "duration_ms": 0,
                "checks": {},
                "counters": {},
            }
        finally:
            _private_write(
                case_evidence_path,
                _canonical_json(
                    {
                        "schema": WORKER_SCHEMA,
                        "run_index": run_index,
                        "run_id_hash": run_hash,
                        "case_id": scenario.case_id,
                        "fresh_database": True,
                        "raw_private_evidence": raw_evidence,
                        "closed_result": result,
                    }
                ),
            )
        result["fresh_database"] = True
        results.append(result)
    _private_write(
        evidence_path,
        _canonical_json(
            {
                "schema": WORKER_SCHEMA,
                "run_index": run_index,
                "run_id_hash": run_hash,
                "fresh_database_per_case": True,
                "closed_results": results,
            }
        ),
    )
    return {
        "schema": WORKER_SCHEMA,
        "run_index": run_index,
        "run_id_hash": run_hash,
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "duration_ms": round((time.monotonic() - started) * 1000),
        "cases": results,
    }


def _worker_main(args: argparse.Namespace) -> int:
    try:
        result = execute_worker(int(args.run_index))
    except Exception as exc:  # noqa: BLE001 - emit a closed code only
        result = {
            "schema": WORKER_SCHEMA,
            "run_index": int(args.run_index),
            "status": "failed",
            "failure_codes": [f"worker_exception_{type(exc).__name__}"],
            "cases": [],
        }
    sys.stdout.buffer.write(_canonical_json(result) + b"\n")
    return 0 if result.get("status") == "passed" else 1


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return completed.stdout.strip()


def _validate_live_gate(freeze_commit: str, bridge_stopped: bool) -> str:
    if not bridge_stopped:
        raise BatteryFailure("bridge_stop_assertion_required")
    if re.fullmatch(r"[0-9a-fA-F]{40}", freeze_commit or "") is None:
        raise BatteryFailure("freeze_commit_required")
    head = _git_output("rev-parse", "HEAD")
    resolved = _git_output("rev-parse", f"{freeze_commit}^{{commit}}")
    if resolved != head:
        raise BatteryFailure("freeze_commit_is_not_head")
    if _git_output("status", "--porcelain=v1", "--untracked-files=all"):
        raise BatteryFailure("release_worktree_is_dirty")
    return head


def _controller_source_env_file(value: str) -> Path | None:
    configured = str(value or os.environ.get("FRIDAY_ENV_FILE") or "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser().resolve()
    if not path.is_file():
        raise BatteryFailure("source_env_file_missing")
    return path


def run_controller(args: argparse.Namespace) -> dict[str, Any]:
    commit = _validate_live_gate(str(args.freeze_commit or ""), bool(args.bridge_stopped))
    source_env_file = _controller_source_env_file(str(args.source_env_file or ""))
    run_id = _new_run_id()
    run_hash = _run_id_hash(run_id)
    private_root = Path(tempfile.mkdtemp(prefix="friday-document-live-battery-")).resolve()
    private_root.chmod(0o700)
    reports: list[dict[str, Any]] = []
    try:
        for run_index in range(1, RUNS + 1):
            run_token = _run_token(run_id, run_index, "state-path")
            run_dir = _private_dir(private_root / f"run-{run_index}-{run_token}")
            owner_chats = _run_owner_chats(run_id, run_index)
            environment = build_worker_environment(
                run_dir,
                owner_chats=owner_chats,
                source_env_file=source_env_file,
                run_id=run_id,
            )
            log_path = run_dir / "private-worker.log"
            log_descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(log_descriptor, "wb") as log:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--run-index",
                        str(run_index),
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    timeout=WORKER_TIMEOUT_SEC,
                    check=False,
                )
            try:
                report = json.loads(completed.stdout.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                report = {
                    "schema": WORKER_SCHEMA,
                    "run_index": run_index,
                    "status": "failed",
                    "failure_codes": ["worker_output_invalid"],
                    "cases": [],
                }
            if (
                report.get("schema") != WORKER_SCHEMA
                or report.get("run_index") != run_index
                or report.get("run_id_hash") != run_hash
            ):
                report = {
                    "schema": WORKER_SCHEMA,
                    "run_index": run_index,
                    "run_id_hash": run_hash,
                    "status": "failed",
                    "failure_codes": ["worker_identity_mismatch"],
                    "cases": [],
                }
            reports.append(report)
            if report.get("status") != "passed":
                # A failed first streak must be fixed on a new frozen commit;
                # spending another full live run cannot turn it into 2/2.
                break
        aggregate = {
            "schema": REPORT_SCHEMA,
            "commit": commit,
            "run_id_hash": run_hash,
            "runs_expected": RUNS,
            "runs_completed": len(reports),
            "cases_expected_per_run": CASES,
            "status": (
                "passed"
                if len(reports) == RUNS and all(item.get("status") == "passed" for item in reports)
                else "failed"
            ),
            "runs": reports,
        }
        if args.keep_private_run_dir:
            aggregate["private_run_dir"] = str(private_root)
        if args.report:
            report_path = Path(args.report).expanduser().resolve()
            _private_write(report_path, _canonical_json(aggregate) + b"\n")
        return aggregate
    finally:
        if not args.keep_private_run_dir:
            shutil.rmtree(private_root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="offline manifest/isolation proof only")
    parser.add_argument("--run-live", action="store_true", help="explicitly authorize the live battery")
    parser.add_argument("--freeze-commit", default="", help="exact 40-hex commit frozen for the run")
    parser.add_argument(
        "--source-env-file",
        default="",
        help=(
            "operator env file used only to copy allowlisted local sidecar settings; "
            "defaults to controller FRIDAY_ENV_FILE"
        ),
    )
    parser.add_argument(
        "--bridge-stopped",
        action="store_true",
        help="operator assertion that the production Telegram bridge is stopped",
    )
    parser.add_argument("--report", default="", help="optional closed aggregate JSON path")
    parser.add_argument(
        "--keep-private-run-dir",
        action="store_true",
        help="retain private raw evidence directory (0600); off by default",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-index", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.worker:
        return _worker_main(args)
    if args.self_test:
        print(json.dumps(offline_self_test(), ensure_ascii=False, sort_keys=True))
        return 0
    if not args.run_live:
        raise SystemExit("Refusing live execution: use --run-live after code freeze and bridge stop")
    report = run_controller(args)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
