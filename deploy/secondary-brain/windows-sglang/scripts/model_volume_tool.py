"""Populate or verify the exact sealed official GPT-OSS source volume."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "friday.secondary-source-manifest.v1"
MODEL_REPOSITORY = "openai/gpt-oss-20b"
MODEL_REVISION = "6cee5e81ee83917806bbde320786a8fb61efebee"
SNAPSHOT_DIRECTORY = "snapshot"
SOURCE_MANIFEST_NAME = "source-manifest.json"
SOURCE_EXCLUDED_PREFIXES = ["metal/", "original/"]
SOURCE_FILE_COUNT = 14
SOURCE_TOTAL_BYTES = 13_789_264_674
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


class ModelVolumeError(RuntimeError):
    """A content-free population or verification failure."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ModelVolumeError("source manifest contains a duplicate key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ModelVolumeError("source manifest contains a non-finite number")


def _canonical_manifest() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "verified",
        "repository": MODEL_REPOSITORY,
        "revision": MODEL_REVISION,
        "root_only": True,
        "excluded_prefixes": SOURCE_EXCLUDED_PREFIXES,
        "file_count": SOURCE_FILE_COUNT,
        "total_bytes": SOURCE_TOTAL_BYTES,
        "files": {name: {"bytes": size, "sha256": digest} for name, (size, digest) in SOURCE_FILES.items()},
    }


