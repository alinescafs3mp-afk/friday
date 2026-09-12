"""Real retag CLI/storage; scripted model boundary is not live-model evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import stat
import sys

import pytest

from friday import cli, config
from friday import storage as storage_module
from friday.agent_runtime import llm as llm_module
from friday.ingestion._boilerplate import BOILERPLATE_KEY
from friday.storage import FridayStorage
from friday.storage.models import KnowledgeObject, RawObject

OWN, FOREIGN = "retag-owner", "retag-foreign"
TABLES = (
    "raw_objects",
    "knowledge_objects",
    "knowledge_object_versions",
    "entities",
    "knowledge_entity_links",
    "relations",
    "relation_revisions",
)
EVENT = "knowledge.retagged"
QUOTE = "Выдано Иванову для предъявления."
SECRET = "synthetic-private-model-credential-089"


def _locked(settings, role):
    p = settings.state_dir / f"{role}.lock"
    if not p.exists():
        return False
    with p.open("r+b") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


@pytest.fixture
def retag_context(settings, storage, monkeypatch):
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


def _doc(storage, user, key, text, tags, title="документ.txt"):
    key = user + "-" + key
    raw = RawObject(
        id="raw-" + key,
        user_id=user,
        source="test",
        source_ref=key,
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id="ko-" + key,
        user_id=user,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title,
        tags_json=tags,
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _snapshot(settings):
    fresh = FridayStorage(settings)
    try:
        return {
            "tables": {
                name: [dict(r) for r in fresh.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()]
                for name in TABLES
            },
            "events": fresh.list_events(event_type=EVENT),
            "boilerplate": fresh.kv_get(BOILERPLATE_KEY),
        }
    finally:
        fresh.close()


def _run(settings, monkeypatch, capsys, *args):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", "retag-documents", *args])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    assert not _locked(settings, "account-deletion") and not _locked(settings, "backend")
    return exited.value.code, captured.out, captured.err


def _protect(before, after, changed):
    for name in TABLES:
        if name not in ("knowledge_objects", "knowledge_object_versions"):
            assert after["tables"][name] == before["tables"][name], name
    old = {r["id"]: r for r in before["tables"]["knowledge_objects"]}
    new = {r["id"]: r for r in after["tables"]["knowledge_objects"]}
    assert set(old) == set(new)
    for key in old:
        if key not in changed:
            assert old[key] == new[key]
            continue
        assert old[key]["user_id"] == new[key]["user_id"] == OWN
        assert new[key]["version"] == old[key]["version"] + 1
        assert {k: v for k, v in old[key].items() if k not in {"tags_json", "updated_at", "version"}} == {
            k: v for k, v in new[key].items() if k not in {"tags_json", "updated_at", "version"}
        }
    count = len(before["tables"]["knowledge_object_versions"])
    assert (
        after["tables"]["knowledge_object_versions"][:count] == before["tables"]["knowledge_object_versions"]
    )
    added = after["tables"]["knowledge_object_versions"][count:]
    assert len(added) == len(changed)
    assert {r["knowledge_object_id"] for r in added} == set(changed)


def _event(before, after, payload):
    assert len(after["events"]) == len(before["events"]) + 1
    assert after["events"][1:] == before["events"]
    assert after["events"][0]["payload"] == payload


@pytest.mark.parametrize("mode", ["show", "apply"])
def test_retag_cli_report_matches_owned_changes_and_repeat_preserves_versions(
    retag_context, monkeypatch, capsys, mode
):
    settings, storage, _opened = retag_context
    kept = _doc(
        storage,
        OWN,
        "kept",
        "Ежедневная подача автомобилей на 15 число\n| 1 | КамАЗ | 08:00 |",
        ["вид:график", "document", "автомобилей"],
        "подача.xlsx",
    )
    replaced = _doc(
        storage,
        OWN,
        "replaced",
        "ВЕДОМОСТЬ выдачи имущества\n| 1 | Иванов | автомат |",
        ["вид:график", "application"],
        "ведомость.docx",
    )
    _doc(storage, FOREIGN, "foreign", "ВЕДОМОСТЬ выдачи имущества", ["document", "application", "чужое"])
    storage.close()
    before = _snapshot(settings)
    report = settings.home / "tag-report.jsonl"
    args = ["--user", OWN, "--batch", "1", "--report", str(report)] + (["--apply"] if mode == "apply" else [])
    code, output, error = _run(settings, monkeypatch, capsys, *args)
    assert code == 0 and error == "" and "Просмотрено объектов: 2." in output
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    assert [r["id"] for r in rows] == [kept, replaced]
    assert [(r["kind"], r["removed"], r["added"]) for r in rows] == [
        ("", ["document"], []),
        ("ведомость", ["application", "вид:график"], ["вид:ведомость"]),
    ]
    assert "ВЕДОМОСТЬ" in rows[1]["evidence"]
    after = _snapshot(settings)
    if mode == "show":
        assert after == before and "Изменилось бы объектов: 2." in output
        return
    _protect(before, after, [kept, replaced])
    tags = {r["id"]: json.loads(r["tags_json"]) for r in after["tables"]["knowledge_objects"]}
    assert tags[kept] == ["автомобилей", "вид:график"] and tags[replaced] == ["вид:ведомость"]
    assert after["boilerplate"] == before["boilerplate"]
    _event(before, after, {"seen": 2, "kinds": 2, "changed": 2})
    code, output, error = _run(settings, monkeypatch, capsys, "--user", OWN, "--apply")
    again = _snapshot(settings)
    assert code == 0 and error == "" and "Изменено объектов: 0." in output
    assert again["tables"] == after["tables"] and again["boilerplate"] == after["boilerplate"]
    _event(after, again, {"seen": 2, "kinds": 2, "changed": 0})


@pytest.mark.parametrize("answer", ["valid", "unquoted", "new-kind", "error"])
def test_retag_cli_scripted_arbiter_requires_a_grounded_known_kind(
    retag_context, monkeypatch, capsys, answer
):
    settings, storage, _opened = retag_context
    own = _doc(storage, OWN, "arbiter", QUOTE, ["вид:график", "document"])
    _doc(storage, FOREIGN, "arbiter", QUOTE, ["document"])
    storage.close()
    before = _snapshot(settings)
    calls = []

    class ScriptedRouter:
        enabled = True
        base_url = "http://synthetic.invalid"

        def __init__(self, current):
            assert current == settings

        async def chat(self, messages, **kwargs):
            calls.append(messages)
            assert QUOTE in json.dumps(messages, ensure_ascii=False)
            assert kwargs == {"temperature": 0.0, "max_tokens": 200, "priority": "background", "tools": []}
            if answer == "error":
                raise RuntimeError(SECRET)
            payload = {
                "kind": "справка",
                "quote": QUOTE if answer == "valid" else "Несуществующая выдержка из источника.",
            }
            if answer == "new-kind":
                payload = {"kind": "другое", "proposed": "новый-тип", "quote": QUOTE}
            return {"content": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(llm_module, "LLMRouter", ScriptedRouter)
    report = settings.home / "arbiter-report.jsonl"
    code, output, error = _run(
        settings, monkeypatch, capsys, "--user", OWN, "--arbiter", "--apply", "--report", str(report)
    )
    after = _snapshot(settings)
    assert len(calls) == 1 and code == 0
    assert SECRET not in output + error + report.read_text(), (
        "model failure must not expose private exception material"
    )
    if answer != "error":
        assert error == ""
    expected = "вид:справка" if answer == "valid" else "вид:график"
    assert [json.loads(r["tags_json"]) for r in after["tables"]["knowledge_objects"] if r["id"] == own] == [
        [expected]
    ]
    _protect(before, after, [own])
    _event(before, after, {"seen": 1, "kinds": 1, "changed": 1})
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["id"] == own
    assert rows[0]["kind"] == ("справка" if answer == "valid" else "")
    if answer == "new-kind":
        assert "новый-тип" in output and rows[0]["evidence"] == "другое: новый-тип"


def test_retag_cli_limit_bounds_tag_writes_and_report_rows(retag_context, monkeypatch, capsys):
    settings, storage, _opened = retag_context
    own = [_doc(storage, OWN, f"limit{i}", "РАПОРТ\nПрошу разрешить убытие.", ["document"]) for i in range(3)]
    _doc(storage, FOREIGN, "foreign-limit", "РАПОРТ\nПрошу разрешить убытие.", ["document"])
    storage.close()
    before = _snapshot(settings)
    report = settings.home / "limited.jsonl"
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        "--user",
        OWN,
        "--batch",
        "200",
        "--limit",
        "1",
        "--apply",
        "--report",
        str(report),
    )
    after = _snapshot(settings)
    old = {r["id"]: r for r in before["tables"]["knowledge_objects"]}
    assert [r["id"] for r in after["tables"]["knowledge_objects"] if r != old[r["id"]]] == own[:1], (
        "--limit1 must not retag an entire batch"
    )
    assert [json.loads(line)["id"] for line in report.read_text().splitlines()] == own[:1]
    assert code == 0 and error == "" and "Просмотрено объектов: 1." in output


def test_retag_cli_disabled_arbiter_refuses_without_report_or_mutation(retag_context, monkeypatch, capsys):
    settings, storage, opened = retag_context
    _doc(storage, OWN, "disabled", QUOTE, ["document"])
    storage.close()
    before = _snapshot(settings)
    assert not settings.llm_enabled
    report = settings.home / "disabled.jsonl"
    code, output, error = _run(
        settings, monkeypatch, capsys, "--user", OWN, "--arbiter", "--apply", "--report", str(report)
    )
    assert code == 2 and output == "" and "Модель выключена" in error
    assert opened == [True] and not report.exists() and _snapshot(settings) == before


@pytest.mark.parametrize("mode", ["show", "apply"])
def test_retag_cli_learns_owned_boilerplate_and_rebuilds_tags_without_foreign_writes(
    retag_context, monkeypatch, capsys, mode
):
    settings, storage, _opened = retag_context
    own = [
        _doc(
            storage,
            OWN,
            f"boilerplate{i}",
            f"РАПОРТ\nНачало абонентский номер телефона окончание. Прошу отпуск {i}.",
            ["document", "устаревшийтег"],
        )
        for i in range(10)
    ]
    _doc(
        storage,
        FOREIGN,
        "foreign-boilerplate",
        "Исключительный чужой секретный материал.",
        ["document", "чужое"],
    )
    storage.close()
    before = _snapshot(settings)
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        "--user",
        OWN,
        "--learn-boilerplate",
        "--rebuild-tags",
        *(["--apply"] if mode == "apply" else []),
    )
    assert code == 0 and error == "" and "Просмотрено объектов: 10." in output
    after = _snapshot(settings)
    if mode == "show":
        assert after == before and "ПОКАЗ: список не сохранён" in output
        return
    learned = json.loads(after["boilerplate"])
    assert learned["documents"] == 10 and {"абонентский", "номер", "телефона"} <= set(learned["words"])
    assert not {"чужой", "секретный", "материал"} & set(learned["words"])
    _protect(before, after, own)
    for row in after["tables"]["knowledge_objects"]:
        if row["id"] in own:
            tags = set(json.loads(row["tags_json"]))
            assert "вид:рапорт" in tags
            assert not ({"document", "устаревшийтег"} | set(learned["words"])) & tags
    _event(before, after, {"seen": 10, "kinds": 10, "changed": 10})


@pytest.mark.parametrize("target", ["symlink", "directory"])
def test_retag_cli_invalid_report_target_is_safe_and_leaves_archive_unchanged(
    retag_context, monkeypatch, capsys, target
):
    settings, storage, _opened = retag_context
    _doc(storage, OWN, "bad-report", "РАПОРТ\nПрошу разрешить убытие.", ["document"])
    storage.close()
    before = _snapshot(settings)
    canary = settings.home / "unrelated.txt"
    canary.write_bytes(b"protected unrelated bytes")
    report = settings.home / "invalid-report"
    if target == "symlink":
        report.symlink_to(canary)
    else:
        report.mkdir()
    code, _output, _error = _run(
        settings, monkeypatch, capsys, "--user", OWN, "--apply", "--report", str(report)
    )
    assert code == 2
    assert canary.read_bytes() == b"protected unrelated bytes"
    assert _snapshot(settings) == before
