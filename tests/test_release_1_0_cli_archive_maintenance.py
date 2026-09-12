"""Real CLI archive writes, tenant boundaries and counters on private SQLite.

Embedding tests mark existing vectors only; they do not run a model/indexer.
Office fixtures exercise stored metadata extraction, not the upload pipeline.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import sqlite3
import sys
import zipfile

import pytest

from friday import cli, config
from friday import storage as storage_module
from friday.dedup import pack_vector
from friday.storage import FridayStorage
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, Relation, RelationType

OWN = "archive-cli-owner"
FOREIGN = "archive-cli-foreign"
TABLES = (
    "raw_objects",
    "knowledge_objects",
    "knowledge_object_versions",
    "knowledge_embeddings",
    "knowledge_chunk_embeddings",
    "relations",
    "relation_revisions",
)
EVENTS = ("embeddings.reindex_requested", "documents.dates_backfilled", "graph.relation_dates_backfilled")


def _locked(settings, name):
    path = settings.state_dir / f"{name}.lock"
    if not path.exists():
        return False
    with path.open("r+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


@pytest.fixture
def archive_context(settings, storage, monkeypatch):
    for user in (OWN, FOREIGN):
        storage.ensure_user(user, source="test", display_name=user)
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    original = storage_module.init_storage
    opened = []

    def observed(current):
        assert current == settings
        assert _locked(settings, "account-deletion") and _locked(settings, "backend")
        opened.append(True)
        return original(current)

    monkeypatch.setattr(storage_module, "init_storage", observed)
    return settings, storage, opened


def _document(storage, user, key, *, metadata=None, file=None):
    key = user + "-" + key
    text = "Synthetic archive text " + key
    raw = RawObject(
        id="raw-" + key,
        user_id=user,
        source="test",
        source_ref=key,
        raw_content=text,
        content_type="file" if file else "text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        metadata_json={"stored_path": file} if file else {},
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id="ko-" + key,
        user_id=user,
        raw_object_id=raw.id,
        content=text,
        title=key,
        content_type=raw.content_type,
        metadata_json=metadata or {},
    )
    storage.store_knowledge_object(ko)
    return ko


def _snapshot(settings):
    fresh = FridayStorage(settings)
    try:
        return {
            "tables": {
                name: [dict(row) for row in fresh.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()]
                for name in TABLES
            },
            "events": {name: fresh.list_events(event_type=name) for name in EVENTS},
            "files": {
                str(p.relative_to(settings.files_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in settings.files_dir.rglob("*")
                if p.is_file()
            },
        }
    finally:
        fresh.close()


def _run(monkeypatch, capsys, *args):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *args])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


def _event(before, after, name, payload):
    assert after["events"][name][1:] == before["events"][name]
    assert len(after["events"][name]) == len(before["events"][name]) + 1
    assert after["events"][name][0]["payload"] == payload
    for other in EVENTS:
        if other != name:
            assert after["events"][other] == before["events"][other]


def _unchanged(before, after, except_tables):
    assert after["files"] == before["files"]
    for name in TABLES:
        if name not in except_tables:
            assert after["tables"][name] == before["tables"][name]


def _vectors(storage):
    for user in (OWN, FOREIGN):
        obj = _document(storage, user, "vector")
        entry = {
            "knowledge_object_id": obj.id,
            "user_id": user,
            "model": "synthetic-embed",
            "dim": 2,
            "source_version": 1,
            "content_hash": hashlib.sha256(obj.content.encode()).hexdigest(),
            "vector": pack_vector([0.3, 0.7]),
        }
        chunk = {
            **entry,
            "chunk_scheme": "v2",
            "chunk_index": 0,
            "start_char": 0,
            "end_char": len(obj.content),
        }
        storage.upsert_knowledge_vectors([entry], {obj.id: [chunk]})


@pytest.mark.parametrize("scope", ["owner", "all"])
def test_cli_reindex_preserves_object_and_chunk_vectors_and_marks_exact_tenants(
    archive_context, monkeypatch, capsys, scope
):
    settings, storage, opened = archive_context
    _vectors(storage)
    storage.close()
    before = _snapshot(settings)
    args = ["--user", OWN] if scope == "owner" else []
    code, output, error = _run(monkeypatch, capsys, "reindex-embeddings", *args, "--yes")
    count = 1 if scope == "owner" else 2
    label = f"арендатора {OWN}" if scope == "owner" else "всех арендаторов"
    assert code == 0 and error == "" and opened == [True]
    assert (
        output
        == f"Помечено устаревшими векторов {label}: {count}.\nПересчёт идёт фоновым индексатором; следите за `jericho status` или журналом.\n"
    )
    after = _snapshot(settings)
    changed = {"knowledge_embeddings", "knowledge_chunk_embeddings"}
    _unchanged(before, after, changed)
    for table in changed:
        expected = [
            {**row, "source_version": -1, "content_hash": ""}
            if scope == "all" or row["user_id"] == OWN
            else row
            for row in before["tables"][table]
        ]
        assert after["tables"][table] == expected
    _event(before, after, EVENTS[0], {"marked": count, "user_id": OWN if scope == "owner" else "*"})


def test_cli_reindex_without_confirmation_opens_no_storage_and_changes_nothing(
    archive_context, monkeypatch, capsys
):
    settings, storage, opened = archive_context
    _vectors(storage)
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(monkeypatch, capsys, "reindex-embeddings", "--user", OWN)
    assert code == 2 and output == "" and "Повторите с --yes." in error and opened == []
    assert _snapshot(settings) == before


def _office(created):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        )
        if created:
            z.writestr(
                "docProps/core.xml",
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dcterms="http://purl.org/dc/terms/"><dcterms:created>'
                + created
                + "T09:00:00Z</dcterms:created></cp:coreProperties>",
            )
    return stream.getvalue()


def _file_doc(settings, storage, user, key, date, *, missing=False, existing=False):
    relative = f"{user}/{key}.docx"
    path = settings.files_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not missing:
        path.write_bytes(_office(date))
    return _document(storage, user, key, file=relative, metadata={"document_date": date} if existing else {})


def test_cli_document_dates_crosses_undated_pages_preserves_owner_and_repeats_safely(
    archive_context, monkeypatch, capsys
):
    settings, storage, _opened = archive_context
    _file_doc(settings, storage, OWN, "undated", None)
    dated = [
        _file_doc(settings, storage, OWN, "dated1", "2020-01-02"),
        _file_doc(settings, storage, OWN, "dated2", "2021-03-04"),
    ]
    _file_doc(settings, storage, OWN, "missing", "2018-01-01", missing=True)
    _file_doc(settings, storage, OWN, "already", "2017-01-01", existing=True)
    _file_doc(settings, storage, FOREIGN, "foreign", "2015-01-01")
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(monkeypatch, capsys, "backfill-document-dates", "--user", OWN, "--batch", "1")
    assert code == 0 and error == ""
    assert output == "Просмотрено объектов: 4; проставлено дат: 2; файлов не найдено: 1.\n"
    after = _snapshot(settings)
    _unchanged(before, after, {"knowledge_objects"})
    wanted = {dated[0].id: "2020-01-02", dated[1].id: "2021-03-04"}
    for old, new in zip(
        before["tables"]["knowledge_objects"], after["tables"]["knowledge_objects"], strict=True
    ):
        if old["id"] not in wanted:
            assert new == old
        else:
            assert {k: v for k, v in new.items() if k != "metadata_json"} == {
                k: v for k, v in old.items() if k != "metadata_json"
            }
            assert json.loads(new["metadata_json"]) == {
                **json.loads(old["metadata_json"]),
                "document_date": wanted[old["id"]],
            }
    _event(before, after, EVENTS[1], {"scanned": 4, "dated": 2, "files_missing": 1})
    code, output, error = _run(monkeypatch, capsys, "backfill-document-dates", "--user", OWN, "--batch", "1")
    again = _snapshot(settings)
    assert (
        code == 0
        and error == ""
        and "Просмотрено объектов: 2; проставлено дат: 0; файлов не найдено: 1." in output
    )
    assert "это не ошибка, а отсутствие данных" in output
    _unchanged(after, again, set())
    _event(after, again, EVENTS[1], {"scanned": 2, "dated": 0, "files_missing": 1})


def test_cli_document_dates_limit_bounds_actual_writes_inside_a_larger_batch(
    archive_context, monkeypatch, capsys
):
    settings, storage, _opened = archive_context
    own = [_file_doc(settings, storage, OWN, f"limit{i}", "2020-01-02") for i in range(3)]
    _file_doc(settings, storage, FOREIGN, "limit-foreign", "2015-01-01")
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        monkeypatch, capsys, "backfill-document-dates", "--user", OWN, "--batch", "200", "--limit", "1"
    )
    after = _snapshot(settings)
    changed = [
        new["id"]
        for old, new in zip(
            before["tables"]["knowledge_objects"], after["tables"]["knowledge_objects"], strict=True
        )
        if old != new
    ]
    assert changed == [own[0].id], "--limit 1 must not update the rest of the fetched batch"
    assert (
        code == 0
        and error == ""
        and output == "Просмотрено объектов: 1; проставлено дат: 1; файлов не найдено: 0.\n"
    )
    _unchanged(before, after, {"knowledge_objects"})
    _event(before, after, EVENTS[1], {"scanned": 1, "dated": 1, "files_missing": 0})


def _relation(storage, user, key, *, date=None, no_source=False, valid_from=""):
    source = Entity("ent-" + user + key + "a", user, key + " person", EntityType.PERSON)
    target = Entity("ent-" + user + key + "b", user, key + " unit", EntityType.ORGANIZATION)
    storage.create_entity(source)
    storage.create_entity(target)
    doc = _document(storage, user, "relation-" + key, metadata={"document_date": date} if date else {})
    rel = Relation(
        "rel-" + user + key,
        user,
        source.id,
        target.id,
        RelationType.MEMBER_OF,
        valid_from=valid_from,
        metadata_json={} if no_source else {"evidence": {"knowledge_object_id": doc.id}},
    )
    storage.create_relation(rel)
    return rel.id


def _relation_fixture(storage):
    wanted = {
        _relation(storage, OWN, "dated1", date="2020-01-02"): "2020-01-02",
        _relation(storage, OWN, "dated2", date="2021-03-04"): "2021-03-04",
    }
    _relation(storage, OWN, "no-source", no_source=True)
    _relation(storage, OWN, "no-date")
    _relation(storage, OWN, "already", date="2016-01-01", valid_from="2017-02-03")
    _relation(storage, FOREIGN, "foreign", date="2015-01-01")
    return wanted


@pytest.mark.parametrize("apply", [False, True], ids=["show", "apply"])
def test_cli_relation_dates_preserves_foreign_and_existing_dates_and_batches_history(
    archive_context, monkeypatch, capsys, apply
):
    settings, storage, _opened = archive_context
    wanted = _relation_fixture(storage)
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        monkeypatch, capsys, "backfill-relation-dates", "--user", OWN, *(["--apply"] if apply else [])
    )
    expected = "Связей без начала: 4.\n  дата документа найдена: 2\n  документ-основание не назван или удалён: 1\n  у документа нет своей даты: 1\n"
    if not apply:
        expected += "Это ПОКАЗ, база не тронута. Чтобы применить, повторите с --apply.\n"
    assert code == 0 and output == expected and error == ""
    after = _snapshot(settings)
    if not apply:
        assert after == before
        return
    _unchanged(before, after, {"relations", "relation_revisions"})
    assert after["tables"]["relations"] == [
        {**row, "valid_from": wanted[row["id"]]} if row["id"] in wanted else row
        for row in before["tables"]["relations"]
    ]
    prior = before["tables"]["relation_revisions"]
    assert after["tables"]["relation_revisions"][: len(prior)] == prior
    added = after["tables"]["relation_revisions"][len(prior) :]
    assert len(added) == 2 and {row["relation_id"] for row in added} == set(wanted)
    assert len({row["batch_id"] for row in added}) == len({row["recorded_at"] for row in added}) == 1
    assert added[0]["batch_id"].startswith("relation_batch_")
    _event(before, after, EVENTS[2], {"seen": 4, "dated": 2})


def test_cli_relation_dates_injected_second_write_failure_rolls_back_rows_history_and_event(
    archive_context, monkeypatch, capsys
):
    settings, storage, _opened = archive_context
    _relation_fixture(storage)
    storage.close()
    before = _snapshot(settings)
    observed_init = storage_module.init_storage

    def with_transient_failure(current):
        real = observed_init(current)
        # Inject only after the normal schema check. TEMP objects belong to this
        # connection and disappear when the real handler closes it.
        real.execute("CREATE TEMP TABLE synthetic_date_counter(value INTEGER NOT NULL)")
        real.execute("INSERT INTO synthetic_date_counter VALUES(0)")
        real.execute("""CREATE TEMP TRIGGER synthetic_date_failure
            BEFORE UPDATE OF valid_from ON main.relations
            WHEN OLD.valid_from='' AND NEW.valid_from<>'' BEGIN
            UPDATE synthetic_date_counter SET value=value+1;
            SELECT CASE WHEN (SELECT value FROM synthetic_date_counter)=2
            THEN RAISE(ABORT, 'synthetic second date write failure') END; END""")
        real.commit()
        return real

    monkeypatch.setattr(storage_module, "init_storage", with_transient_failure)
    monkeypatch.setattr(sys, "argv", ["friday", "backfill-relation-dates", "--user", OWN, "--apply"])
    capsys.readouterr()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic second date write failure"):
        cli.main()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert _snapshot(settings) == before
    assert not _locked(settings, "account-deletion") and not _locked(settings, "backend")


def test_cli_document_dates_limit_caps_second_page_and_next_run_resumes(archive_context, monkeypatch, capsys):
    settings, storage, _opened = archive_context
    own = [_file_doc(settings, storage, OWN, f"limit{i}", "2020-01-02") for i in range(5)]
    _file_doc(settings, storage, FOREIGN, "limit-foreign", "2015-01-01")
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        monkeypatch, capsys, "backfill-document-dates", "--user", OWN, "--batch", "2", "--limit", "3"
    )
    after = _snapshot(settings)
    changed = [
        new["id"]
        for old, new in zip(
            before["tables"]["knowledge_objects"], after["tables"]["knowledge_objects"], strict=True
        )
        if old != new
    ]
    assert changed == [row.id for row in own[:3]], "--limit 3 must stop within the second page"
    assert (
        code == 0
        and error == ""
        and output == "Просмотрено объектов: 3; проставлено дат: 3; файлов не найдено: 0.\n"
    )
    _unchanged(before, after, {"knowledge_objects"})
    _event(before, after, EVENTS[1], {"scanned": 3, "dated": 3, "files_missing": 0})
    code, output, error = _run(
        monkeypatch, capsys, "backfill-document-dates", "--user", OWN, "--batch", "2", "--limit", "3"
    )
    resumed = _snapshot(settings)
    changed_after_resume = [
        new["id"]
        for old, new in zip(
            after["tables"]["knowledge_objects"], resumed["tables"]["knowledge_objects"], strict=True
        )
        if old != new
    ]
    assert changed_after_resume == [row.id for row in own[3:]]
    assert code == 0 and error == ""
    assert output == "Просмотрено объектов: 2; проставлено дат: 2; файлов не найдено: 0.\n"
    _unchanged(after, resumed, {"knowledge_objects"})
    _event(after, resumed, EVENTS[1], {"scanned": 2, "dated": 2, "files_missing": 0})
