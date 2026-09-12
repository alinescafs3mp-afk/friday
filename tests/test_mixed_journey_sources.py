from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from test_v12_file_evidence_reader import _actor, _register

from friday.file_evidence import (
    current_turn_file_reference_for_tenant,
    stamp_current_turn_file_reference_for_tenant,
)
from friday.file_evidence_reader import (
    FileEvidenceUnavailable,
    reauthorize_prepared_file_evidence_in_transaction,
)
from friday.organs.mixed_journey.sources import prepare_mixed_file_archive_sources
from friday.permissions import AuthorizationService


class _Carrier(dict[str, Any]):
    pass


def _current(storage, settings) -> tuple[Any, _Carrier]:
    raw, _reference = _register(storage, settings, filename="current.txt", text="CURRENT TERMS")
    row = storage.execute("SELECT * FROM raw_objects WHERE id=?", (raw.id,)).fetchone()
    assert row is not None
    carrier = _Carrier(
        raw_object_id=str(raw.id),
        filename="current.txt",
        mime_type="text/plain",
        persisted=True,
        current_turn_only=True,
    )
    stamp_current_turn_file_reference_for_tenant(carrier, dict(row), tenant_id="alice")
    assert current_turn_file_reference_for_tenant(carrier, tenant_id="alice") is not None
    return raw, carrier


def _prepare(storage, settings, carrier, selector):
    return prepare_mixed_file_archive_sources(
        storage=storage,
        authorization=AuthorizationService(storage),
        files_root=settings.files_dir,
        actor=_actor(),
        message=f"Сравни этот файл с архивом {selector} и текущими публичными правилами в интернете.",
        attachments=[carrier],
        max_bytes=settings.max_upload_bytes,
        absolute_deadline=time.monotonic() + 5,
    )


@pytest.mark.parametrize(
    "selector_template", ["archive.txt", "{raw_id}", "@archive.txt", "archive.txt!", "{raw_id}."]
)
def test_real_current_and_archive_sources_keep_separate_authorized_snapshots(
    storage,
    settings,
    selector_template: str,
) -> None:
    current, carrier = _current(storage, settings)
    archive, _ = _register(storage, settings, filename="archive.txt", text="ARCHIVE TERMS")
    selector = selector_template.format(raw_id=archive.id)
    prepared_current, prepared_archive = _prepare(storage, settings, carrier, selector)
    assert prepared_current.raw_ids == (current.id,)
    assert prepared_archive.raw_ids == (archive.id,)
    assert prepared_current.bundle.parts[0].text == "CURRENT TERMS"
    assert prepared_archive.bundle.parts[0].text == "ARCHIVE TERMS"
    assert prepared_current.historical_selection is None
    assert prepared_archive.historical_selection is not None
    with storage.transaction() as conn:
        for prepared in (prepared_current, prepared_archive):
            assert reauthorize_prepared_file_evidence_in_transaction(
                conn,
                AuthorizationService(storage),
                settings.files_dir,
                _actor(),
                prepared,
                max_bytes=settings.max_upload_bytes,
                storage=storage,
            )


def test_exact_revision_remains_selectable_when_archive_filename_is_ambiguous(storage, settings) -> None:
    _, carrier = _current(storage, settings)
    older, _ = _register(storage, settings, filename="archive.txt", text="OLDER TERMS")
    _register(storage, settings, filename="archive.txt", text="NEWER TERMS")
    with pytest.raises(FileEvidenceUnavailable, match="not_unique"):
        _prepare(storage, settings, carrier, "archive.txt")
    _, prepared_archive = _prepare(storage, settings, carrier, older.id)
    assert prepared_archive.raw_ids == (older.id,)
    assert prepared_archive.bundle.parts[0].text == "OLDER TERMS"


def test_source_preparation_does_not_advance_relation_history_clock(storage, settings) -> None:
    _, carrier = _current(storage, settings)
    _register(storage, settings, filename="archive.txt", text="ARCHIVE TERMS")
    before = tuple(storage.execute("SELECT * FROM relation_revision_context").fetchone())
    busy_timeout = storage.execute("PRAGMA busy_timeout").fetchone()[0]
    _prepare(storage, settings, carrier, "archive.txt")
    assert tuple(storage.execute("SELECT * FROM relation_revision_context").fetchone()) == before
    assert storage.execute("PRAGMA busy_timeout").fetchone()[0] == busy_timeout


def test_source_preparation_reads_committed_snapshot_without_waiting_for_writer(storage, settings) -> None:
    _, carrier = _current(storage, settings)
    _register(storage, settings, filename="archive.txt", text="ARCHIVE TERMS")
    started, release = threading.Event(), threading.Event()
    failures = []

    def writer() -> None:
        try:
            with storage.transaction() as conn:
                conn.execute("UPDATE users SET display_name='pending writer' WHERE id='alice'")
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test writer not released")
        except BaseException as exc:
            failures.append(exc)
            started.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert started.wait(5) and not failures
        prepared = prepare_mixed_file_archive_sources(
            storage=storage,
            authorization=AuthorizationService(storage),
            files_root=settings.files_dir,
            actor=_actor(),
            message="Сравни этот файл с архивом archive.txt и текущими публичными правилами в интернете.",
            attachments=[carrier],
            max_bytes=settings.max_upload_bytes,
            absolute_deadline=time.monotonic() + 0.5,
        )
        assert prepared[0].bundle.parts[0].text == "CURRENT TERMS"
        assert prepared[1].bundle.parts[0].text == "ARCHIVE TERMS"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not failures


