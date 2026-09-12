"""Synthetic certificate transport checks; no TLS handshake or product admission credit."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from tools import release_1_0_native_assets as assets

# Valid certificate-only PEM framing with synthetic bytes, not a trusted X.509 certificate.
PEM = b"-----BEGIN CERTIFICATE-----\nc3ludGhldGljLWNh\n-----END CERTIFICATE-----\n"


def _source(tmp_path: Path, payload: bytes = PEM) -> Path:
    path = tmp_path / "input-ca.pem"
    path.write_bytes(payload)
    path.chmod(0o644)
    return path


def _evidence(tmp_path: Path) -> Path:
    path = tmp_path / "evidence"
    path.mkdir(mode=0o700)
    return path


def test_ca_capture_and_private_stage_preserve_exact_bytes_and_closed_identity(tmp_path):
    source = _source(tmp_path)
    evidence = _evidence(tmp_path)
    captured = assets.capture_ca(source)
    assert captured.source_path == source
    assert captured.content == PEM
    assert stat.S_IMODE(source.stat().st_mode) == 0o644
    assets.check_source(captured)
    descriptor = assets.stage_ca(captured, evidence)
    staged = evidence / "secondary-ca.pem"
    metadata = staged.stat()
    assert descriptor == {
        "schema": "friday.r10-native-ca.v1",
        "source_path_sha256": hashlib.sha256(str(source).encode()).hexdigest(),
        "sha256": hashlib.sha256(PEM).hexdigest(),
        "size_bytes": len(PEM),
        "staged_path": str(staged),
        "staged_stamp": [
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ],
    }
    assert staged.read_bytes() == source.read_bytes() == PEM
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    assert assets.verify_staged(json.loads(json.dumps(descriptor)), evidence) == staged
    with pytest.raises(assets.NativeAssetError, match="^native_ca_stage_invalid$"):
        assets.stage_ca(captured, evidence)
    assert staged.read_bytes() == PEM
    assert assets.verify_staged(descriptor, evidence) == staged


def test_relative_ca_path_is_bound_to_capture_cwd_and_full_size_limit_is_inclusive(tmp_path, monkeypatch):
    payload = PEM + b"\n" * (65_536 - len(PEM))
    source = _source(tmp_path, payload)
    evidence = _evidence(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured = assets.capture_ca(Path("input-ca.pem"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert captured.source_path == source
    assets.check_source(captured)
    descriptor = assets.stage_ca(captured, evidence)
    assert descriptor["source_path_sha256"] == hashlib.sha256(str(source).encode()).hexdigest()
    assert descriptor["size_bytes"] == 65_536
    assert assets.verify_staged(descriptor, evidence).read_bytes() == payload


@pytest.mark.parametrize("fault", ["content", "replace", "mode"])
def test_source_change_prevents_reuse_even_when_replacement_has_identical_bytes(tmp_path, fault):
    source = _source(tmp_path)
    captured = assets.capture_ca(source)
    evidence = _evidence(tmp_path)
    if fault == "content":
        source.write_bytes(PEM + b"\n")
    elif fault == "replace":
        replacement = tmp_path / "replacement.pem"
        replacement.write_bytes(PEM)
        replacement.chmod(0o644)
        replacement.replace(source)
    else:
        source.chmod(0o666)
    with pytest.raises(assets.NativeAssetError, match="^native_ca_source_changed$"):
        assets.check_source(captured)
    with pytest.raises(assets.NativeAssetError, match="^native_ca_source_changed$"):
        assets.stage_ca(captured, evidence)
    assert not (evidence / "secondary-ca.pem").exists()


@pytest.mark.parametrize("fault", ["content", "replace", "mode", "symlink", "hardlink"])
def test_stage_change_is_rejected_including_identical_byte_replacement(tmp_path, fault):
    source = _source(tmp_path)
    evidence = _evidence(tmp_path)
    descriptor = assets.stage_ca(assets.capture_ca(source), evidence)
    staged = evidence / "secondary-ca.pem"
    if fault == "content":
        staged.write_bytes(PEM + b"\n")
    elif fault == "replace":
        replacement = tmp_path / "replacement.pem"
        replacement.write_bytes(PEM)
        replacement.chmod(0o600)
        replacement.replace(staged)
    elif fault == "mode":
        staged.chmod(0o644)
    elif fault == "symlink":
        staged.unlink()
        staged.symlink_to(source)
    else:
        os.link(staged, tmp_path / "alias.pem")
    with pytest.raises(assets.NativeAssetError, match="^native_ca_stage_invalid$"):
        assets.verify_staged(descriptor, evidence)


@pytest.mark.parametrize(
    "fault",
    ["symlink", "fifo", "directory", "oversize", "empty", "writable", "private_key", "markers", "base64"],
)
def test_ca_capture_refuses_unsafe_files_and_non_certificate_payloads(tmp_path, fault):
    source = tmp_path / "input-ca.pem"
    if fault == "symlink":
        target = tmp_path / "target.pem"
        target.write_bytes(PEM)
        source.symlink_to(target)
    elif fault == "fifo":
        os.mkfifo(source, 0o600)
    elif fault == "directory":
        source.mkdir()
    else:
        payload = {
            "oversize": PEM + b"\n" * 65_536,
            "empty": b"",
            "writable": PEM,
            "private_key": PEM + b"-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n",
            "markers": b"c3ludGhldGljLWNh\n-----END CERTIFICATE-----\n",
            "base64": b"-----BEGIN CERTIFICATE-----\n!invalid!\n-----END CERTIFICATE-----\n",
        }[fault]
        source.write_bytes(payload)
        source.chmod(0o666 if fault == "writable" else 0o644)
    with pytest.raises(assets.NativeAssetError, match="^native_ca_source_invalid$"):
        assets.capture_ca(source)


@pytest.mark.parametrize("fault", ["content", "replace"])
def test_ca_capture_rejects_change_during_descriptor_read(tmp_path, monkeypatch, fault):
    source = _source(tmp_path)
    replacement = tmp_path / "replacement.pem"
    replacement.write_bytes(PEM)
    replacement.chmod(0o644)
    real_read = os.read
    injected = False

    def changed_read(descriptor, size):
        nonlocal injected
        value = real_read(descriptor, size)
        if not injected:
            injected = True
            if fault == "content":
                source.write_bytes(PEM + b"\n")
            else:
                replacement.replace(source)
        return value

    monkeypatch.setattr(assets.os, "read", changed_read)
    with pytest.raises(assets.NativeAssetError, match="^native_ca_source_invalid$"):
        assets.capture_ca(source)
    assert injected


@pytest.mark.parametrize(
    "fault",
    ["extra", "missing", "schema", "path", "relative_path", "size_bool", "stamp_bool", "stamp_type", "hash"],
)
def test_staged_descriptor_is_closed_and_bound_to_fixed_path_and_exact_bytes(tmp_path, fault):
    source = _source(tmp_path)
    evidence = _evidence(tmp_path)
    descriptor = assets.stage_ca(assets.capture_ca(source), evidence)
    altered = copy.deepcopy(descriptor)
    if fault == "extra":
        altered["extra"] = True
    elif fault == "missing":
        del altered["source_path_sha256"]
    elif fault == "schema":
        altered["schema"] = "other"
    elif fault == "path":
        altered["staged_path"] = str(source)
    elif fault == "relative_path":
        altered["staged_path"] = "secondary-ca.pem"
    elif fault == "size_bool":
        altered["size_bytes"] = True
    elif fault == "stamp_bool":
        altered["staged_stamp"][0] = True
    elif fault == "stamp_type":
        altered["staged_stamp"] = tuple(altered["staged_stamp"])
    else:
        altered["sha256"] = "0" * 64
    code = "native_ca_stage_invalid" if fault == "hash" else "native_ca_descriptor_invalid"
    with pytest.raises(assets.NativeAssetError, match=f"^{code}$"):
        assets.verify_staged(altered, evidence)
    assert assets.verify_staged(descriptor, evidence).read_bytes() == PEM


@pytest.mark.parametrize("fault", ["public", "symlink"])
def test_staging_requires_an_existing_private_owned_evidence_directory(tmp_path, fault):
    captured = assets.capture_ca(_source(tmp_path))
    directory = _evidence(tmp_path)
    if fault == "public":
        directory.chmod(0o755)
        evidence = directory
    else:
        evidence = tmp_path / "evidence-link"
        evidence.symlink_to(directory, target_is_directory=True)
    with pytest.raises(assets.NativeAssetError, match="^native_ca_stage_invalid$"):
        assets.stage_ca(captured, evidence)
    assert not (directory / "secondary-ca.pem").exists()


def test_descriptor_validation_is_pure_and_checks_path_and_stamp_values(tmp_path, monkeypatch):
    source = _source(tmp_path)
    descriptor = assets.stage_ca(assets.capture_ca(source), _evidence(tmp_path))
    invalid = []
    for key, value in (
        ("staged_path", str(tmp_path / "evidence" / ".." / "secondary-ca.pem")),
        ("staged_path", str(tmp_path / "other.pem")),
        ("source_path_sha256", "not-a-digest"),
        ("size_bytes", 0),
        ("size_bytes", 65_537),
        ("schema", True),
    ):
        altered = copy.deepcopy(descriptor)
        altered[key] = value
        invalid.append(altered)
    for index, value in ((2, stat.S_IFREG | 0o644), (5, 2), (6, len(PEM) + 1)):
        altered = copy.deepcopy(descriptor)
        altered["staged_stamp"][index] = value
        invalid.append(altered)

    def unexpected_io(*args, **kwargs):
        pytest.fail("descriptor validation attempted filesystem access")

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "lstat", unexpected_io)
        patcher.setattr(assets.os, "open", unexpected_io)
        assert assets.validate_descriptor(descriptor) is None
        for altered in invalid:
            with pytest.raises(assets.NativeAssetError, match="^native_ca_descriptor_invalid$"):
                assets.validate_descriptor(altered)
