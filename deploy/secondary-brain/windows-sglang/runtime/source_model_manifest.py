"""Content-free verifier for the sealed official GPT-OSS source snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "friday.secondary-source-manifest.v1"
SOURCE_REPOSITORY = "openai/gpt-oss-20b"
SOURCE_REVISION = "6cee5e81ee83917806bbde320786a8fb61efebee"
SOURCE_FILE_COUNT = 14
SOURCE_TOTAL_BYTES = 13_789_264_674
SOURCE_EXCLUDED_PREFIXES = ["metal/", "original/"]
SOURCE_MANIFEST_RAW_SHA256 = "438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9"
SOURCE_MANIFEST_SEMANTIC_SHA256 = "e75b176ed1817e762cf9b7f2262f6e58491a0f9d48d1ea51e466a6e2c3b8a3ab"
SOURCE_FILES = {
    ".gitattributes": (1570, "34448b82c17d60fec9b65b1f093c115ddbaadc04beb1b0140b6bfed2e012a930"),
    "LICENSE": (11357, "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd"),
    "README.md": (7095, "03c2fcf292549176757b85c911e7dcf527aef3e4241d64b6caec94af3ecf3ac2"),
    "USAGE_POLICY": (200, "d6387ef7985019c45db8d9801e6ac5fd9f98f02b9f1c56f8c5af80c3e8f385d0"),
    "chat_template.jinja": (
        16738,
        "a4c9919cbbd4acdd51ccffe22da049264b1b73e59055fa58811a99efbd7c8146",
    ),
    "config.json": (1806, "3a2a26ded679375b7928ddeca59764df7cea83220c1961035f6d6e232659e9ce"),
    "generation_config.json": (
        177,
        "f9970ada892d2d1f72e3ed0a6535ccebadd11897318794ca671d8c7014c957da",
    ),
    "model-00000-of-00002.safetensors": (
        4_792_272_488,
        "16d0f997dcfc4462089d536bffe51b4bcea2f872f5c430be09ef8ed392312427",
    ),
    "model-00001-of-00002.safetensors": (
        4_798_702_184,
        "4fbe328ab445455d6f58dc73852b85873bd626986310abd91cd4d2ce3245eaea",
    ),
    "model-00002-of-00002.safetensors": (
        4_170_342_232,
        "a18106b209e9ab35c3406db4f6f12a927364a058b21e9d1373d682e20674b303",
    ),
    "model.safetensors.index.json": (
        36355,
        "0e085b977c4c9942f85938828e8c989ed7d5cdabf852e4da6a67c116cd502cd1",
    ),
    "special_tokens_map.json": (
        98,
        "dd5e191d20c12d2fee1da5bae14ca1db0f5f4215300af691f23cdee97120a293",
    ),
    "tokenizer.json": (
        27_868_174,
        "0614fe83cadab421296e664e1f48f4261fa8fef6e03e63bb75c20f38e37d07d3",
    ),
    "tokenizer_config.json": (
        4200,
        "9279e942392b742d633c7adbb89ebe002c98399db8926a7af5125c726f404070",
    ),
}
_MAX_MANIFEST_BYTES = 1 << 20


class SourceModelManifestError(RuntimeError):
    """A content-free launch rejection."""


@dataclass(frozen=True, slots=True)
class SourceModelReceipt:
    manifest_sha256: str
    source_revision: str
    file_count: int
    total_bytes: int


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SourceModelManifestError("source manifest contains a duplicate key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise SourceModelManifestError("source manifest contains a non-finite number")


def _read_regular(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum_bytes:
            raise SourceModelManifestError(f"{label} size or file type is invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != metadata.st_dev
            or before.st_ino != metadata.st_ino
            or before.st_size != metadata.st_size
            or before.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise SourceModelManifestError(f"{label} changed before verification")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(raw) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SourceModelManifestError(f"{label} changed during verification")
        return raw
    except SourceModelManifestError:
        raise
    except OSError as exc:
        raise SourceModelManifestError(f"{label} is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _hash_regular(path: Path, *, expected_size: int) -> str:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise SourceModelManifestError("source snapshot file identity is invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != metadata.st_dev
            or before.st_ino != metadata.st_ino
            or before.st_size != metadata.st_size
            or before.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise SourceModelManifestError("source snapshot changed before verification")
        digest = hashlib.sha256()
        observed_size = 0
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            while chunk := stream.read(8 * 1024 * 1024):
                observed_size += len(chunk)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if observed_size != expected_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SourceModelManifestError("source snapshot changed during verification")
        return digest.hexdigest()
    except SourceModelManifestError:
        raise
    except OSError as exc:
        raise SourceModelManifestError("source snapshot file is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_manifest(path: Path, expected_sha256: str) -> dict[str, Any]:
    if expected_sha256 != SOURCE_MANIFEST_RAW_SHA256:
        raise SourceModelManifestError("source manifest expectation is invalid")
    raw = _read_regular(path, maximum_bytes=_MAX_MANIFEST_BYTES, label="source manifest")
    if hashlib.sha256(raw).hexdigest() != expected_sha256 or raw.startswith(b"\xef\xbb\xbf"):
        raise SourceModelManifestError("source manifest bytes are invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except SourceModelManifestError:
        raise
    except Exception:
        raise SourceModelManifestError("source manifest is not strict UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise SourceModelManifestError("source manifest is not an object")
    semantic = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(semantic).hexdigest() != SOURCE_MANIFEST_SEMANTIC_SHA256:
        raise SourceModelManifestError("source manifest semantic identity is invalid")
    return value


def _validate_manifest(value: dict[str, Any]) -> None:
    expected_keys = {
        "schema",
        "status",
        "repository",
        "revision",
        "root_only",
        "excluded_prefixes",
        "file_count",
        "total_bytes",
        "files",
    }
    if set(value) != expected_keys:
        raise SourceModelManifestError("source manifest shape is invalid")
    expected_identity = {
        "schema": SCHEMA,
        "status": "verified",
        "repository": SOURCE_REPOSITORY,
        "revision": SOURCE_REVISION,
        "root_only": True,
        "excluded_prefixes": SOURCE_EXCLUDED_PREFIXES,
        "file_count": SOURCE_FILE_COUNT,
        "total_bytes": SOURCE_TOTAL_BYTES,
    }
    if any(value.get(key) != expected for key, expected in expected_identity.items()):
        raise SourceModelManifestError("source manifest identity is invalid")
    rows = value.get("files")
    if not isinstance(rows, dict) or len(rows) != SOURCE_FILE_COUNT:
        raise SourceModelManifestError("source manifest file set is invalid")
    observed: dict[str, tuple[int, str]] = {}
    for name, row in rows.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 255
            or PurePosixPath(name).name != name
            or not isinstance(row, dict)
            or set(row) != {"bytes", "sha256"}
        ):
            raise SourceModelManifestError("source manifest file row is invalid")
        size = row.get("bytes")
        digest = row.get("sha256")
        if type(size) is not int or size <= 0 or not _is_sha256(digest):
            raise SourceModelManifestError("source manifest file identity is invalid")
        observed[name] = (size, str(digest))
    if observed != SOURCE_FILES:
        raise SourceModelManifestError("source manifest differs from the sealed snapshot")


def verify_source_model_manifest(
    manifest_path: Path,
    expected_manifest_sha256: str,
) -> SourceModelReceipt:
    """Verify the exported manifest identity without requiring its Docker volume."""

    manifest = _load_manifest(manifest_path, expected_manifest_sha256)
    _validate_manifest(manifest)
    return SourceModelReceipt(
        manifest_sha256=expected_manifest_sha256,
        source_revision=SOURCE_REVISION,
        file_count=SOURCE_FILE_COUNT,
        total_bytes=SOURCE_TOTAL_BYTES,
    )


def verify_source_model_snapshot(
    source_root: Path,
    expected_manifest_sha256: str,
) -> SourceModelReceipt:
    """Verify the exact manifest and every byte before SGLang imports or CUDA state."""

    try:
        root_metadata = source_root.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise SourceModelManifestError("source root is absent or unsafe")
        entries = {entry.name: entry for entry in source_root.iterdir()}
    except SourceModelManifestError:
        raise
    except OSError as exc:
        raise SourceModelManifestError("source root is unavailable") from exc
    if set(entries) != {"snapshot", "source-manifest.json"}:
        raise SourceModelManifestError("source root has an unexpected entry")
    snapshot = entries["snapshot"]
    try:
        snapshot_metadata = snapshot.lstat()
        if not stat.S_ISDIR(snapshot_metadata.st_mode):
            raise SourceModelManifestError("source snapshot is absent or unsafe")
        files = sorted(snapshot.iterdir(), key=lambda path: path.name)
    except SourceModelManifestError:
        raise
    except OSError as exc:
        raise SourceModelManifestError("source snapshot is unavailable") from exc
    if [path.name for path in files] != sorted(SOURCE_FILES):
        raise SourceModelManifestError("source snapshot file set is invalid")

    receipt = verify_source_model_manifest(
        entries["source-manifest.json"],
        expected_manifest_sha256,
    )
    total_bytes = 0
    for path in files:
        expected_size, expected_digest = SOURCE_FILES[path.name]
        if _hash_regular(path, expected_size=expected_size) != expected_digest:
            raise SourceModelManifestError("source snapshot file content is invalid")
        total_bytes += expected_size
    if total_bytes != SOURCE_TOTAL_BYTES:
        raise SourceModelManifestError("source snapshot aggregate is invalid")
    return SourceModelReceipt(
        manifest_sha256=expected_manifest_sha256,
        source_revision=receipt.source_revision,
        file_count=receipt.file_count,
        total_bytes=total_bytes,
    )
