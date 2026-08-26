"""Deterministic, non-executing bundles for Engineer work products.

The compiler/patcher owns producing and structurally checking bytes.  This
module only freezes exact sources and outputs into a content-addressed ZIP.  It
never imports, executes, links or loads an artifact.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import stat
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from friday.file_delivery import (
    AuthorizedFileBytes,
    AuthorizedFileReadError,
    FileRecordUnavailable,
    read_authorized_file_in_transaction,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import (
    AuthorizedFileSnapshotToken,
    authorized_file_snapshot_token_is_process_owned,
)

BUNDLE_MANIFEST_SCHEMA = "friday.engineer.artifact-bundle-manifest.v1"
BUNDLE_RECEIPT_SCHEMA = "friday.engineer.artifact-bundle-receipt.v1"
BUNDLE_MIME_TYPE = "application/zip"
MAX_BUNDLE_SOURCES = 16
MAX_BUNDLE_ARTIFACTS = 16
MAX_BUNDLE_ITEM_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_METADATA_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 48 * 1024 * 1024

_AUTHORITY = object()
_RAW_ID = re.compile(r"raw_[0-9a-f]{16}")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}")
_TOOL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+:/-]{0,79}")
_OPERATIONS = frozenset({"compile", "package", "patch", "rebuild", "report"})
_ROLES = frozenset({"binary", "library", "package", "report", "source"})
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


class EngineerArtifactBundleError(ValueError):
    """A closed bundle failure which never embeds a path, filename or bytes."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "bundle_invalid")
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class BundleSourceLineage:
    """Process-owned pin for one initially authorized immutable Raw source."""

    raw_id: str
    filename: str
    mime_type: str
    content_sha256: str
    size_bytes: int
    snapshot_token: AuthorizedFileSnapshotToken = field(repr=False)
    _authority: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class BundleSource:
    """Exact bytes admitted to a bundle by a reviewed source factory."""

    filename: str
    mime_type: str
    content: bytes = field(repr=False)
    content_sha256: str
    origin_kind: Literal["owned_raw", "generated"]
    parent_sha256s: tuple[str, ...] = ()
    instruction_sha256: str = ""
    origin_user_message_sha256: str = ""
    producer: str = ""
    _authority: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ProducedArtifact:
    """Exact, structurally checked output bytes; no runtime-execution claim."""

    filename: str
    mime_type: str
    content: bytes = field(repr=False)
    content_sha256: str
    role: Literal["binary", "library", "package", "report", "source"]
    parent_sha256s: tuple[str, ...]
    tool_name: str
    tool_version: str
    verification_checks: tuple[str, ...]
    _authority: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class EngineerArtifactBundle:
    filename: str
    mime_type: str
    payload: bytes = field(repr=False)
    sha256: str
    manifest: Mapping[str, Any]
    receipt: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EngineerArtifactDelivery:
    """Ordered Telegram carriers: directly usable outputs, then full bundle."""

    artifacts: tuple[ProducedArtifact, ...]
    bundle: EngineerArtifactBundle
    attachments: tuple[dict[str, str], ...]


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise EngineerArtifactBundleError("bundle_metadata_invalid") from exc
    if len(encoded) > MAX_BUNDLE_METADATA_BYTES:
        raise EngineerArtifactBundleError("bundle_metadata_limit")
    return encoded


def _safe_filename(value: object, *, fallback: str = "artifact") -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(char if char >= " " and char != "\x7f" else "_" for char in text)
    text = re.sub(r'[<>:"/\\|?*]', "_", text)
    text = " ".join(text.split()).strip(" .")
    if not text or text in {".", ".."}:
        text = fallback
    stem = text.split(".", 1)[0].casefold()
    if stem in _WINDOWS_RESERVED_NAMES:
        text = "_" + text
    return text[:120].rstrip(" .") or fallback


def _safe_bundle_filename(value: object) -> str:
    stem = _safe_filename(value, fallback="engineer-artifacts")
    suffix = ".engineer-bundle.zip"
    if stem.casefold().endswith(suffix):
        return stem
    if stem.casefold().endswith(".zip"):
        stem = stem[:-4].rstrip(" .") or "engineer-artifacts"
    return (stem[: 120 - len(suffix)] or "engineer-artifacts") + suffix