def test_deadlined_source_read_rejects_uncommitted_authority_before_file_bytes(
    storage, settings, monkeypatch
) -> None:
    from friday import file_evidence_reader

    _, carrier = _current(storage, settings)
    _register(storage, settings, filename="archive.txt", text="ARCHIVE TERMS")

    def no_read(*_args, **_kwargs):
        pytest.fail("uncommitted evidence reached the file reader")

    monkeypatch.setattr(file_evidence_reader, "read_authorized_file_in_transaction", no_read)
    with storage.transaction(), pytest.raises(FileEvidenceUnavailable, match="snapshot_uncommitted"):
        _prepare(storage, settings, carrier, "archive.txt")


@pytest.mark.parametrize("nested", [False, True])
def test_deadlined_read_snapshot_rolls_back_only_its_scope_and_restores_timeout(
    storage, monkeypatch, nested
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from friday.storage import _core

    storage.ensure_user("snapshot-user")
    clock = [0.0]
    monkeypatch.setattr(_core, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    before_timeout = storage.execute("PRAGMA busy_timeout").fetchone()[0]
    context = storage.transaction() if nested else nullcontext(storage.conn)
    with context as outer:
        before_name = outer.execute("SELECT display_name FROM users WHERE id='snapshot-user'").fetchone()[0]
        if nested:
            outer.execute("UPDATE users SET display_name='outer pending' WHERE id='snapshot-user'")
            before_name = "outer pending"
        with (
            pytest.raises(TimeoutError, match="read deadline expired"),
            _core.read_only_storage_snapshot(storage, absolute_deadline=1.0) as conn,
        ):
            conn.execute(
                "UPDATE users SET display_name='accidental read-scope write' WHERE id='snapshot-user'"
            )
            clock[0] = 2.0
        assert (
            outer.execute("SELECT display_name FROM users WHERE id='snapshot-user'").fetchone()[0]
            == before_name
        )
        assert outer.in_transaction is nested
        assert outer.execute("PRAGMA busy_timeout").fetchone()[0] == before_timeout


def test_json_copy_cannot_supply_current_upload_authority(storage, settings) -> None:
    _, carrier = _current(storage, settings)
    with pytest.raises(FileEvidenceUnavailable, match="authority_missing"):
        _prepare(storage, settings, dict(carrier), "archive.txt")


def test_quoted_spaced_filename_selects_the_whole_name_and_unquoted_suffix_is_rejected(
    storage,
    settings,
) -> None:
    _, carrier = _current(storage, settings)
    exact, _ = _register(storage, settings, filename="private deal.txt", text="EXACT ARCHIVE TERMS")
    _register(storage, settings, filename="deal.txt", text="DIFFERENT ARCHIVE TERMS")
    with pytest.raises(FileEvidenceUnavailable):
        _prepare(storage, settings, carrier, "private deal.txt")
    _, archive = _prepare(storage, settings, carrier, '"private deal.txt"')
    assert archive.raw_ids == (exact.id,)
    assert archive.bundle.parts[0].text == "EXACT ARCHIVE TERMS"


def test_exact_archive_id_cannot_select_another_uploader(storage, settings) -> None:
    _, carrier = _current(storage, settings)
    foreign, _ = _register(
        storage,
        settings,
        uploaded_by="bob",
        filename="archive.txt",
        text="BOB PRIVATE TERMS",
    )
    with pytest.raises(FileEvidenceUnavailable):
        _prepare(storage, settings, carrier, foreign.id)


@pytest.mark.parametrize("revoked_lane", ["current", "archive"])
def test_each_prepared_lane_still_requires_final_source_reauthorization(
    storage,
    settings,
    revoked_lane: str,
) -> None:
    current, carrier = _current(storage, settings)
    archive, _ = _register(storage, settings, filename="archive.txt", text="ARCHIVE TERMS")
    prepared = _prepare(storage, settings, carrier, archive.id)
    selected = 0 if revoked_lane == "current" else 1
    raw_id = current.id if selected == 0 else archive.id
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET deleted_at='2026-09-07T00:00:00Z' WHERE id=?", (raw_id,))
    with storage.transaction() as conn:
        assert not reauthorize_prepared_file_evidence_in_transaction(
            conn,
            AuthorizationService(storage),
            settings.files_dir,
            _actor(),
            prepared[selected],
            max_bytes=settings.max_upload_bytes,
            storage=storage,
        )


def test_current_upload_cannot_also_be_the_archive_revision(storage, settings) -> None:
    current, carrier = _current(storage, settings)
    with pytest.raises(FileEvidenceUnavailable, match="not_distinct"):
        _prepare(storage, settings, carrier, current.id)
