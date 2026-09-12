"""Real eval-bootstrap CLI, proposal audit and storage; scripted chat is not live evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sys
from dataclasses import replace

import pytest

from friday import cli, config
from friday import storage as storage_module
from friday.agent_runtime import llm as llm_module
from friday.storage import FridayStorage
from friday.storage.models import KnowledgeObject, RawObject

OWN, FOREIGN = "eval-cli-owner", "eval-cli-foreign"
DOCUMENT = "Правило резервных копий. Копия на том же диске копией не является. Нужен внешний носитель и регулярная проверка восстановлением."
QUERY = "почему копия рядом бесполезна"
BAD_QUERY = "правило резервных копий внешний носитель"
SECRET = "synthetic-private-eval-credential-089"
TABLES = (
    "raw_objects",
    "knowledge_objects",
    "knowledge_object_versions",
    "entities",
    "relations",
    "eval_cases",
)


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
def eval_context(settings, storage, monkeypatch):
    current = replace(settings, llm_enabled=True)
    for user in (OWN, FOREIGN):
        storage.ensure_user(user, source="test", display_name=user)
    monkeypatch.setattr(config, "load_settings", lambda: current)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    original, opened = storage_module.init_storage, []

    def observed(loaded):
        assert loaded == current
        assert _locked(current, "account-deletion") and _locked(current, "backend")
        opened.append(True)
        return original(loaded)

    monkeypatch.setattr(storage_module, "init_storage", observed)
    return current, storage, opened


def _doc(storage, user, key):
    body = DOCUMENT + " " + user + " " + key
    raw = RawObject(
        id="raw-" + user + key,
        user_id=user,
        source="test",
        source_ref=key,
        raw_content=body,
        content_type="text",
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id="ko-" + user + key,
        user_id=user,
        raw_object_id=raw.id,
        content=body,
        title="Правило " + key,
        summary=DOCUMENT,
        knowledge_kind="note",
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _snapshot(settings):
    fresh = FridayStorage(settings)
    try:
        return {
            name: [dict(row) for row in fresh.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in TABLES
        }
    finally:
        fresh.close()


def _run(settings, monkeypatch, capsys, *args):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", "eval-bootstrap", *args])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    assert not _locked(settings, "account-deletion") and not _locked(settings, "backend")
    return exited.value.code, captured.out, captured.err


def _script(monkeypatch, settings, reply):
    calls = []

    class ScriptedRouter:
        def __init__(self, current):
            assert current == settings

        async def chat(self, messages, **kwargs):
            assert len(messages) == 1 and messages[0]["role"] == "user"
            content = str(messages[0]["content"])
            assert OWN in content and FOREIGN not in content and DOCUMENT in content
            assert kwargs == {"temperature": 0.2, "max_tokens": 200}
            calls.append(content)
            result = reply(content, len(calls))
            if isinstance(result, Exception):
                raise result
            return {"content": result}

    monkeypatch.setattr(llm_module, "LLMRouter", ScriptedRouter)
    return calls


@pytest.mark.parametrize("form", ["json", "plain"])
@pytest.mark.parametrize("save", [False, True])
def test_eval_cli_filter_show_save_provenance_repeat_and_foreign_preservation(
    eval_context, monkeypatch, capsys, form, save
):
    settings, storage, opened = eval_context
    accepted = _doc(storage, OWN, "VALID-DOC")
    _doc(storage, OWN, "REJECT-DOC")
    foreign = _doc(storage, FOREIGN, "FOREIGN-DOC")
    storage.add_eval_case(FOREIGN, QUERY, [foreign], source="manual", note="keep foreign manual case")
    storage.add_eval_case(
        OWN, "вручную заданный вопрос", [accepted], source="manual", note="keep owned manual case"
    )
    storage.close()
    before = _snapshot(settings)

    def reply(content, _number):
        query = QUERY if "VALID-DOC" in content else BAD_QUERY
        return json.dumps({"query": query}, ensure_ascii=False) if form == "json" else query

    calls = _script(monkeypatch, settings, reply)
    args = ["--user", OWN, "--limit", "2"] + (["--save"] if save else [])
    code, output, error = _run(settings, monkeypatch, capsys, *args)
    assert code == 0 and not error and len(calls) == 2 and len(opened) == 1
    assert "Годных 1 из 2." in output and "пересказывает" in output
    after = _snapshot(settings)
    if not save:
        assert "Ничего не сохранено" in output and after == before
        return
    assert "Сохранено кейсов: 1." in output
    for table in TABLES:
        if table != "eval_cases":
            assert after[table] == before[table], table
    assert after["eval_cases"][: len(before["eval_cases"])] == before["eval_cases"]
    rows = after["eval_cases"][len(before["eval_cases"]) :]
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == OWN and row["query"] == QUERY and row["source"] == "bootstrap"
    assert json.loads(row["expected_ids_json"]) == [accepted] and row["note"].startswith("bootstrap:")
    code, output, error = _run(settings, monkeypatch, capsys, *args)
    assert code == 0 and not error and len(calls) == 4 and "Сохранено кейсов: 0." in output
    assert "такой вопрос уже есть" in output and _snapshot(settings) == after


@pytest.mark.parametrize("answer", ["paraphrase", "short", "empty"])
def test_eval_cli_refuses_unusable_proposals_without_saving(eval_context, monkeypatch, capsys, answer):
    settings, storage, _opened = eval_context
    _doc(storage, OWN, "own")
    storage.close()
    before = _snapshot(settings)
    value = {"paraphrase": BAD_QUERY, "short": "копии", "empty": ""}[answer]
    calls = _script(
        monkeypatch, settings, lambda _content, _number: json.dumps({"query": value}) if value else ""
    )
    code, output, error = _run(settings, monkeypatch, capsys, "--user", OWN, "--save")
    assert code == 0 and not error and len(calls) == 1 and "Годных 0 из 1." in output
    assert {"paraphrase": "пересказывает", "short": "слишком короткий", "empty": "не вернула вопрос"}[
        answer
    ] in output
    assert "Сохранено кейсов: 0." in output and _snapshot(settings) == before


@pytest.mark.parametrize("mode", ["disabled", "ambiguous", "empty"])
def test_eval_cli_disabled_ambiguous_and_empty_refuse_or_report_without_mutation(
    eval_context, monkeypatch, capsys, mode
):
    settings, storage, opened = eval_context
    if mode != "empty":
        _doc(storage, OWN, "own")
    _doc(storage, FOREIGN, "foreign")
    storage.close()
    before = _snapshot(settings)
    if mode == "disabled":
        monkeypatch.setattr(config, "load_settings", lambda: replace(settings, llm_enabled=False))
        calls = []  # Actual disabled settings gate; no router replacement.
    else:
        calls = _script(monkeypatch, settings, lambda _content, _number: json.dumps({"query": QUERY}))
    args = ["--save"] + ([] if mode == "ambiguous" else ["--user", OWN])
    code, output, error = _run(settings, monkeypatch, capsys, *args)
    assert code == (0 if mode == "empty" else 2) and not calls
    if mode == "empty":
        assert "в базе нет знаний" in output and not error
    elif mode == "disabled":
        assert "Нужна работающая модель" in error and not opened
    else:
        assert "--user" in error and opened
    assert _snapshot(settings) == before


def test_eval_cli_limit_caps_real_model_proposals_and_saved_cases(eval_context, monkeypatch, capsys):
    settings, storage, _opened = eval_context
    ids = {_doc(storage, OWN, key) for key in ("one", "two", "three")}
    _doc(storage, FOREIGN, "foreign")
    storage.close()
    before = _snapshot(settings)
    calls = _script(monkeypatch, settings, lambda _content, _number: json.dumps({"query": QUERY}))
    code, output, error = _run(settings, monkeypatch, capsys, "--user", OWN, "--limit", "1", "--save")
    assert code == 0 and not error and len(calls) == 1 and "Годных 1 из 1." in output
    after = _snapshot(settings)
    for table in TABLES:
        if table != "eval_cases":
            assert after[table] == before[table]
    assert len(after["eval_cases"]) == 1
    expected = json.loads(after["eval_cases"][0]["expected_ids_json"])
    assert len(expected) == 1 and expected[0] in ids


def test_eval_cli_one_model_failure_continues_and_never_prints_secret(eval_context, monkeypatch, capsys):
    settings, storage, _opened = eval_context
    for key in ("one", "two"):
        _doc(storage, OWN, key)
    _doc(storage, FOREIGN, "foreign")
    storage.close()
    before = _snapshot(settings)
    calls = _script(
        monkeypatch,
        settings,
        lambda _content, number: RuntimeError(SECRET) if number == 1 else json.dumps({"query": QUERY}),
    )
    code, output, error = _run(settings, monkeypatch, capsys, "--user", OWN, "--limit", "2", "--save")
    assert code == 0 and not error and len(calls) == 2
    assert (
        "модель недоступна: RuntimeError" in output
        and "Годных 1 из 2." in output
        and SECRET not in output + error
    )
    after = _snapshot(settings)
    for table in TABLES:
        if table != "eval_cases":
            assert after[table] == before[table]
    assert len(after["eval_cases"]) == 1 and after["eval_cases"][0]["user_id"] == OWN
    assert SECRET not in json.dumps(after)
