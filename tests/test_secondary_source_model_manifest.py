"""Fail-closed contracts for the sealed official GPT-OSS source volume."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "deploy" / "secondary-brain" / "windows-sglang" / "runtime"
sys.path.insert(0, str(RUNTIME))
source_model_manifest = importlib.import_module("source_model_manifest")
sys.path.remove(str(RUNTIME))


def _source_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Path, str]:
    module = source_model_manifest
    source_root = tmp_path / "source"
    snapshot = source_root / "snapshot"
    snapshot.mkdir(parents=True)
    contents = {
        "config.json": b'{"model_type":"gpt_oss"}\n',
        "tokenizer.json": b'{"fixture":true}\n',
    }
    files: dict[str, tuple[int, str]] = {}
    for name, raw in contents.items():
        (snapshot / name).write_bytes(raw)
        files[name] = (len(raw), hashlib.sha256(raw).hexdigest())
    total_bytes = sum(size for size, _digest in files.values())
    value = {
        "schema": module.SCHEMA,
        "status": "verified",
        "repository": module.SOURCE_REPOSITORY,
        "revision": module.SOURCE_REVISION,
        "root_only": True,
        "excluded_prefixes": module.SOURCE_EXCLUDED_PREFIXES,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": {name: {"bytes": size, "sha256": digest} for name, (size, digest) in files.items()},
    }
    raw_manifest = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    semantic = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
    (source_root / "source-manifest.json").write_bytes(raw_manifest)
    monkeypatch.setattr(module, "SOURCE_FILE_COUNT", len(files))
    monkeypatch.setattr(module, "SOURCE_TOTAL_BYTES", total_bytes)
    monkeypatch.setattr(module, "SOURCE_FILES", files)
    monkeypatch.setattr(module, "SOURCE_MANIFEST_RAW_SHA256", manifest_sha256)
    monkeypatch.setattr(
        module,
        "SOURCE_MANIFEST_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return module, source_root, manifest_sha256


def test_sealed_source_constants_are_internally_consistent() -> None:
    module = source_model_manifest
    assert len(module.SOURCE_FILES) == module.SOURCE_FILE_COUNT == 14
    assert sum(size for size, _digest in module.SOURCE_FILES.values()) == module.SOURCE_TOTAL_BYTES
    assert module.SOURCE_TOTAL_BYTES == 13_789_264_674


def test_manifest_and_full_snapshot_return_the_same_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, source_root, manifest_sha256 = _source_fixture(tmp_path, monkeypatch)
    manifest_receipt = module.verify_source_model_manifest(
        source_root / "source-manifest.json",
        manifest_sha256,
    )
    snapshot_receipt = module.verify_source_model_snapshot(source_root, manifest_sha256)
    assert snapshot_receipt == manifest_receipt
    assert snapshot_receipt.source_revision == module.SOURCE_REVISION
    assert snapshot_receipt.file_count == 2


def test_snapshot_rejects_tampering_symlinks_and_extra_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, source_root, manifest_sha256 = _source_fixture(tmp_path, monkeypatch)
    target = source_root / "snapshot" / "config.json"
    target.write_bytes(b'{"model_type":"tampered"}\n')
    with pytest.raises(module.SourceModelManifestError):
        module.verify_source_model_snapshot(source_root, manifest_sha256)

    module, source_root, manifest_sha256 = _source_fixture(tmp_path / "symlink", monkeypatch)
    target = source_root / "snapshot" / "config.json"
    target.unlink()
    target.symlink_to(source_root / "snapshot" / "tokenizer.json")
    with pytest.raises(module.SourceModelManifestError):
        module.verify_source_model_snapshot(source_root, manifest_sha256)

    module, source_root, manifest_sha256 = _source_fixture(tmp_path / "extra", monkeypatch)
    (source_root / "unexpected").write_bytes(b"no")
    with pytest.raises(module.SourceModelManifestError):
        module.verify_source_model_snapshot(source_root, manifest_sha256)


def test_manifest_rejects_wrong_expected_hash_and_duplicate_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, source_root, manifest_sha256 = _source_fixture(tmp_path, monkeypatch)
    manifest_path = source_root / "source-manifest.json"
    with pytest.raises(module.SourceModelManifestError):
        module.verify_source_model_manifest(manifest_path, "0" * 64)

    duplicate = b'{"schema":"one","schema":"two"}\n'
    duplicate_sha256 = hashlib.sha256(duplicate).hexdigest()
    manifest_path.write_bytes(duplicate)
    monkeypatch.setattr(module, "SOURCE_MANIFEST_RAW_SHA256", duplicate_sha256)
    with pytest.raises(module.SourceModelManifestError, match="duplicate key"):
        module.verify_source_model_manifest(manifest_path, duplicate_sha256)
