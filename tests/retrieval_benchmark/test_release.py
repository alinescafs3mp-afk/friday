from __future__ import annotations

import os
from pathlib import Path

import pytest

import friday.retrieval_benchmark.release as release_module
from friday.retrieval_benchmark.release import RecallReleaseIdentityError


def test_release_source_read_rejects_parent_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "live"
    alternate_parent = tmp_path / "alternate-parent"
    held = tmp_path / "held"
    live.mkdir()
    alternate_parent.mkdir()
    source = live / "source.py"
    alternate_source = alternate_parent / source.name
    source.write_bytes(b"expected source\n")
    alternate_source.write_bytes(b"attacker source\n")
    real_open = release_module.os.open

    def substitute_parent_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        if Path(path) != source:
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        live.rename(held)
        alternate_parent.rename(live)
        try:
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        finally:
            live.rename(alternate_parent)
            held.rename(live)

    monkeypatch.setattr(release_module.os, "open", substitute_parent_before_open)
    with pytest.raises(RecallReleaseIdentityError):
        release_module._stable_source_bytes(source)
    assert source.read_bytes() == b"expected source\n"
    assert alternate_source.read_bytes() == b"attacker source\n"
