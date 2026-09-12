"""Real CLI graph maintenance on private two-owner SQLite; no model calls."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sys

import pytest

from friday import cli, config
from friday import storage as storage_module
from friday.storage import FridayStorage
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject

OWN, FOREIGN = "graph-cli-owner", "graph-cli-foreign"
NAMES = ("Петров Иван Иванович", "Сидоров Пётр Никифорович", "Кузнецова Анна Сергеевна")
BODY = (
    "Приказом назначен " + NAMES[0] + ", а обязанности принял " + NAMES[1] + ". Ознакомлен " + NAMES[2] + "."
)
TABLES = (
    "raw_objects",
    "knowledge_objects",
    "knowledge_object_versions",
    "entities",
    "entity_versions",
    "knowledge_entity_links",
    "relations",
    "relation_revisions",
    "relation_candidates",
    "knowledge_conflicts",
)
EVENTS = (
    "graph.entities_backfilled",
    "graph.entities_pruned",
    "graph.relations_backfilled",
    "knowledge.exact_duplicates_resolved",
)


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
def graph_context(settings, storage, monkeypatch):
    for user in (OWN, FOREIGN):
        storage.ensure_user(user, source="test", display_name=user)
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    original, opened = storage_module.init_storage, []

    def observed(current):
        assert current == settings
        assert _locked(settings, "account-deletion") and _locked(settings, "backend")
        opened.append(True)
        return original(current)

    monkeypatch.setattr(storage_module, "init_storage", observed)
    return settings, storage, opened


def _doc(storage, user, key, text, *, title="Документ", hour=9):
    key = user + "-" + key
    when = f"2026-07-29T{hour:02d}:00:00+00:00"
    raw = RawObject(
        id="raw-" + key,
        user_id=user,
        source="test",
        source_ref=key,
        raw_content=text,
        content_type="text",
        created_at=when,
        received_at=when,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id="ko-" + key,
        user_id=user,
        raw_object_id=raw.id,
        content=text,
        title=title,
        knowledge_kind="document",
        created_at=when,
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _entity(
    storage,
    user,
    ko,
    key,
    name,
    *,
    status="accepted",
    reviewed=False,
    method="explicit_person_patronymic",
    kind=EntityType.PERSON,
):
    entity = Entity(
        id="ent-" + user + "-" + key,
        user_id=user,
        name=name,
        entity_type=kind,
        metadata_json={"created_by": "test", **({"extraction_method": method} if method else {})},
    )
    storage.create_entity(entity)
    storage.link_knowledge_entity(
        user, ko, entity.id, status=status, confidence=0.9, reviewed_by=user if reviewed else None
    )
    return entity.id


def _snapshot(settings):
    fresh = FridayStorage(settings)
    try:
        return {
            "tables": {
                name: [dict(row) for row in fresh.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()]
                for name in TABLES
            },
            "events": {name: fresh.list_events(event_type=name) for name in EVENTS},
        }
    finally:
        fresh.close()


def _run(settings, monkeypatch, capsys, *args):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *args])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    assert not _locked(settings, "account-deletion") and not _locked(settings, "backend")
    return exited.value.code, captured.out, captured.err


def _stable(before, after, changed):
    for table in TABLES:
        if table not in changed:
            assert after["tables"][table] == before["tables"][table], table


def _foreign(before, after):
    for table in TABLES:
        assert [r for r in after["tables"][table] if r.get("user_id") == FOREIGN] == [
            r for r in before["tables"][table] if r.get("user_id") == FOREIGN
        ], table


def _event(before, after, name, payload):
    assert after["events"][name][1:] == before["events"][name]
    assert len(after["events"][name]) == len(before["events"][name]) + 1
    assert after["events"][name][0]["payload"] == payload
    for other in EVENTS:
        if other != name:
            assert after["events"][other] == before["events"][other]


@pytest.mark.parametrize("mode", ["show", "apply"])
def test_entity_cli_keeps_human_rejection_and_foreign_rows_and_reports_actual_changes(
    graph_context, monkeypatch, capsys, mode
):
    settings, storage, _opened = graph_context
    own = _doc(storage, OWN, "people", BODY)
    _doc(storage, OWN, "blank", "Нейтральная запись без имён.")
    foreign = _doc(storage, FOREIGN, "people", BODY)
    rejected = _entity(storage, OWN, own, "rejected", NAMES[0], status="rejected", reviewed=True)
    _entity(storage, FOREIGN, foreign, "kept", NAMES[0])
    storage.close()
    before = _snapshot(settings)
    args = ["backfill-entities", "--user", OWN, "--method", "explicit_person_patronymic", "--batch", "1"] + (
        ["--apply"] if mode == "apply" else []
    )
    code, output, error = _run(settings, monkeypatch, capsys, *args)
    assert code == 0 and error == ""
    assert "Просмотрено объектов: 2; пар документ-имя: 3; различных имён: 3." in output
    after = _snapshot(settings)
    if mode == "show":
        assert after == before and "Это ПОКАЗ" in output
        return
    _foreign(before, after)
    _stable(before, after, {"entities", "entity_versions", "knowledge_entity_links", "knowledge_objects"})
    own_entities = [r for r in after["tables"]["entities"] if r["user_id"] == OWN]
    assert {r["name"] for r in own_entities} == set(NAMES)
    assert len(own_entities) == 3
    links = [r for r in after["tables"]["knowledge_entity_links"] if r["user_id"] == OWN]
    assert len(links) == 3 and sum(r["status"] == "accepted" for r in links) == 2
    # The first accepted link also fills the legacy primary pointer; it must
    # reference that accepted pair and change no other document material.
    primary = next(r for r in links if r["status"] == "accepted")
    for old, new in zip(
        before["tables"]["knowledge_objects"], after["tables"]["knowledge_objects"], strict=True
    ):
        if old["id"] != own:
            assert new == old
            continue
        assert not old["entity_id"] and new["entity_id"] == primary["entity_id"]
        assert new["entity_id"] != rejected
        assert new["updated_at"] == primary["created_at"]
        assert {k: v for k, v in new.items() if k not in {"entity_id", "updated_at"}} == {
            k: v for k, v in old.items() if k not in {"entity_id", "updated_at"}
        }
    assert [r for r in links if r["entity_id"] == rejected] == [
        r for r in before["tables"]["knowledge_entity_links"] if r["entity_id"] == rejected
    ]
    assert "Создано сущностей: 2; связей знание-человек: 2; обойдено решённых человеком: 1." in output
    _event(
        before,
        after,
        EVENTS[0],
        {
            "method": "explicit_person_patronymic",
            "scanned": 2,
            "mentions": 3,
            "distinct_names": 3,
            "entities_created": 2,
            "links": 2,
            "skipped_decided": 1,
        },
    )


def test_entity_cli_rejects_non_declaring_method_before_opening_storage(graph_context, monkeypatch, capsys):
    settings, storage, opened = graph_context
    _doc(storage, OWN, "people", BODY)
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        settings, monkeypatch, capsys, "backfill-entities", "--method", "capitalized_person_name", "--apply"
    )
    assert code == 2 and output == "" and "не объявляющий" in error
    assert opened == [] and _snapshot(settings) == before


def test_entity_cli_limit_bounds_actual_writes_in_a_larger_batch(graph_context, monkeypatch, capsys):
    settings, storage, _opened = graph_context
    for i, name in enumerate(NAMES):
        _doc(storage, OWN, f"limit{i}", "Приказом назначен " + name + ".")
    _doc(storage, FOREIGN, "limit-foreign", BODY)
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        "backfill-entities",
        "--user",
        OWN,
        "--method",
        "explicit_person_patronymic",
        "--batch",
        "200",
        "--limit",
        "1",
        "--apply",
    )
    after = _snapshot(settings)
    assert [r["name"] for r in after["tables"]["entities"] if r["user_id"] == OWN] == [NAMES[0]], (
        "--limit1 must bound actual entity creation"
    )
    assert code == 0 and error == "" and "Просмотрено объектов: 1;" in output
    _foreign(before, after)


@pytest.mark.parametrize("mode", ["show", "apply"])
def test_prune_cli_only_tombstones_stale_unreviewed_owner_node(graph_context, monkeypatch, capsys, mode):
    settings, storage, _opened = graph_context
    own = _doc(storage, OWN, "prune", "Приказом назначен Петров Иван Иванович.")
    _doc(storage, OWN, "permutation", "Списком идёт ХАСАНОВ Руслан Рашитович, санитар-стрелок.")
    foreign = _doc(storage, FOREIGN, "prune", "Нейтральная запись.")
    doomed = _entity(storage, OWN, own, "stale", "Уважаемая Вера Андреевна")
    _entity(storage, OWN, own, "live", NAMES[0])
    _entity(storage, OWN, own, "reviewed", "Особый Знакомый Иванович", reviewed=True)
    _entity(storage, OWN, own, "human", "Тайный Знакомый Иванович", method=None)
    _entity(storage, OWN, own, "permuted", "Руслан Рашитович Хасанов")
    _entity(storage, FOREIGN, foreign, "stale", "Уважаемая Вера Андреевна")
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        "prune-entities",
        "--user",
        OWN,
        "--batch",
        "1",
        *(["--apply"] if mode == "apply" else []),
    )
    assert code == 0 and error == ""
    for fragment in (
        "Просмотрено объектов: 2;",
        "Узлов под снос: 1;",
        "оставлено не рождённых правилом: 1;",
        "оставлено решённых человеком: 1;",
        "оставлено перестановок живого имени: 1.",
    ):
        assert fragment in output
    after = _snapshot(settings)
    if mode == "show":
        assert after == before and "Это ПОКАЗ" in output
        return
    _foreign(before, after)
    _stable(before, after, {"entities", "entity_versions"})
    old = {r["id"]: r for r in before["tables"]["entities"]}
    new = {r["id"]: r for r in after["tables"]["entities"]}
    assert set(old) == set(new) and [k for k in old if old[k] != new[k]] == [doomed]
    assert new[doomed]["deleted_at"] and new[doomed]["canonical"] == 0
    assert new[doomed]["version"] == old[doomed]["version"] + 1
    versions = after["tables"]["entity_versions"]
    assert versions[:-1] == before["tables"]["entity_versions"]
    assert len(versions) == len(before["tables"]["entity_versions"]) + 1
    assert versions[-1]["entity_id"] == doomed
    _event(
        before,
        after,
        EVENTS[1],
        {"scanned": 2, "removed": 1, "names": ["Уважаемая Вера Андреевна"], "user_ids": [OWN]},
    )


def test_prune_cli_refuses_incomplete_corpus_without_any_write(graph_context, monkeypatch, capsys):
    settings, storage, _opened = graph_context
    ko = _doc(storage, OWN, "first", "Нейтральная запись.")
    _doc(storage, OWN, "second", BODY)
    _entity(storage, OWN, ko, "stale", "Уважаемая Вера Андреевна")
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        "prune-entities",
        "--user",
        OWN,
        "--batch",
        "1",
        "--limit",
        "1",
        "--apply",
    )
    assert code == 2 and output == "" and "удалять по нему нельзя" in error
    assert _snapshot(settings) == before


@pytest.mark.parametrize("mode", ["show", "apply"])
def test_relations_cli_only_proposes_owner_accepted_links_and_preserves_facts(
    graph_context, monkeypatch, capsys, mode
):
    settings, storage, _opened = graph_context
    endpoints = []
    for user, key, status in (
        (OWN, "accepted", "accepted"),
        (OWN, "suggested", "suggested"),
        (FOREIGN, "accepted", "accepted"),
    ):
        ko = _doc(storage, user, key, "Сервис Атлас использует базу Полярис.")
        left = _entity(storage, user, ko, key + "-left", "Атлас", method=None, kind=EntityType.CONCEPT)
        right = _entity(
            storage, user, ko, key + "-right", "Полярис", status=status, method=None, kind=EntityType.CONCEPT
        )
        endpoints.append((ko, left, right))
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        "backfill-relations",
        "--user",
        OWN,
        "--batch",
        "1",
        *(["--apply"] if mode == "apply" else []),
    )
    assert code == 0 and error == "" and "Просмотрено объектов: 2." in output
    after = _snapshot(settings)
    if mode == "show":
        assert after == before and "Это ПОКАЗ" in output
        return
    _foreign(before, after)
    _stable(before, after, {"relation_candidates"})
    assert len(after["tables"]["relation_candidates"]) == 1
    row = after["tables"]["relation_candidates"][0]
    assert (
        row["user_id"],
        row["source_entity_id"],
        row["target_entity_id"],
        row["relation_type"],
        row["status"],
    ) == (OWN, endpoints[0][1], endpoints[0][2], "uses", "suggested")
    evidence = json.loads(row["evidence_json"])
    assert (
        evidence["knowledge_object_id"] == endpoints[0][0] and evidence["phrase"].casefold() == "использует"
    )
    assert "Срабатываний: 1; различных пар в очереди: 1; документов со связями: 1." in output
    _event(before, after, EVENTS[2], {"scanned": 2, "matches": 1, "candidates": 1, "documents": 1})


def _copies(storage, user):
    nodes = [
        _doc(storage, user, "copy" + str(i), "Одинаковый текст копий.", title=f"Копия{i}", hour=hour)
        for i, hour in enumerate((11, 9, 10))
    ]
    conflicts = [
        str(
            storage.store_knowledge_conflict(
                user,
                nodes[a],
                nodes[b],
                conflict_type="near_duplicate",
                confidence=1.0,
                evidence={"cosine": 1.0},
            )["id"]
        )
        for a, b in ((0, 1), (1, 2), (0, 2))
    ]
    return nodes, conflicts


@pytest.mark.parametrize("mode", ["show", "apply"])
def test_exact_duplicates_cli_keeps_global_components_inside_each_owner(
    graph_context, monkeypatch, capsys, mode
):
    settings, storage, _opened = graph_context
    groups = [_copies(storage, user) for user in (OWN, FOREIGN)]
    a = _doc(storage, OWN, "version-a", "Первый текст версии.", title="Версия")
    b = _doc(storage, OWN, "version-b", "Второй текст версии.", title="Версия")
    version = str(
        storage.store_knowledge_conflict(OWN, a, b, conflict_type="near_duplicate", confidence=0.99)["id"]
    )
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        settings, monkeypatch, capsys, "resolve-exact-duplicates", *(["--apply"] if mode == "apply" else [])
    )
    assert code == 0 and error == ""
    assert (
        "Конфликтов «почти-дубликат» в очереди: 7." in output
        and "точные копии (решать нечего): 6" in output
        and "версии одного имени:          1" in output
    )
    after = _snapshot(settings)
    if mode == "show":
        assert after == before and "Это ПОКАЗ" in output
        return
    _stable(before, after, {"knowledge_objects", "knowledge_object_versions", "knowledge_conflicts"})
    oldko = {r["id"]: r for r in before["tables"]["knowledge_objects"]}
    newko = {r["id"]: r for r in after["tables"]["knowledge_objects"]}
    oldcf = {r["id"]: r for r in before["tables"]["knowledge_conflicts"]}
    newcf = {r["id"]: r for r in after["tables"]["knowledge_conflicts"]}
    assert set(oldko) == set(newko) and set(oldcf) == set(newcf)
    for nodes, conflicts in groups:
        winner = nodes[1]
        assert newko[winner] == oldko[winner]
        for loser in (nodes[0], nodes[2]):
            assert (
                newko[loser]["lifecycle_stage"] == "deprecated" and newko[loser]["superseded_by_id"] == winner
            )
            assert not newko[loser]["deleted_at"] and newko[loser]["content"] == oldko[loser]["content"]
        for cf in conflicts:
            assert newcf[cf]["status"] == "resolved" and newcf[cf]["reviewed_by"] == "exact_duplicate_pass"
    assert newcf[version] == oldcf[version] and newko[a] == oldko[a] and newko[b] == oldko[b]
    assert "Закрыто как точные копии: 6." in output
    _event(before, after, EVENTS[3], {"closed": 6, "exact": 6, "versions": 1, "different": 0})
    code, output, error = _run(settings, monkeypatch, capsys, "resolve-exact-duplicates", "--apply")
    assert code == 0 and error == "" and "Закрыто как точные копии: 0." in output
    assert _snapshot(settings) == after


def test_exact_duplicates_cli_failed_second_loser_rolls_back_whole_component(
    graph_context, monkeypatch, capsys
):
    settings, storage, _opened = graph_context
    _copies(storage, OWN)
    storage.close()
    before = _snapshot(settings)
    observed_init = storage_module.init_storage

    def with_failure(current):
        real = observed_init(current)
        real.execute("CREATE TEMP TABLE synthetic_copy_counter(value INTEGER)")
        real.execute("INSERT INTO synthetic_copy_counter VALUES(0)")
        real.execute("""CREATE TEMP TRIGGER synthetic_copy_failure BEFORE UPDATE OF lifecycle_stage ON main.knowledge_objects
            WHEN NEW.lifecycle_stage='deprecated' AND OLD.lifecycle_stage<>'deprecated' BEGIN
            UPDATE synthetic_copy_counter SET value=value+1;
            SELECT CASE WHEN (SELECT value FROM synthetic_copy_counter)=2
            THEN RAISE(ABORT, 'private synthetic copy failure') END; END""")
        real.commit()
        return real

    monkeypatch.setattr(storage_module, "init_storage", with_failure)
    code, output, error = _run(settings, monkeypatch, capsys, "resolve-exact-duplicates", "--apply")
    assert code == 0 and "Закрыто как точные копии: 0." in output
    assert error.strip() == "кластер точных копий: IntegrityError"
    assert _snapshot(settings) == before
