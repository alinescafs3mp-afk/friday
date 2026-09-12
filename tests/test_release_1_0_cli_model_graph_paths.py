"""Real CLI/graph/storage with a scripted chat boundary, never live-model evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import stat
import sys

import pytest

from friday import cli, config
from friday import storage as storage_module
from friday.agent_runtime import llm as llm_module
from friday.knowledge_graph import KnowledgeGraph
from friday.storage import FridayStorage
from friday.storage.models import EntityType, KnowledgeObject, RawObject

OWN, FOREIGN = "model-graph-owner", "model-graph-foreign"
EXTRACT, REVIEW = "extract-structure-relations", "review-relation-candidates"
SECRET = "synthetic-private-model-credential-089"
TABLES = (
    "raw_objects",
    "knowledge_objects",
    "knowledge_object_versions",
    "entities",
    "knowledge_entity_links",
    "relation_candidates",
    "relations",
    "relation_revisions",
)
EVENTS = ("graph.structure_relations_extracted", "graph.relation_candidates_reviewed")


def _locked(settings, role):
    path = settings.state_dir / f"{role}.lock"
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


def _document(storage, user, key, *, queued=False, invented=False):
    names = ["Иванов Пётр Петрович " + key, "Смирнова Анна Ивановна " + key]
    quote = "Супруга: " + names[1]
    text = "Анкета. " + user + "\n" + names[0] + "\n" + quote
    raw = RawObject(
        id="raw-" + user + key,
        user_id=user,
        source="test",
        source_ref=key,
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id="ko-" + user + key, user_id=user, raw_object_id=raw.id, content=text, title="Анкета"
    )
    storage.store_knowledge_object(ko)
    graph = KnowledgeGraph(storage)
    ids = [str(graph.create_entity(user, name, EntityType.PERSON)["id"]) for name in names]
    for entity in ids:
        graph.link_knowledge_to_entity(ko.id, entity, user)
    candidate = None
    if queued:
        candidate = storage.store_relation_candidate(
            user,
            *ids,
            "family_of",
            confidence=0.8,
            evidence={
                "knowledge_object_id": ko.id,
                "source_name": names[0],
                "target_name": names[1],
                "excerpt": "Цитата отсутствует в документе" if invented else quote,
                "method": "document_structure_arbiter",
            },
        )["id"]
    return {"ko": ko.id, "ids": ids, "names": names, "quote": quote, "candidate": candidate}


def _snapshot(settings):
    fresh = FridayStorage(settings)
    try:
        return {
            "tables": {
                name: [dict(r) for r in fresh.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                for name in TABLES
            },
            "events": {name: fresh.list_events(event_type=name) for name in EVENTS},
        }
    finally:
        fresh.close()


def _run(settings, monkeypatch, capsys, command, *args):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", command, *args])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    assert not _locked(settings, "account-deletion") and not _locked(settings, "backend")
    return exited.value.code, captured.out, captured.err


def _protect(before, after, changed):
    for table in TABLES:
        old, new = before["tables"][table], after["tables"][table]
        if table not in changed:
            assert old == new, table
        else:
            assert [r for r in old if r.get("user_id") == FOREIGN] == [
                r for r in new if r.get("user_id") == FOREIGN
            ], table


def _script(monkeypatch, settings, command, answer="valid"):
    calls = []

    class ScriptedRouter:
        enabled = True
        base_url = "http://synthetic.invalid"

        def __init__(self, current):
            assert current == settings

        async def chat(self, messages, **kwargs):
            calls.append(messages)
            rendered = json.dumps(messages, ensure_ascii=False)
            assert OWN in rendered and FOREIGN not in rendered
            assert kwargs == {
                "temperature": 0.0,
                "max_tokens": 1200 if command == EXTRACT else 400,
                "priority": "background",
                "tools": [],
            }
            if answer == "error":
                raise RuntimeError(SECRET)
            if answer == "malformed":
                return {"content": "unparseable model response"}
            if command == EXTRACT:
                listing = str(messages[1]["content"])
                entries = re.findall(r"^(\d+)\. (.+) \[person\]$", listing, re.M)
                assert len(entries) == 2
                source = next((index, name) for index, name in entries if name.startswith("Иванов"))
                target = next((index, name) for index, name in entries if name.startswith("Смирнова"))
                payload = {
                    "subject": source[1],
                    "relations": [
                        {
                            "source": int(source[0]),
                            "target": int(target[0]),
                            "type": "invented_relation" if answer == "bad-type" else "family_of",
                            "quote": "Вымышленная выдержка"
                            if answer == "bad-quote"
                            else "Супруга: " + target[1],
                            "confidence": 0.8,
                        }
                    ],
                }
            else:
                verdict = (
                    "reject"
                    if answer == "disagree" and len(calls) % 2 == 0
                    else ("confirm" if answer == "disagree" else answer)
                )
                payload = {"verdict": verdict, "about": "", "reason": "synthetic bounded verdict"}
            return {"content": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr(llm_module, "LLMRouter", ScriptedRouter)
    return calls


@pytest.mark.parametrize("apply", [False, True])
def test_structure_cli_real_queue_show_apply_repeat_and_foreign_preservation(
    graph_context, monkeypatch, capsys, apply
):
    settings, storage, _opened = graph_context
    own = _document(storage, OWN, "own")
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, EXTRACT)
    args = ["--user", OWN, "--batch", "1"] + (["--apply"] if apply else [])
    code, output, error = _run(settings, monkeypatch, capsys, EXTRACT, *args)
    assert code == 0 and not error and len(calls) == 1
    assert "Просмотрено объектов: 1." in output and "прошло проверки: 1." in output
    after = _snapshot(settings)
    if not apply:
        assert after == before and "ПОКАЗ" in output
        return
    _protect(before, after, {"relation_candidates"})
    rows = [r for r in after["tables"]["relation_candidates"] if r["user_id"] == OWN]
    assert len(rows) == 1 and rows[0]["status"] == "suggested"
    assert [rows[0]["source_entity_id"], rows[0]["target_entity_id"]] == own["ids"]
    evidence = json.loads(rows[0]["evidence_json"])
    assert evidence["knowledge_object_id"] == own["ko"] and evidence["excerpt"] == own["quote"]
    assert evidence["method"] == "document_structure_arbiter"
    assert after["events"][EVENTS[0]][0]["payload"] == {"scanned": 1, "proposed": 1, "kept": 1}
    code, _output, error = _run(settings, monkeypatch, capsys, EXTRACT, *args)
    again = _snapshot(settings)
    assert code == 0 and not error and again["tables"] == after["tables"] and len(calls) == 2


@pytest.mark.parametrize("answer", ["bad-quote", "bad-type", "malformed", "error"])
def test_structure_cli_rejects_ungrounded_output_and_reports_model_failure(
    graph_context, monkeypatch, capsys, answer
):
    settings, storage, _opened = graph_context
    _document(storage, OWN, "own")
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, EXTRACT, answer)
    code, output, error = _run(settings, monkeypatch, capsys, EXTRACT, "--user", OWN, "--apply")
    assert code == (1 if answer == "error" else 0) and len(calls) == 1
    assert SECRET not in output + error
    if answer == "error":
        assert "ОШИБОК ВЫЗОВА МОДЕЛИ: 1" in error and "Ни одно окно не разобрано" in error
    else:
        assert not error
    assert _snapshot(settings)["tables"] == before["tables"]


@pytest.mark.parametrize("batch", [1, 50])
def test_structure_cli_limit_bounds_actual_document_and_model_work(graph_context, monkeypatch, capsys, batch):
    settings, storage, _opened = graph_context
    for key in ("one", "two", "three"):
        _document(storage, OWN, key)
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, EXTRACT)
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        EXTRACT,
        "--user",
        OWN,
        "--batch",
        str(batch),
        "--limit",
        "1",
        "--apply",
    )
    assert code == 0 and not error
    after = _snapshot(settings)
    _protect(before, after, {"relation_candidates"})
    assert len(calls) == 1 and "Просмотрено объектов: 1." in output, output
    assert len([r for r in after["tables"]["relation_candidates"] if r["user_id"] == OWN]) == 1


@pytest.mark.parametrize("command", [EXTRACT, REVIEW])
def test_model_graph_cli_real_disabled_router_refuses_without_mutation(
    graph_context, monkeypatch, capsys, command
):
    settings, storage, opened = graph_context
    assert not settings.llm_enabled
    _document(storage, OWN, "own", queued=True)
    storage.close()
    before = _snapshot(settings)
    code, _output, error = _run(settings, monkeypatch, capsys, command, "--user", OWN, "--apply")
    assert code == 2 and "Модель выключена" in error and opened
    assert _snapshot(settings) == before


@pytest.mark.parametrize("answer", ["confirm", "reject", "unsure", "disagree", "malformed"])
@pytest.mark.parametrize("apply", [False, True])
def test_review_cli_verdicts_votes_private_report_show_apply_and_foreign_preservation(
    graph_context, monkeypatch, capsys, answer, apply
):
    settings, storage, _opened = graph_context
    own = _document(storage, OWN, "own", queued=True)
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, REVIEW, answer)
    report = settings.home / "review.jsonl"
    args = ["--user", OWN, "--votes", "2", "--report", str(report)] + (["--apply"] if apply else [])
    code, output, error = _run(settings, monkeypatch, capsys, REVIEW, *args)
    assert code == 0 and not error and len(calls) == 2 and "Просмотрено кандидатов: 1." in output
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    verdict = answer if answer in {"confirm", "reject"} else "unsure"
    (row,) = [json.loads(line) for line in report.read_text().splitlines()]
    assert row["candidate_id"] == own["candidate"] and row["knowledge_object_id"] == own["ko"]
    assert row["source_name"] == own["names"][0] and row["target_name"] == own["names"][1]
    assert row["excerpt"] == own["quote"] and row["relation_type"] == "family_of"
    assert row["verdict"] == verdict and row["checked_by"] == "arbiter"
    assert FOREIGN not in report.read_text()
    after = _snapshot(settings)
    if not apply:
        assert after == before and "ПОКАЗ" in output
        return
    changed = {"relation_candidates", "relations", "relation_revisions"} if verdict != "unsure" else set()
    _protect(before, after, changed)
    candidate = next(r for r in after["tables"]["relation_candidates"] if r["id"] == own["candidate"])
    assert (
        candidate["status"] == {"confirm": "accepted", "reject": "rejected", "unsure": "suggested"}[verdict]
    )
    relations = [r for r in after["tables"]["relations"] if r["user_id"] == OWN]
    assert len(relations) == (1 if verdict == "confirm" else 0)
    if relations:
        assert [relations[0]["source_entity_id"], relations[0]["target_entity_id"]] == own["ids"]
        assert relations[0]["relation_type"] == "family_of"
    event = after["events"][EVENTS[1]][0]["payload"]
    assert event["seen"] == 1 and event[verdict] == 1 and event["applied"] == int(verdict != "unsure")
    assert event["model_errors"] == 0 and event["verdicts"] is None
    if verdict != "unsure":
        code, output, error = _run(settings, monkeypatch, capsys, REVIEW, *args)
        assert code == 0 and not error and "Просмотрено кандидатов: 0." in output and len(calls) == 2
        assert _snapshot(settings)["tables"] == after["tables"]


def test_review_cli_limit_processes_only_one_owned_candidate(graph_context, monkeypatch, capsys):
    settings, storage, _opened = graph_context
    for key in ("one", "two"):
        _document(storage, OWN, key, queued=True)
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, REVIEW, "reject")
    code, output, error = _run(
        settings, monkeypatch, capsys, REVIEW, "--user", OWN, "--votes", "1", "--limit", "1", "--apply"
    )
    assert code == 0 and not error and len(calls) == 1 and "Просмотрено кандидатов: 1." in output
    after = _snapshot(settings)
    _protect(before, after, {"relation_candidates"})
    assert sorted(r["status"] for r in after["tables"]["relation_candidates"] if r["user_id"] == OWN) == [
        "rejected",
        "suggested",
    ]


def test_review_cli_false_quote_rejects_without_calling_model(graph_context, monkeypatch, capsys):
    settings, storage, _opened = graph_context
    own = _document(storage, OWN, "own", queued=True, invented=True)
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, REVIEW, "confirm")
    report = settings.home / "structure-verdict.jsonl"
    code, _output, error = _run(
        settings,
        monkeypatch,
        capsys,
        REVIEW,
        "--user",
        OWN,
        "--votes",
        "3",
        "--apply",
        "--report",
        str(report),
    )
    assert code == 0 and not error and not calls
    after = _snapshot(settings)
    _protect(before, after, {"relation_candidates"})
    assert (
        next(r for r in after["tables"]["relation_candidates"] if r["id"] == own["candidate"])["status"]
        == "rejected"
    )
    row = json.loads(report.read_text())
    assert row["verdict"] == "reject" and row["checked_by"] == "structure"


def test_review_cli_model_failure_is_truthful_without_exception_secret_in_report(
    graph_context, monkeypatch, capsys
):
    settings, storage, _opened = graph_context
    _document(storage, OWN, "own", queued=True)
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, REVIEW, "error")
    report = settings.home / "error-report.jsonl"
    code, output, error = _run(
        settings, monkeypatch, capsys, REVIEW, "--user", OWN, "--apply", "--report", str(report)
    )
    assert code == 1 and len(calls) == 1 and "ОШИБОК ВЫЗОВА МОДЕЛИ: 1" in error
    assert _snapshot(settings)["tables"] == before["tables"]
    assert (
        stat.S_IMODE(report.stat().st_mode) == 0o600 and json.loads(report.read_text())["verdict"] == "unsure"
    )
    assert SECRET not in output + error + report.read_text(), (
        "private model exception material must not enter durable reports"
    )


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_review_cli_invalid_report_target_refuses_without_graph_mutation(
    graph_context, monkeypatch, capsys, kind
):
    settings, storage, _opened = graph_context
    _document(storage, OWN, "own", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, REVIEW, "confirm")
    report, canary = settings.home / "invalid-report", settings.home / "canary"
    canary.write_text("preserve report destination canary")
    if kind == "symlink":
        report.symlink_to(canary)
    else:
        report.mkdir()
    code, _output, error = _run(
        settings, monkeypatch, capsys, REVIEW, "--user", OWN, "--apply", "--report", str(report)
    )
    assert code == 2 and error and not calls
    assert canary.read_text() == "preserve report destination canary" and _snapshot(settings) == before


def test_structure_cli_limit_stops_on_partial_second_page(graph_context, monkeypatch, capsys):
    settings, storage, _opened = graph_context
    for key in ("one", "two", "three", "four", "five"):
        _document(storage, OWN, key)
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, EXTRACT)
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        EXTRACT,
        "--user",
        OWN,
        "--batch",
        "2",
        "--limit",
        "3",
        "--apply",
    )
    assert code == 0 and not error
    after = _snapshot(settings)
    _protect(before, after, {"relation_candidates"})
    assert len(calls) == 3 and "Просмотрено объектов: 3." in output, output
    assert len([r for r in after["tables"]["relation_candidates"] if r["user_id"] == OWN]) == 3


@pytest.mark.parametrize("apply", [False, True])
def test_structure_cli_outer_failure_stays_private_and_marks_partial_batch_failed(
    graph_context, monkeypatch, capsys, apply
):
    settings, storage, _opened = graph_context
    bad = _document(storage, OWN, "first-error")
    _document(storage, OWN, "second-valid")
    _document(storage, FOREIGN, "foreign", queued=True)
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, EXTRACT)
    original = KnowledgeGraph.suggest_relations_from_structure

    async def provider(self, user_id, knowledge_id, **kwargs):
        if knowledge_id == bad["ko"]:
            raise RuntimeError(SECRET + " private document fragment")
        return await original(self, user_id, knowledge_id, **kwargs)

    monkeypatch.setattr(KnowledgeGraph, "suggest_relations_from_structure", provider)
    code, output, error = _run(
        settings,
        monkeypatch,
        capsys,
        EXTRACT,
        "--user",
        OWN,
        *(["--apply"] if apply else []),
    )
    assert len(calls) == 1 and "Просмотрено объектов: 2." in output
    after = _snapshot(settings)
    _protect(before, after, {"relation_candidates"} if apply else set())
    assert (code, SECRET in output + error) == (1, False)
    assert "RuntimeError" in error and "ОШИБОК ОБРАБОТКИ ОБЪЕКТОВ: 1" in error
