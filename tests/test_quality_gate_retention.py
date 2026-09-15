from __future__ import annotations

import hashlib
import json
import os
import shutil

import pytest

from tools import quality_gate as gate


@pytest.fixture
def retained_inputs(tmp_path):
    build = tmp_path / "candidate-dist"
    build.mkdir(mode=0o700)
    wheel = build / "friday-1.0-py3-none-any.whl"
    wheel.write_bytes(b"synthetic verified build bytes" * 100)
    wheel.chmod(0o600)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    descriptor = gate._open_evidence_directory(evidence)
    try:
        yield wheel, evidence, descriptor
    finally:
        os.close(descriptor)


def _retain(inputs, **overrides):
    wheel, evidence, descriptor = inputs
    arguments = {
        "wheel": wheel,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "evidence_dir": evidence,
        "evidence_fd": descriptor,
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
    }
    arguments.update(overrides)
    return gate._retain_candidate_wheel(**arguments)


def test_retained_build_survives_scratch_cleanup_without_a_gate_verdict(retained_inputs):
    wheel, evidence, descriptor = retained_inputs
    payload = wheel.read_bytes()
    retained = _retain(retained_inputs)
    shutil.rmtree(wheel.parent)
    gate._require_retained_wheel(evidence, descriptor, retained)
    assert (evidence / retained["filename"]).read_bytes() == payload
    receipt = json.loads((evidence / gate._RETAINED_WHEEL_RECEIPT).read_bytes())
    assert receipt == {
        "schema": "friday.quality-gate-wheel.v1",
        "status": "retained_without_gate_verdict",
        "certification_eligible": False,
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
        "wheel": {
            "filename": retained["filename"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        },
    }
    assert not (evidence / "quality-gate-summary.json").exists()


@pytest.mark.parametrize("mutation", ("symlink", "hardlink", "fifo", "public", "oversized", "empty"))
def test_unsafe_source_is_refused_before_publishing_a_receipt(retained_inputs, mutation):
    wheel, evidence, descriptor = retained_inputs
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if mutation in {"symlink", "hardlink", "fifo"}:
        other = wheel.with_suffix(".original")
        wheel.rename(other)
        if mutation == "symlink":
            wheel.symlink_to(other)
        elif mutation == "hardlink":
            os.link(other, wheel)
        else:
            os.mkfifo(wheel, 0o600)
    elif mutation == "public":
        wheel.chmod(0o644)
    else:
        with wheel.open("wb") as stream:
            stream.truncate(gate._MAX_RETAINED_WHEEL_BYTES + 1 if mutation == "oversized" else 0)
    with pytest.raises((OSError, RuntimeError)):
        gate._retain_candidate_wheel(
            wheel, digest, evidence, descriptor, candidate_sha="a" * 40, candidate_tree="b" * 40
        )
    assert list(evidence.iterdir()) == []


@pytest.mark.parametrize("field", ("wheel_sha256", "candidate_sha", "candidate_tree"))
def test_retention_identity_is_checked_before_output(retained_inputs, field):
    _wheel, evidence, _descriptor = retained_inputs
    with pytest.raises(RuntimeError, match="identity"):
        _retain(retained_inputs, **{field: "invalid"})
    assert list(evidence.iterdir()) == []


def test_mismatched_verified_digest_cannot_publish_a_receipt(retained_inputs):
    _wheel, evidence, _descriptor = retained_inputs
    with pytest.raises(RuntimeError, match="changed during retention"):
        _retain(retained_inputs, wheel_sha256="0" * 64)
    assert not (evidence / gate._RETAINED_WHEEL_RECEIPT).exists()


def test_existing_evidence_is_not_overwritten(retained_inputs):
    wheel, evidence, _descriptor = retained_inputs
    sentinel = evidence / wheel.name
    sentinel.write_bytes(b"previous evidence")
    with pytest.raises(RuntimeError, match="contents changed"):
        _retain(retained_inputs)
    assert sentinel.read_bytes() == b"previous evidence"


@pytest.mark.parametrize("name", ("wheel", "receipt"))
def test_modified_retained_bytes_refuse_terminal_publication(retained_inputs, name):
    _wheel, evidence, descriptor = retained_inputs
    retained = _retain(retained_inputs)
    target = evidence / (retained["filename"] if name == "wheel" else gate._RETAINED_WHEEL_RECEIPT)
    original = target.read_bytes()
    target.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(RuntimeError, match="digest changed"):
        gate._require_retained_wheel(evidence, descriptor, retained)


@pytest.mark.parametrize("mutation", ("symlink", "hardlink", "fifo", "public", "missing", "extra"))
def test_retained_namespace_substitution_is_refused(retained_inputs, mutation):
    _wheel, evidence, descriptor = retained_inputs
    retained = _retain(retained_inputs)
    target = evidence / retained["filename"]
    if mutation in {"symlink", "hardlink", "fifo", "missing"}:
        original = evidence.parent / "original-wheel"
        target.rename(original)
        if mutation == "symlink":
            target.symlink_to(original)
        elif mutation == "hardlink":
            os.link(original, target)
        elif mutation == "fifo":
            os.mkfifo(target, 0o600)
    elif mutation == "public":
        target.chmod(0o644)
    else:
        (evidence / "injected").touch()
    with pytest.raises((OSError, RuntimeError)):
        gate._require_retained_wheel(evidence, descriptor, retained)


def test_replaced_directory_cannot_receive_or_validate_retention(retained_inputs):
    _wheel, evidence, descriptor = retained_inputs
    retained = _retain(retained_inputs)
    original = evidence.with_name("original-evidence")
    evidence.rename(original)
    evidence.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="identity"):
        gate._require_retained_wheel(evidence, descriptor, retained)
    assert list(evidence.iterdir()) == []
    assert (original / retained["filename"]).is_file()