def canonical_manifest_bytes() -> bytes:
    raw = (
        json.dumps(
            _canonical_manifest(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    semantic = json.dumps(
        _canonical_manifest(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if (
        hashlib.sha256(raw).hexdigest() != SOURCE_MANIFEST_RAW_SHA256
        or hashlib.sha256(semantic).hexdigest() != SOURCE_MANIFEST_SEMANTIC_SHA256
    ):
        raise ModelVolumeError("code-owned source manifest identity is inconsistent")
    return raw


def _read_regular(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum_bytes:
            raise ModelVolumeError(f"{label} size or file type is invalid")
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
            raise ModelVolumeError(f"{label} changed before verification")
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
            raise ModelVolumeError(f"{label} changed during verification")
        return raw
    except ModelVolumeError:
        raise
    except OSError as exc:
        raise ModelVolumeError(f"{label} is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _hash_regular(path: Path, *, expected_size: int) -> str:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise ModelVolumeError("source snapshot file identity is invalid")
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
            raise ModelVolumeError("source snapshot changed before verification")
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
            raise ModelVolumeError("source snapshot changed during verification")
        return digest.hexdigest()
    except ModelVolumeError:
        raise
    except OSError as exc:
        raise ModelVolumeError("source snapshot file is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_manifest(value: dict[str, Any]) -> None:
    if set(value) != {
        "schema",
        "status",
        "repository",
        "revision",
        "root_only",
        "excluded_prefixes",
        "file_count",
        "total_bytes",
        "files",
    }:
        raise ModelVolumeError("source manifest shape is invalid")
    if (
        value.get("schema") != SCHEMA
        or value.get("status") != "verified"
        or value.get("repository") != MODEL_REPOSITORY
        or value.get("revision") != MODEL_REVISION
        or value.get("root_only") is not True
        or value.get("excluded_prefixes") != SOURCE_EXCLUDED_PREFIXES
        or type(value.get("file_count")) is not int
        or value.get("file_count") != SOURCE_FILE_COUNT
        or type(value.get("total_bytes")) is not int
        or value.get("total_bytes") != SOURCE_TOTAL_BYTES
    ):
        raise ModelVolumeError("source manifest identity is invalid")
    rows = value.get("files")
    if not isinstance(rows, dict) or len(rows) != SOURCE_FILE_COUNT:
        raise ModelVolumeError("source manifest file set is invalid")
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
            raise ModelVolumeError("source manifest file row is invalid")
        size = row.get("bytes")
        digest = row.get("sha256")
        if (
            type(size) is not int
            or size <= 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ModelVolumeError("source manifest file identity is invalid")
        observed[name] = (size, digest)
    if observed != SOURCE_FILES:
        raise ModelVolumeError("source manifest differs from the sealed snapshot")


def _load_manifest(path: Path) -> dict[str, Any]:
    raw = _read_regular(path, maximum_bytes=_MAX_MANIFEST_BYTES, label="source manifest")
    if raw.startswith(b"\xef\xbb\xbf") or hashlib.sha256(raw).hexdigest() != SOURCE_MANIFEST_RAW_SHA256:
        raise ModelVolumeError("source manifest bytes are invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except ModelVolumeError:
        raise
    except Exception:
        raise ModelVolumeError("source manifest is not strict UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise ModelVolumeError("source manifest is not an object")
    semantic = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if hashlib.sha256(semantic).hexdigest() != SOURCE_MANIFEST_SEMANTIC_SHA256:
        raise ModelVolumeError("source manifest semantic identity is invalid")
    _validate_manifest(value)
    return value


def _snapshot_files(snapshot: Path) -> list[Path]:
    try:
        metadata = snapshot.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ModelVolumeError("source snapshot is absent or unsafe")
        files = sorted(snapshot.iterdir(), key=lambda item: item.name)
    except ModelVolumeError:
        raise
    except OSError as exc:
        raise ModelVolumeError("source snapshot is unavailable") from exc
    if [path.name for path in files] != sorted(SOURCE_FILES):
        raise ModelVolumeError("source snapshot file set is invalid")
    return files


def _verify_snapshot(snapshot: Path) -> None:
    total_bytes = 0
    for path in _snapshot_files(snapshot):
        size, expected_digest = SOURCE_FILES[path.name]
        if _hash_regular(path, expected_size=size) != expected_digest:
            raise ModelVolumeError("source snapshot file content is invalid")
        total_bytes += size
    if total_bytes != SOURCE_TOTAL_BYTES:
        raise ModelVolumeError("source snapshot aggregate is invalid")


def _verify_source_root(source_root: Path) -> None:
    try:
        metadata = source_root.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ModelVolumeError("source volume root is absent or unsafe")
        entries = {entry.name: entry for entry in source_root.iterdir()}
    except ModelVolumeError:
        raise
    except OSError as exc:
        raise ModelVolumeError("source volume root is unavailable") from exc
    if set(entries) != {SNAPSHOT_DIRECTORY, SOURCE_MANIFEST_NAME}:
        raise ModelVolumeError("source volume root has an unexpected entry")
    _load_manifest(entries[SOURCE_MANIFEST_NAME])
    _verify_snapshot(entries[SNAPSHOT_DIRECTORY])


def verify_manifest(source_root: Path, manifest_path: Path | None = None) -> dict[str, Any]:
    _verify_source_root(source_root)
    if manifest_path is not None:
        _load_manifest(manifest_path)
    return {
        "schema": "friday.secondary-source-volume-verification.v1",
        "status": "passed",
        "repository": MODEL_REPOSITORY,
        "revision": MODEL_REVISION,
        "file_count": SOURCE_FILE_COUNT,
        "total_bytes": SOURCE_TOTAL_BYTES,
        "manifest_raw_sha256": SOURCE_MANIFEST_RAW_SHA256,
        "manifest_semantic_sha256": SOURCE_MANIFEST_SEMANTIC_SHA256,
        "raw_content_retained": False,
    }


def _write_manifest(source_root: Path) -> None:
    destination = source_root / SOURCE_MANIFEST_NAME
    temporary = source_root / f".{SOURCE_MANIFEST_NAME}.tmp-{os.getpid()}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        raw = canonical_manifest_bytes()
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o444)
    except OSError as exc:
        raise ModelVolumeError("source manifest could not be sealed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def download_snapshot(volume: Path) -> None:
    try:
        metadata = volume.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or any(volume.iterdir()):
            raise ModelVolumeError("population requires a new empty Docker volume")
    except ModelVolumeError:
        raise
    except OSError as exc:
        raise ModelVolumeError("population volume is unavailable") from exc
    try:
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ModelVolumeError("pinned downloader image has no huggingface_hub") from exc

    snapshot = volume / SNAPSHOT_DIRECTORY
    try:
        snapshot_download(
            repo_id=MODEL_REPOSITORY,
            revision=MODEL_REVISION,
            local_dir=snapshot,
            allow_patterns=sorted(SOURCE_FILES),
            ignore_patterns=[f"{prefix}*" for prefix in SOURCE_EXCLUDED_PREFIXES],
            token=None,
        )
        metadata_path = snapshot / ".cache"
        if metadata_path.is_symlink():
            raise ModelVolumeError("downloader metadata path is unsafe")
        if metadata_path.exists():
            shutil.rmtree(metadata_path)
        _verify_snapshot(snapshot)
        _write_manifest(volume)
        _load_manifest(volume / SOURCE_MANIFEST_NAME)
        os.sync()
    except ModelVolumeError:
        raise
    except Exception as exc:
        raise ModelVolumeError("pinned official source download failed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser("download")
    download.add_argument("--volume", type=Path, default=Path("/volume"))
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--source", type=Path, default=Path("/volume"))
    verify = subparsers.add_parser("verify")
    verify.add_argument("--source", type=Path, default=Path("/volume"))
    verify.add_argument("--manifest", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "download":
            download_snapshot(args.volume)
            sys.stdout.buffer.write(canonical_manifest_bytes())
        elif args.command == "manifest":
            _verify_source_root(args.source)
            sys.stdout.buffer.write(canonical_manifest_bytes())
        else:
            result = verify_manifest(args.source, args.manifest)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 0
    except ModelVolumeError as exc:
        print(f"model volume operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