def _mime(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _MIME_TYPE.fullmatch(candidate) else "application/octet-stream"


def _sha256s(values: Sequence[str], *, allow_empty: bool) -> tuple[str, ...]:
    normalized = tuple(sorted({str(item or "").strip().casefold() for item in values}))
    if (not normalized and not allow_empty) or any(_SHA256.fullmatch(item) is None for item in normalized):
        raise EngineerArtifactBundleError("source_lineage_invalid")
    return normalized


def bundle_source_lineage(stored: AuthorizedFileBytes) -> BundleSourceLineage:
    """Freeze the initial verified Raw identity before a long build starts."""

    if type(stored) is not AuthorizedFileBytes:
        raise EngineerArtifactBundleError("source_lineage_invalid")
    token = stored.snapshot_token
    content = bytes(stored.content)
    digest = _digest(content)
    if (
        not _RAW_ID.fullmatch(stored.raw_id)
        or token is None
        or not authorized_file_snapshot_token_is_process_owned(token)
        or token.source.raw_id != stored.raw_id
        or not _SHA256.fullmatch(token.content_sha256)
        or digest != token.content_sha256
        or len(content) > MAX_BUNDLE_ITEM_BYTES
    ):
        raise EngineerArtifactBundleError("source_lineage_invalid")
    return BundleSourceLineage(
        raw_id=stored.raw_id,
        filename=_safe_filename(stored.filename, fallback="source.bin"),
        mime_type=_mime(stored.mime_type),
        content_sha256=digest,
        size_bytes=len(content),
        snapshot_token=token,
        _authority=_AUTHORITY,
    )


def generated_bundle_source(
    *,
    filename: str,
    content: bytes,
    mime_type: str,
    origin_user_message_id: str,
    instruction_sha256: str,
    producer: str,
    parent_sha256s: Sequence[str] = (),
) -> BundleSource:
    """Admit same-turn authored/modified source with exact instruction lineage.

    The caller must already own the source-generation action.  The opaque user
    message id is retained only as a digest in the delivered receipt.
    """

    payload = bytes(content)
    instruction_digest = str(instruction_sha256 or "").strip().casefold()
    message_id = str(origin_user_message_id or "").strip()
    producer_name = str(producer or "").strip()
    if (
        not payload
        or len(payload) > MAX_BUNDLE_ITEM_BYTES
        or _MESSAGE_ID.fullmatch(message_id) is None
        or _SHA256.fullmatch(instruction_digest) is None
        or _CODE.fullmatch(producer_name) is None
    ):
        raise EngineerArtifactBundleError("generated_source_lineage_invalid")
    return BundleSource(
        filename=_safe_filename(filename, fallback="generated-source.txt"),
        mime_type=_mime(mime_type),
        content=payload,
        content_sha256=_digest(payload),
        origin_kind="generated",
        parent_sha256s=_sha256s(parent_sha256s, allow_empty=True),
        instruction_sha256=instruction_digest,
        origin_user_message_sha256=_digest(message_id.encode("utf-8")),
        producer=producer_name,
        _authority=_AUTHORITY,
    )


def reauthorize_bundle_sources_in_transaction(
    conn: Any,
    *,
    files_root: Path,
    authorization: AuthorizationService,
    actor: ActorContext,
    tenant_id: str,
    lineages: Sequence[BundleSourceLineage],
    max_bytes: int,
) -> tuple[BundleSource, ...]:
    """Recheck files.read, actor state, Raw identity and bytes under one lock."""

    frozen = tuple(lineages)
    if not 1 <= len(frozen) <= MAX_BUNDLE_SOURCES or actor.user_id != str(tenant_id):
        raise EngineerArtifactBundleError("source_authority_denied")
    principal = str(actor.own_id or "").strip()
    principal_row = conn.execute(
        "SELECT preset_key, status FROM users WHERE id=?",
        (principal,),
    ).fetchone()
    if principal_row is None or str(principal_row["status"] or "") != "active":
        raise EngineerArtifactBundleError("source_authority_denied")
    fresh_actor = replace(actor, preset_key=str(principal_row["preset_key"] or "user"))
    if not authorization.authorize(fresh_actor, "files.read").allowed:
        raise EngineerArtifactBundleError("source_authority_denied")

    seen: set[str] = set()
    admitted: list[BundleSource] = []
    total = 0
    for lineage in frozen:
        if (
            type(lineage) is not BundleSourceLineage
            or lineage._authority is not _AUTHORITY
            or lineage.raw_id in seen
            or not authorized_file_snapshot_token_is_process_owned(lineage.snapshot_token)
        ):
            raise EngineerArtifactBundleError("source_lineage_invalid")
        seen.add(lineage.raw_id)
        try:
            stored = read_authorized_file_in_transaction(
                conn,
                Path(files_root),
                lineage.raw_id,
                str(tenant_id),
                person_id=principal,
                max_bytes=min(max(0, int(max_bytes)), MAX_BUNDLE_ITEM_BYTES),
            )
        except (AuthorizedFileReadError, FileRecordUnavailable, OSError, ValueError) as exc:
            raise EngineerArtifactBundleError("source_unavailable") from exc
        token = stored.snapshot_token
        content = bytes(stored.content)
        digest = _digest(content)
        if (
            not authorized_file_snapshot_token_is_process_owned(token)
            or token != lineage.snapshot_token
            or stored.raw_id != lineage.raw_id
            or digest != lineage.content_sha256
            or len(content) != lineage.size_bytes
            or _safe_filename(stored.filename, fallback="source.bin") != lineage.filename
            or _mime(stored.mime_type) != lineage.mime_type
        ):
            raise EngineerArtifactBundleError("source_lineage_changed")
        total += len(content)
        if total > max(0, int(max_bytes)):
            raise EngineerArtifactBundleError("source_size_limit")
        admitted.append(
            BundleSource(
                filename=lineage.filename,
                mime_type=lineage.mime_type,
                content=content,
                content_sha256=digest,
                origin_kind="owned_raw",
                _authority=_AUTHORITY,
            )
        )
    return tuple(admitted)


def produced_artifact(
    *,
    filename: str,
    content: bytes,
    mime_type: str,
    role: str,
    parent_sha256s: Sequence[str],
    tool_name: str,
    tool_version: str,
    verification_checks: Sequence[str],
) -> ProducedArtifact:
    """Freeze one adapter output without implying it was ever executed."""

    payload = bytes(content)
    normalized_role = str(role or "").strip().casefold()
    normalized_tool = str(tool_name or "").strip()
    normalized_version = " ".join(str(tool_version or "").split())
    checks = tuple(sorted({str(item or "").strip().casefold() for item in verification_checks}))
    parents = _sha256s(parent_sha256s, allow_empty=normalized_role == "report")
    if (
        not payload
        or len(payload) > MAX_BUNDLE_ITEM_BYTES
        or normalized_role not in _ROLES
        or _TOOL_NAME.fullmatch(normalized_tool) is None
        or _TOOL_VERSION.fullmatch(normalized_version) is None
        or not checks
        or len(checks) > 32
        or any(_CODE.fullmatch(item) is None for item in checks)
    ):
        raise EngineerArtifactBundleError("produced_artifact_invalid")
    return ProducedArtifact(
        filename=_safe_filename(filename),
        mime_type=_mime(mime_type),
        content=payload,
        content_sha256=_digest(payload),
        role=normalized_role,  # type: ignore[arg-type]
        parent_sha256s=parents,
        tool_name=normalized_tool,
        tool_version=normalized_version,
        verification_checks=checks,
        _authority=_AUTHORITY,
    )


def _source_public(source: BundleSource, path: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": path,
        "filename": source.filename,
        "mime_type": source.mime_type,
        "size_bytes": len(source.content),
        "sha256": source.content_sha256,
        "origin_kind": source.origin_kind,
        "parent_sha256s": list(source.parent_sha256s),
    }
    if source.origin_kind == "generated":
        item.update(
            {
                "instruction_sha256": source.instruction_sha256,
                "origin_user_message_sha256": source.origin_user_message_sha256,
                "producer": source.producer,
            }
        )
    return item


def _artifact_public(artifact: ProducedArtifact, path: str) -> dict[str, Any]:
    return {
        "path": path,
        "filename": artifact.filename,
        "mime_type": artifact.mime_type,
        "size_bytes": len(artifact.content),
        "sha256": artifact.content_sha256,
        "role": artifact.role,
        "parent_sha256s": list(artifact.parent_sha256s),
        "tool_name": artifact.tool_name,
        "tool_version": artifact.tool_version,
        "verification_checks": list(artifact.verification_checks),
    }


def _zip_entry(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    archive.writestr(info, payload)


def _validate_sources_and_artifacts(
    sources: Sequence[BundleSource],
    artifacts: Sequence[ProducedArtifact],
) -> tuple[tuple[BundleSource, ...], tuple[ProducedArtifact, ...]]:
    checked_sources = tuple(sources)
    checked_artifacts = tuple(artifacts)
    if not 1 <= len(checked_sources) <= MAX_BUNDLE_SOURCES:
        raise EngineerArtifactBundleError("source_count_invalid")
    if not 1 <= len(checked_artifacts) <= MAX_BUNDLE_ARTIFACTS:
        raise EngineerArtifactBundleError("artifact_count_invalid")
    if any(
        type(item) is not BundleSource
        or item._authority is not _AUTHORITY
        or item.content_sha256 != _digest(item.content)
        or not 1 <= len(item.content) <= MAX_BUNDLE_ITEM_BYTES
        for item in checked_sources
    ):
        raise EngineerArtifactBundleError("source_lineage_invalid")
    if any(
        type(item) is not ProducedArtifact
        or item._authority is not _AUTHORITY
        or item.content_sha256 != _digest(item.content)
        or not 1 <= len(item.content) <= MAX_BUNDLE_ITEM_BYTES
        for item in checked_artifacts
    ):
        raise EngineerArtifactBundleError("produced_artifact_invalid")
    source_digests = {item.content_sha256 for item in checked_sources}
    known_lineage = source_digests | {parent for item in checked_sources for parent in item.parent_sha256s}
    if any(not set(item.parent_sha256s) <= known_lineage for item in checked_artifacts):
        raise EngineerArtifactBundleError("artifact_parent_missing")
    artifact_names = [item.filename.casefold() for item in checked_artifacts]
    if len(artifact_names) != len(set(artifact_names)):
        raise EngineerArtifactBundleError("artifact_filename_collision")
    return (
        tuple(
            sorted(
                checked_sources,
                key=lambda item: (item.origin_kind, item.content_sha256, item.filename.casefold()),
            )
        ),
        tuple(
            sorted(
                checked_artifacts,
                key=lambda item: (item.filename.casefold(), item.role, item.content_sha256),
            )
        ),
    )


def build_engineer_artifact_delivery(
    *,
    sources: Sequence[BundleSource],
    artifacts: Sequence[ProducedArtifact],
    bundle_name: str,
    operation: str,
    max_bundle_bytes: int = MAX_BUNDLE_BYTES,
) -> EngineerArtifactDelivery:
    """Build direct outputs plus their bundle under one cumulative byte cap."""

    normalized_operation = str(operation or "").strip().casefold()
    if normalized_operation not in _OPERATIONS:
        raise EngineerArtifactBundleError("bundle_operation_invalid")
    ordered_sources, ordered_artifacts = _validate_sources_and_artifacts(sources, artifacts)
    limit = min(max(0, int(max_bundle_bytes)), MAX_BUNDLE_BYTES)
    content_bytes = sum(len(item.content) for item in ordered_sources) + sum(
        len(item.content) for item in ordered_artifacts
    )
    if limit <= 0 or content_bytes > limit:
        raise EngineerArtifactBundleError("bundle_size_limit")

    source_rows: list[dict[str, Any]] = []
    artifact_rows: list[dict[str, Any]] = []
    entries: list[tuple[str, bytes]] = []
    for index, source in enumerate(ordered_sources, start=1):
        path = f"sources/{index:02d}-{source.filename}"
        source_rows.append(_source_public(source, path))
        entries.append((path, source.content))
    for index, artifact in enumerate(ordered_artifacts, start=1):
        path = f"artifacts/{index:02d}-{artifact.filename}"
        artifact_rows.append(_artifact_public(artifact, path))
        entries.append((path, artifact.content))

    manifest: dict[str, Any] = {
        "schema": BUNDLE_MANIFEST_SCHEMA,
        "operation": normalized_operation,
        "source_count": len(source_rows),
        "artifact_count": len(artifact_rows),
        "sources": source_rows,
        "artifacts": artifact_rows,
        "receipt_path": "RECEIPT.json",
    }
    manifest_bytes = _canonical_json(manifest)
    receipt: dict[str, Any] = {
        "schema": BUNDLE_RECEIPT_SCHEMA,
        "operation": normalized_operation,
        "manifest_sha256": _digest(manifest_bytes),
        "source_sha256s": [item["sha256"] for item in source_rows],
        "artifact_sha256s": [item["sha256"] for item in artifact_rows],
        "toolchains": [
            {"tool_name": item.tool_name, "tool_version": item.tool_version} for item in ordered_artifacts
        ],
        "verification_checks": [
            {"artifact_sha256": item.content_sha256, "checks": list(item.verification_checks)}
            for item in ordered_artifacts
        ],
        "sample_executed": False,
        "network": "none",
        "runtime_validation": "not_performed",
    }
    receipt_bytes = _canonical_json(receipt)

    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            _zip_entry(archive, "MANIFEST.json", manifest_bytes)
            _zip_entry(archive, "RECEIPT.json", receipt_bytes)
            for name, payload in entries:
                _zip_entry(archive, name, payload)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise EngineerArtifactBundleError("bundle_write_failed") from exc
    bundle_payload = output.getvalue()
    delivery_bytes = sum(len(item.content) for item in ordered_artifacts) + len(bundle_payload)
    if not bundle_payload or len(bundle_payload) > limit or delivery_bytes > limit:
        raise EngineerArtifactBundleError("bundle_size_limit")
    bundle = EngineerArtifactBundle(
        filename=_safe_bundle_filename(bundle_name),
        mime_type=BUNDLE_MIME_TYPE,
        payload=bundle_payload,
        sha256=_digest(bundle_payload),
        manifest=manifest,
        receipt=receipt,
    )
    attachments = tuple(
        {
            "kind": "document",
            "filename": item.filename,
            "mime_type": item.mime_type,
            "content_base64": base64.b64encode(item.content).decode("ascii"),
        }
        for item in ordered_artifacts
    ) + (
        {
            "kind": "document",
            "filename": bundle.filename,
            "mime_type": bundle.mime_type,
            "content_base64": base64.b64encode(bundle.payload).decode("ascii"),
        },
    )
    return EngineerArtifactDelivery(
        artifacts=ordered_artifacts,
        bundle=bundle,
        attachments=attachments,
    )


__all__ = [
    "BUNDLE_MANIFEST_SCHEMA",
    "BUNDLE_MIME_TYPE",
    "BUNDLE_RECEIPT_SCHEMA",
    "BundleSource",
    "BundleSourceLineage",
    "EngineerArtifactBundle",
    "EngineerArtifactBundleError",
    "EngineerArtifactDelivery",
    "MAX_BUNDLE_ARTIFACTS",
    "MAX_BUNDLE_BYTES",
    "MAX_BUNDLE_ITEM_BYTES",
    "MAX_BUNDLE_SOURCES",
    "ProducedArtifact",
    "build_engineer_artifact_delivery",
    "bundle_source_lineage",
    "generated_bundle_source",
    "produced_artifact",
    "reauthorize_bundle_sources_in_transaction",
]
