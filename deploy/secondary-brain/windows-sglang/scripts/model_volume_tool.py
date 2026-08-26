"""Populate or verify the exact sealed abliterated GPT-OSS source volume."""

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
MODEL_REPOSITORY = "huihui-ai/Huihui-gpt-oss-20b-mxfp4-abliterated-v2"
MODEL_REVISION = "79f64a520a4a0275f639c1a47d9a5614a8a54477"
SNAPSHOT_DIRECTORY = "snapshot"
SOURCE_MANIFEST_NAME = "source-manifest.json"
SOURCE_EXCLUDED_PREFIXES = ["GGUF/"]
SOURCE_FILE_COUNT = 12
SOURCE_TOTAL_BYTES = 13_789_257_124
SOURCE_MANIFEST_RAW_SHA256 = "8dfc3a50d1a9407fbb07dde5f1b494157664c75cdd0e140ecb85f7d55732a296"
SOURCE_MANIFEST_SEMANTIC_SHA256 = "4ab38461ce42f76c32d998ed091b8cfc0a8b483279f676eb8221e56df28d6d02"
SOURCE_FILES = {
    ".gitattributes": (1653, "47af3bffdadc5314122fc91026aca376ceea3932bac23a52dd950da21a07a8cc"),
    "README.md": (9660, "436c7ebb1d039bb4651dcc2a90955c093a65ab4d683cb96b50fdb456f2b9a7aa"),
    "chat_template.jinja": (
        15078,
        "445c3a7c29d9cf61860179de179f60b6cf24834518b491016993eba63c8b1ecc",
    ),
    "config.json": (2091, "b25d0700aca90b471b3a39bdd6d6a2fea1f31086316cd0575d9ea7bd7c02d4ca"),
    "generation_config.json": (
        165,
        "97b165839e19bf43309d1e571d1720ebc081dde24e6722cfb644257215ad1e66",
    ),
    "model-00001-of-00003.safetensors": (
        4_999_744_880,
        "a5720f36ef8e3c331388c17c01496a1d911282a8eb8deb7800cc40aa047ee554",
    ),
    "model-00002-of-00003.safetensors": (
        4_795_391_048,
        "477e0bcd49ad66c87e7375848cf8f5125b9d25119e7dec108a082399b8506a72",
    ),
    "model-00003-of-00003.safetensors": (
        3_966_181_488,
        "fa8cdf4bc70e87b551c3f5de19ec0ce08faced901db28f22bf2557d66be6e5b3",
    ),
    "model.safetensors.index.json": (
        38247,
        "0138dfec3982a0d2673f0989eb2356109cf61318a88f03e75b546380f2ac6489",
    ),
    "special_tokens_map.json": (
        440,
        "8464cabd6eda239fe46ebf8ae63b46c417721784a961a022f6b59174a2cda0e2",
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
        raise ModelVolumeError("pinned abliterated source download failed") from exc


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
