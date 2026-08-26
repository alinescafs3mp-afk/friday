from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from friday.host_control.contracts import ContractError
from friday.host_control.service import _stage_exact_job_input

_JOB_ID = "hjob_0123456789abcdef0123456789abcdef"


def _stage(root: Path, content: bytes = b'{"name":"Ada"}') -> str:
    return _stage_exact_job_input(
        root,
        job_id=_JOB_ID,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_exact_private_input_staging_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir(mode=0o700)
    payload = b'{"name":"Ada"}'

    relative = _stage(root, payload)
    assert relative == f"input/source-{hashlib.sha256(payload).hexdigest()[:16]}.json"
    staged = root / _JOB_ID / relative
    before = staged.stat()

    assert _stage(root, payload) == relative
    after = staged.stat()
    assert staged.read_bytes() == b'{"name":"Ada"}'
    assert before.st_ino == after.st_ino
    assert staged.stat().st_mode & 0o077 == 0


def test_staging_rejects_a_symlink_or_existing_identity_drift(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir(mode=0o700)
    relative = _stage(root)
    staged = root / _JOB_ID / relative
    staged.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    staged.symlink_to(outside)

    with pytest.raises(ContractError, match="unsafe"):
        _stage(root)
    assert outside.read_text(encoding="utf-8") == "{}"


def test_staging_rejects_a_non_private_job_root(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir(mode=0o755)

    with pytest.raises(ContractError, match="unsafe ownership or permissions"):
        _stage(root)
    assert list(root.iterdir()) == []
