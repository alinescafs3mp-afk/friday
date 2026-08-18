#!/usr/bin/env python3
"""Copy and verify the exact Qwen3.8 snapshot in a dedicated Docker volume."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn

MANIFEST_PATH = Path("/usr/local/share/friday/qwen38-model-manifest.v1.json")
SOURCE_ROOT = Path("/source-model")
SEALED_ROOT = Path("/sealed-model")
EXPECTED_MANIFEST_SHA256 = "da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333"


def _fail(message: str) -> NoReturn:
    print(f"friday-model-sealer: {message}", file=sys.stderr, flush=True)
    raise SystemExit(78)


def _reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _load_manifest() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest = json.loads(
            MANIFEST_PATH.read_bytes().decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid number")),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        _fail("invalid baked manifest")
    if not isinstance(manifest, dict):
        _fail("invalid baked manifest root")
    semantic = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if hashlib.sha256(semantic).hexdigest() != EXPECTED_MANIFEST_SHA256:
        _fail("baked manifest identity mismatch")
    if set(manifest) != {
        "schema",
        "model_repository",
        "model_revision",
        "model_quantization",
        "snapshot_directory",
        "file_count",
        "total_bytes",
        "files",
    }:
        _fail("invalid baked manifest schema")
    rows = manifest["files"]
    if not isinstance(rows, list) or len(rows) != manifest["file_count"]:
        _fail("invalid baked manifest file count")
    names: list[str] = []
    total = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
            _fail("invalid baked manifest row")
        name = row["path"]
        size = row["size"]
        digest = row["sha256"]
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            _fail("invalid baked manifest row value")
        names.append(name)
        total += size
    if names != sorted(names) or len(names) != len(set(names)) or total != manifest["total_bytes"]:
        _fail("invalid baked manifest ordering or total")
    return manifest, rows


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError:
        _fail("required model directory is unavailable")
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        _fail("required model path is not a directory")
    return descriptor


def _directory_names(descriptor: int) -> list[str]:
    try:
        names = sorted(os.listdir(descriptor))
    except OSError:
        _fail("model directory cannot be listed")
    if any(not isinstance(name, str) for name in names):
        _fail("model directory contains an invalid name")
    return names


def _hash_regular_file(directory: int, row: dict[str, Any]) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(row["path"], flags, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size != row["size"]:
                _fail("sealed model file identity mismatch")
            digest = hashlib.sha256()
            while block := os.read(descriptor, 8 * 1024 * 1024):
                digest.update(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        _fail("sealed model file is unreadable")
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or digest.hexdigest() != row["sha256"]:
        _fail("sealed model file digest mismatch")


def _verify_directory(directory: int, rows: list[dict[str, Any]]) -> None:
    expected = [row["path"] for row in rows]
    if _directory_names(directory) != expected:
        _fail("sealed model file set mismatch")
    for row in rows:
        _hash_regular_file(directory, row)


def _copy_one(source: int, destination: int, row: dict[str, Any], index: int) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    temporary = f".friday-seal-{index:02d}.tmp"
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(row["path"], source_flags, dir_fd=source)
        try:
            before = os.fstat(source_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size != row["size"]:
                _fail("source model file identity mismatch")
            destination_descriptor = os.open(temporary, destination_flags, 0o444, dir_fd=destination)
            try:
                digest = hashlib.sha256()
                copied = 0
                while block := os.read(source_descriptor, 8 * 1024 * 1024):
                    digest.update(block)
                    copied += len(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(destination_descriptor, view)
                        if written <= 0:
                            _fail("sealed model write made no progress")
                        view = view[written:]
                os.fsync(destination_descriptor)
                destination_info = os.fstat(destination_descriptor)
            finally:
                os.close(destination_descriptor)
            after = os.fstat(source_descriptor)
        finally:
            os.close(source_descriptor)
    except OSError:
        _fail("model volume copy failed")
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if (
        identity_before != identity_after
        or copied != row["size"]
        or destination_info.st_size != row["size"]
        or digest.hexdigest() != row["sha256"]
    ):
        _fail("source changed or digest mismatched during model copy")
    try:
        os.replace(temporary, row["path"], src_dir_fd=destination, dst_dir_fd=destination)
        os.chmod(row["path"], 0o444, dir_fd=destination, follow_symlinks=False)
        os.fsync(destination)
    except OSError:
        _fail("sealed model file could not be finalized")


def _result(status_value: str, manifest: dict[str, Any]) -> None:
    result = {
        "schema": "friday.model-volume-sealer-result.v1",
        "status": status_value,
        "manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"seal", "verify"}:
        _fail("expected exactly one mode: seal or verify")
    manifest, rows = _load_manifest()
    if sys.argv[1] == "verify":
        sealed = _open_directory(SEALED_ROOT)
        try:
            _verify_directory(sealed, rows)
        finally:
            os.close(sealed)
        _result("verified", manifest)
        return

    source = _open_directory(SOURCE_ROOT)
    sealed = _open_directory(SEALED_ROOT)
    try:
        if _directory_names(source) != [row["path"] for row in rows]:
            _fail("source model file set mismatch")
        if _directory_names(sealed):
            _fail("destination model volume is not empty")
        source_info = os.fstat(source)
        sealed_info = os.fstat(sealed)
        if (source_info.st_dev, source_info.st_ino) == (sealed_info.st_dev, sealed_info.st_ino):
            _fail("source and destination model directories are identical")
        for index, row in enumerate(rows):
            _copy_one(source, sealed, row, index)
        _verify_directory(sealed, rows)
    finally:
        os.close(source)
        os.close(sealed)
    _result("sealed", manifest)


if __name__ == "__main__":
    main()
