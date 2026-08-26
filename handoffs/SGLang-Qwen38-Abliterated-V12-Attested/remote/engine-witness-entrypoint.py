#!/usr/bin/env python3
"""Fail-closed Qwen3.8 model verification and per-start witness launcher."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn

IDENTITY_PATH = Path("/usr/local/share/friday/deployment-identity.v1.json")
MODEL_MANIFEST_PATH = Path("/usr/local/share/friday/qwen38-model-manifest.v1.json")
LAUNCH_MANIFEST_PATH = Path("/usr/local/share/friday/launch-manifest.v1.json")
WITNESS_DIRECTORY = Path("/run/friday-witness")
WITNESS_PATH = WITNESS_DIRECTORY / "deployment-witness.v1.json"
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _fail(message: str) -> NoReturn:
    print(f"friday-witness: {message}", file=sys.stderr, flush=True)
    raise SystemExit(78)


def _load_exact_json(path: Path) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid number")),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        _fail(f"invalid immutable JSON: {path.name}")
    if not isinstance(value, dict):
        _fail(f"invalid immutable JSON root: {path.name}")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _semantic_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _fail(f"unexpected {label} schema")


def _verify_model_snapshot(manifest: dict[str, Any], model_root: Path) -> None:
    _require_exact_keys(
        manifest,
        {
            "schema",
            "model_repository",
            "model_revision",
            "model_quantization",
            "snapshot_directory",
            "file_count",
            "total_bytes",
            "files",
        },
        "model manifest",
    )
    if manifest["schema"] != "friday.model-snapshot-manifest.v1":
        _fail("unexpected model manifest version")
    rows = manifest["files"]
    if not isinstance(rows, list) or len(rows) != manifest["file_count"]:
        _fail("invalid model file count")
    if not model_root.is_dir() or model_root.is_symlink():
        _fail("model mount is missing or is a symlink")

    expected_paths: list[str] = []
    expected_total = 0
    for row in rows:
        if not isinstance(row, dict):
            _fail("invalid model manifest row")
        _require_exact_keys(row, {"path", "size", "sha256"}, "model file row")
        relative = row["path"]
        size = row["size"]
        digest = row["sha256"]
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
        ):
            _fail("invalid model manifest row value")
        expected_paths.append(relative)
        expected_total += size
    if expected_paths != sorted(expected_paths) or len(set(expected_paths)) != len(expected_paths):
        _fail("model manifest paths are not unique and ordered")
    if expected_total != manifest["total_bytes"]:
        _fail("model manifest byte total mismatch")

    observed_paths: list[str] = []
    for path in model_root.rglob("*"):
        relative = path.relative_to(model_root).as_posix()
        if path.is_symlink() or not path.is_file():
            _fail("model snapshot contains a link or non-file entry")
        observed_paths.append(relative)
    if sorted(observed_paths) != expected_paths:
        _fail("model snapshot file set mismatch")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for row in rows:
        path = model_root / row["path"]
        try:
            fd = os.open(path, os.O_RDONLY | nofollow)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size != row["size"]:
                    _fail("model file identity mismatch")
                hasher = hashlib.sha256()
                while True:
                    block = os.read(fd, 8 * 1024 * 1024)
                    if not block:
                        break
                    hasher.update(block)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError:
            _fail("model file is unreadable")
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity or hasher.hexdigest() != row["sha256"]:
            _fail("model file digest mismatch")


def _required_image_id(name: str) -> str:
    value = os.environ.get(name, "")
    if IMAGE_ID_PATTERN.fullmatch(value) is None:
        _fail(f"missing or invalid {name}")
    return value


def _prepare_witness_directory() -> None:
    WITNESS_DIRECTORY.mkdir(mode=0o755, parents=True, exist_ok=True)
    try:
        directory_info = WITNESS_DIRECTORY.lstat()
    except OSError:
        _fail("witness directory is unavailable")
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
        _fail("witness directory is not a real directory")
    if WITNESS_PATH.exists() or WITNESS_PATH.is_symlink():
        try:
            WITNESS_PATH.unlink()
        except OSError:
            _fail("stale witness cannot be removed")
    directory_fd = os.open(WITNESS_DIRECTORY, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_witness(value: dict[str, Any]) -> None:
    if WITNESS_PATH.exists() or WITNESS_PATH.is_symlink():
        _fail("witness path changed during model verification")
    payload = _canonical_json(value) + b"\n"
    temporary = WITNESS_DIRECTORY / f".deployment-witness.{value['engine_start_nonce']}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o444)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, WITNESS_PATH)
        os.chmod(WITNESS_PATH, 0o444)
        directory_fd = os.open(WITNESS_DIRECTORY, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def main() -> None:
    identity = _load_exact_json(IDENTITY_PATH)
    manifest = _load_exact_json(MODEL_MANIFEST_PATH)
    launch = _load_exact_json(LAUNCH_MANIFEST_PATH)
    _require_exact_keys(
        identity,
        {
            "schema",
            "profile_id",
            "engine_base_image_digest",
            "engine_base_image_id",
            "runtime_source_revision",
            "runtime_reported_version",
            "model_repository",
            "model_revision",
            "model_snapshot_manifest_sha256",
            "model_quantization",
            "served_model_alias",
            "launch_manifest_sha256",
            "proxy_policy_sha256",
        },
        "deployment identity",
    )
    if identity["schema"] != "friday.sglang-deployment-identity.v1":
        _fail("unexpected deployment identity version")
    if _semantic_sha256(manifest) != identity["model_snapshot_manifest_sha256"]:
        _fail("baked model manifest digest mismatch")
    if _semantic_sha256(launch) != identity["launch_manifest_sha256"]:
        _fail("baked launch manifest digest mismatch")
    _require_exact_keys(
        launch,
        {
            "schema",
            "profile_id",
            "executable",
            "model_mount_path",
            "served_model_alias",
            "arguments",
            "dynamic_argument",
            "expected_server_info",
        },
        "launch manifest",
    )
    if launch["schema"] != "friday.sglang-launch-manifest.v1":
        _fail("unexpected launch manifest version")
    if (
        launch["profile_id"] != identity["profile_id"]
        or launch["served_model_alias"] != identity["served_model_alias"]
    ):
        _fail("launch identity mismatch")
    if sys.argv[1:] != launch["arguments"]:
        _fail("runtime arguments differ from the immutable launch manifest")
    if launch["executable"] != ["sglang", "serve"]:
        _fail("runtime executable differs from the immutable launch manifest")
    dynamic = launch["dynamic_argument"]
    if dynamic != {"flag": "--random-seed", "minimum": 1, "maximum": 1073741823}:
        _fail("unexpected dynamic launch contract")
    if launch["expected_server_info"] != {"weight_version": "default"}:
        _fail("unexpected server-info launch contract")

    # Remove the previous start's witness before the expensive model rehash.
    # The proxy therefore cannot serve a stale lease while this engine boots.
    _prepare_witness_directory()
    model_root = Path(launch["model_mount_path"])
    _verify_model_snapshot(manifest, model_root)
    engine_image_id = _required_image_id("FRIDAY_EXPECTED_ENGINE_IMAGE_ID")
    proxy_image_id = _required_image_id("FRIDAY_EXPECTED_PROXY_IMAGE_ID")
    random_seed = secrets.randbelow(dynamic["maximum"] - dynamic["minimum"] + 1) + dynamic["minimum"]
    nonce = secrets.token_hex(32)
    witness = {
        "schema": "friday.sglang-deployment-witness.v1",
        "profile_id": identity["profile_id"],
        "engine_start_nonce": nonce,
        "engine_random_seed": random_seed,
        "engine_image_id": engine_image_id,
        "engine_base_image_digest": identity["engine_base_image_digest"],
        "engine_base_image_id": identity["engine_base_image_id"],
        "runtime_source_revision": identity["runtime_source_revision"],
        "runtime_reported_version": identity["runtime_reported_version"],
        "model_repository": identity["model_repository"],
        "model_revision": identity["model_revision"],
        "model_snapshot_manifest_sha256": identity["model_snapshot_manifest_sha256"],
        "model_quantization": identity["model_quantization"],
        "served_model_alias": identity["served_model_alias"],
        "launch_manifest_sha256": identity["launch_manifest_sha256"],
        "proxy_image_id": proxy_image_id,
        "proxy_policy_sha256": identity["proxy_policy_sha256"],
    }
    _write_witness(witness)
    os.execvp("sglang", ["sglang", "serve", *sys.argv[1:], "--random-seed", str(random_seed)])


if __name__ == "__main__":
    main()
