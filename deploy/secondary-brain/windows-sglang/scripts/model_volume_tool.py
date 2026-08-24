"""Run inside a pinned downloader image to discover or verify a model volume."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any

MODEL_REPOSITORY = "shanjiaz/gpt-oss-20b-nvfp4-modelopt"
MODEL_REVISION = "fb9848e169d5b38cbc00ecf3383283ea1fc33a21"
SCHEMA = "friday.secondary-model-snapshot.v1"
SNAPSHOT_DIRECTORY = "snapshot"
MAX_FILES = 4096


class ModelVolumeError(RuntimeError):
    pass


def _safe_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ModelVolumeError("snapshot root is absent or unsafe")
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ModelVolumeError("snapshot contains a symbolic link")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ModelVolumeError("snapshot contains a non-regular entry")
        if len(files) > MAX_FILES:
            raise ModelVolumeError("snapshot exceeds the file-count bound")
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def observed_manifest(root: Path) -> dict[str, Any]:
    files = _safe_files(root)
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        rows.append({"path": relative, "size": size, "sha256": _sha256(path)})
    if not rows:
        raise ModelVolumeError("snapshot is empty")
    return {
        "schema": SCHEMA,
        "status": "observed_unaccepted",
        "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION,
        "snapshot_directory": SNAPSHOT_DIRECTORY,
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "files": rows,
        "note": "Review live loader evidence, then change only status to accepted before sealing.",
    }


def _validated_manifest(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ModelVolumeError("manifest exceeds 2 MiB")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelVolumeError("manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise ModelVolumeError("manifest must be a JSON object")
    expected = {
        "schema": SCHEMA,
        "status": "accepted",
        "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION,
        "snapshot_directory": SNAPSHOT_DIRECTORY,
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ModelVolumeError("manifest identity is not accepted and exact")
    rows = value.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
        raise ModelVolumeError("manifest file collection is invalid")
    return value


def verify_manifest(snapshot: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _validated_manifest(manifest_path)
    expected_rows = manifest["files"]
    expected: dict[str, tuple[int, str]] = {}
    for row in expected_rows:
        if not isinstance(row, dict):
            raise ModelVolumeError("manifest file row is invalid")
        relative = row.get("path")
        size = row.get("size")
        digest = row.get("sha256")
        if not isinstance(relative, str) or not relative or len(relative) > 512:
            raise ModelVolumeError("manifest path is invalid")
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or ".." in parsed.parts or "." in parsed.parts or len(parsed.parts) > 16:
            raise ModelVolumeError("manifest path escapes the snapshot")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or relative in expected
        ):
            raise ModelVolumeError("manifest file identity is invalid")
        expected[relative] = (size, digest)
    actual_files = _safe_files(snapshot)
    actual_paths = [path.relative_to(snapshot).as_posix() for path in actual_files]
    if actual_paths != sorted(expected):
        raise ModelVolumeError("snapshot file set differs from the accepted manifest")
    total_bytes = 0
    for path in actual_files:
        relative = path.relative_to(snapshot).as_posix()
        size, digest = expected[relative]
        actual_size = path.stat().st_size
        if actual_size != size or _sha256(path) != digest:
            raise ModelVolumeError("snapshot file content differs from the accepted manifest")
        total_bytes += actual_size
    if manifest.get("file_count") != len(actual_files) or manifest.get("total_bytes") != total_bytes:
        raise ModelVolumeError("manifest aggregates do not match the snapshot")
    semantic = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return {
        "schema": "friday.secondary-model-volume-verification.v1",
        "status": "passed",
        "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION,
        "file_count": len(actual_files),
        "total_bytes": total_bytes,
        "manifest_semantic_sha256": hashlib.sha256(semantic).hexdigest(),
    }


def download_snapshot(volume: Path, token_file: Path | None) -> dict[str, Any]:
    if not volume.is_dir() or volume.is_symlink() or any(volume.iterdir()):
        raise ModelVolumeError("discovery requires a new empty Docker volume")
    token: str | None = None
    if token_file is not None:
        try:
            token = token_file.read_text(encoding="utf-8").strip("\r\n")
        except OSError as exc:
            raise ModelVolumeError("token file is unavailable") from exc
        if not token or "\r" in token or "\n" in token or len(token) > 4096:
            raise ModelVolumeError("token file is invalid")
    try:
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ModelVolumeError("pinned downloader image has no huggingface_hub") from exc
    snapshot = volume / SNAPSHOT_DIRECTORY
    snapshot_download(
        repo_id=MODEL_REPOSITORY,
        revision=MODEL_REVISION,
        local_dir=snapshot,
        token=token,
    )
    metadata = snapshot / ".cache"
    if metadata.exists():
        shutil.rmtree(metadata)
    os.sync()
    return observed_manifest(snapshot)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser("download")
    download.add_argument("--volume", type=Path, default=Path("/volume"))
    download.add_argument("--token-file", type=Path)
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--snapshot", type=Path, default=Path("/volume/snapshot"))
    verify = subparsers.add_parser("verify")
    verify.add_argument("--snapshot", type=Path, default=Path("/volume/snapshot"))
    verify.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "download":
            result = download_snapshot(args.volume, args.token_file)
        elif args.command == "manifest":
            result = observed_manifest(args.snapshot)
        else:
            result = verify_manifest(args.snapshot, args.manifest)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 0
    except ModelVolumeError as exc:
        print(f"model volume operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