def test_source_replacement_during_copy_does_not_publish_receipt(retained_inputs, monkeypatch):
    wheel, evidence, _descriptor = retained_inputs
    original_read = os.read
    replaced = False

    def replace_on_read(descriptor, size):
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if chunk and not replaced:
            replaced = True
            substitute = wheel.with_suffix(".replacement")
            substitute.write_bytes(wheel.read_bytes())
            substitute.chmod(0o600)
            substitute.replace(wheel)
        return chunk

    monkeypatch.setattr(gate.os, "read", replace_on_read)
    with pytest.raises(RuntimeError, match="changed during retention"):
        _retain(retained_inputs)
    assert replaced
    assert not (evidence / gate._RETAINED_WHEEL_RECEIPT).exists()


def test_copy_fsync_failure_cannot_publish_receipt(retained_inputs, monkeypatch):
    _wheel, evidence, _descriptor = retained_inputs

    def fail_fsync(_descriptor):
        raise OSError("injected disk failure")

    monkeypatch.setattr(gate.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="disk failure"):
        _retain(retained_inputs)
    assert not (evidence / gate._RETAINED_WHEEL_RECEIPT).exists()


def test_summary_namespace_is_exact_and_retention_remains_noncertifying(retained_inputs):
    _wheel, evidence, descriptor = retained_inputs
    retained = _retain(retained_inputs)
    gate._write_private_json(descriptor, "quality-gate-summary.json", {"test_only": True})
    gate._require_retained_wheel(evidence, descriptor, retained, summary_written=True)
    with pytest.raises(RuntimeError, match="contents changed"):
        gate._require_retained_wheel(evidence, descriptor, retained)
    receipt = json.loads((evidence / gate._RETAINED_WHEEL_RECEIPT).read_bytes())
    assert receipt["certification_eligible"] is False


def test_tiers_without_a_wheel_keep_the_original_evidence_namespace(tmp_path):
    tmp_path.chmod(0o700)
    descriptor = gate._open_evidence_directory(tmp_path)
    try:
        gate._require_retained_wheel(tmp_path, descriptor, None)
        gate._write_private_json(descriptor, "quality-gate-summary.json", {"test_only": True})
        gate._require_retained_wheel(tmp_path, descriptor, None, summary_written=True)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("summary_written", (False, True))
@pytest.mark.parametrize("mutation", ("bytes", "replace", "fifo", "symlink", "hardlink"))
def test_wheel_cannot_change_while_receipt_is_authenticated(
    retained_inputs, monkeypatch, summary_written, mutation
):
    _wheel, evidence, descriptor = retained_inputs
    retained = _retain(retained_inputs)
    target = evidence / retained["filename"]
    if summary_written:
        gate._write_private_json(descriptor, "quality-gate-summary.json", {"test_only": True})
    original_open = os.open
    injected = False

    def replace_before_receipt_open(path, flags, *args, **kwargs):
        nonlocal injected
        if path == gate._RETAINED_WHEEL_RECEIPT and not injected:
            injected = True
            if mutation == "bytes":
                payload = target.read_bytes()
                target.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
            else:
                other = evidence.parent / "displaced-wheel"
                target.rename(other)
                if mutation == "replace":
                    target.write_bytes(other.read_bytes())
                    target.chmod(0o600)
                elif mutation == "fifo":
                    os.mkfifo(target, 0o600)
                elif mutation == "symlink":
                    target.symlink_to(other)
                else:
                    os.link(other, target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(gate.os, "open", replace_before_receipt_open)
    with pytest.raises(RuntimeError, match="changed during pair authentication"):
        gate._require_retained_wheel(evidence, descriptor, retained, summary_written=summary_written)
    assert injected
