"""`jericho import` — the path from a folder on disk into the review queue.

Jericho could take a file; it could not take a folder, which is the shape personal
knowledge actually has. The risks worth pinning are not in the walking:

* **The review gate.** An import is the user pointing at a directory, not a decision
  about each file inside it. Everything must land in the Inbox — a bulk path that
  quietly promotes would turn one gesture into thousands of unreviewed facts.
* **Resumability.** A run over a real corpus will be interrupted. Re-running must skip
  what landed rather than duplicate it, and that rests on ``ingest_file`` deriving its
  ``source_ref`` from the content hash.
* **Survivability.** A real corpus contains something unreadable. One bad file must
  not end the run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jericho.bulk_import import (
    SKIP_EMPTY,
    SKIP_HIDDEN,
    SKIP_SUFFIX,
    SKIP_SYMLINK,
    SKIP_TOO_LARGE,
    plan_import,
    run_import,
    summarise,
)
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph


def _tree(root):
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "alpha.md").write_text("Проект «Альфа» стартует в марте.", encoding="utf-8")
    (root / "notes" / "beta.txt").write_text("Бета: договор подписан 14 мая.", encoding="utf-8")
    (root / "notes" / ".hidden.md").write_text("secret", encoding="utf-8")
    (root / "notes" / "empty.md").write_text("", encoding="utf-8")
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules" / "junk.js").write_text("module.exports = 1", encoding="utf-8")
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    return root


# --- planning: what the walk selects, without writing anything ------------


def test_plan_skips_build_output_vcs_hidden_and_empty(tmp_path):
    plan = plan_import(_tree(tmp_path), max_bytes=1_000_000)

    selected = sorted(item.path.name for item in plan.candidates)
    assert selected == ["alpha.md", "beta.txt"]
    # node_modules and .git are pruned during the walk, so their contents are never
    # even considered — they do not appear as skips.
    assert not [item for item in plan.skipped if "node_modules" in str(item.path)]
    assert not [item for item in plan.skipped if ".git" in str(item.path)]
    reasons = plan.skip_reasons()
    assert reasons.get(SKIP_HIDDEN) == 1 and reasons.get(SKIP_EMPTY) == 1
    assert plan.total_bytes > 0


def test_plan_filters_by_suffix_and_size(tmp_path):
    _tree(tmp_path)
    (tmp_path / "notes" / "big.md").write_text("x" * 5000, encoding="utf-8")

    by_suffix = plan_import(tmp_path, max_bytes=1_000_000, suffixes=["md"])
    assert sorted(i.path.name for i in by_suffix.candidates) == ["alpha.md", "big.md"]
    assert by_suffix.skip_reasons().get(SKIP_SUFFIX) == 1  # beta.txt

    by_size = plan_import(tmp_path, max_bytes=1000)
    assert "big.md" not in [i.path.name for i in by_size.candidates]
    assert by_size.skip_reasons().get(SKIP_TOO_LARGE) == 1


def test_plan_does_not_follow_symlinks_by_default(tmp_path):
    _tree(tmp_path)
    (tmp_path / "notes" / "link.md").symlink_to(tmp_path / "notes" / "alpha.md")

    plan = plan_import(tmp_path, max_bytes=1_000_000)
    assert "link.md" not in [i.path.name for i in plan.candidates]
    assert plan.skip_reasons().get(SKIP_SYMLINK) == 1


def test_plan_order_is_stable_so_limit_batches_rather_than_samples(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for index in range(20):
        (root / f"note-{index:02d}.md").write_text(f"note {index}", encoding="utf-8")

    first = plan_import(root, max_bytes=1_000_000, limit=5)
    again = plan_import(root, max_bytes=1_000_000, limit=5)
    assert [i.path for i in first.candidates] == [i.path for i in again.candidates]
    assert len(first.candidates) == 5
    # The next batch continues where the first ended, because the order is total.
    full = plan_import(root, max_bytes=1_000_000)
    assert [i.path for i in full.candidates][:5] == [i.path for i in first.candidates]


def test_planning_writes_nothing(tmp_path):
    """--dry-run has to be honest: planning must not touch the tree it inspects."""
    root = _tree(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    plan_import(root, max_bytes=1_000_000)
    after = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    assert before == after


# --- ingesting: the gate, resumability, survivability ---------------------


def _pipeline(settings, storage):
    # No LLM: the import path must work before a model exists, which is the state the
    # instance is actually in.
    return IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)


def test_every_imported_file_waits_in_the_inbox(settings, storage):
    """The user pointed at a folder; they did not vouch for each file in it."""
    root = _tree(settings.state_dir.parent / "corpus")
    plan = plan_import(root, max_bytes=settings.max_upload_bytes)
    pipeline = _pipeline(settings, storage)

    outcomes = asyncio.run(run_import(pipeline, "alice", plan))

    assert summarise(outcomes) == {"ingested": 2, "duplicate": 0, "failed": 0}
    assert storage.list_knowledge_objects("alice") == [], "an import must not create knowledge directly"
    pending = [item for item in pipeline.list_inbox("alice") if item["status"] == "pending"]
    assert len(pending) == 2


def test_rerunning_the_same_tree_ingests_nothing_twice(settings, storage):
    root = _tree(settings.state_dir.parent / "corpus")
    pipeline = _pipeline(settings, storage)
    plan = plan_import(root, max_bytes=settings.max_upload_bytes)

    first = asyncio.run(run_import(pipeline, "alice", plan))
    second = asyncio.run(
        run_import(pipeline, "alice", plan_import(root, max_bytes=settings.max_upload_bytes))
    )

    assert summarise(first)["ingested"] == 2
    assert summarise(second) == {"ingested": 0, "duplicate": 2, "failed": 0}
    # Both runs point at the same raw objects: a resume, not a second copy.
    assert {o.raw_object_id for o in first} == {o.raw_object_id for o in second}
    assert len(pipeline.list_inbox("alice")) == 2


def test_identical_files_at_different_paths_collapse_to_one(settings, storage):
    root = settings.state_dir.parent / "dupes"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    (root / "a" / "one.md").write_text("одно и то же содержимое", encoding="utf-8")
    (root / "b" / "copy.md").write_text("одно и то же содержимое", encoding="utf-8")

    outcomes = asyncio.run(
        run_import(
            _pipeline(settings, storage), "alice", plan_import(root, max_bytes=settings.max_upload_bytes)
        )
    )

    assert summarise(outcomes) == {"ingested": 1, "duplicate": 1, "failed": 0}
    assert len({o.raw_object_id for o in outcomes}) == 1


def test_one_unreadable_file_does_not_end_the_run(settings, storage, monkeypatch):
    """A real corpus contains something broken; losing the rest to it would be absurd."""
    root = settings.state_dir.parent / "corpus"
    _tree(root)
    (root / "notes" / "gamma.md").write_text("Гамма: третий файл.", encoding="utf-8")
    plan = plan_import(root, max_bytes=settings.max_upload_bytes)
    assert len(plan.candidates) == 3

    real_read = type(root).read_bytes

    def explode(self):
        if self.name == "beta.txt":
            raise OSError("Input/output error")
        return real_read(self)

    monkeypatch.setattr(type(root), "read_bytes", explode)
    outcomes = asyncio.run(run_import(_pipeline(settings, storage), "alice", plan))

    assert summarise(outcomes) == {"ingested": 2, "duplicate": 0, "failed": 1}
    failed = [o for o in outcomes if o.status == "failed"]
    assert failed[0].path.name == "beta.txt" and "OSError" in failed[0].detail


def test_progress_is_reported_for_every_file(settings, storage):
    root = _tree(settings.state_dir.parent / "corpus")
    seen: list[tuple[int, int, str]] = []
    asyncio.run(
        run_import(
            _pipeline(settings, storage),
            "alice",
            plan_import(root, max_bytes=settings.max_upload_bytes),
            on_progress=lambda index, total, outcome: seen.append((index, total, outcome.status)),
        )
    )
    assert [(index, total) for index, total, _ in seen] == [(1, 2), (2, 2)]


def test_single_file_path_is_accepted(tmp_path):
    note = tmp_path / "solo.md"
    note.write_text("одна заметка", encoding="utf-8")
    plan = plan_import(note, max_bytes=1_000_000)
    assert [i.path.name for i in plan.candidates] == ["solo.md"]


@pytest.mark.parametrize("requested,users,expected", [("bob", [], "bob"), (None, [{"id": "solo"}], "solo")])
def test_target_account_is_resolved_when_unambiguous(requested, users, expected):
    from jericho.cli import _resolve_import_user

    class _Storage:
        def list_users(self, **_kwargs):
            return users

    assert _resolve_import_user(_Storage(), requested) == expected


@pytest.mark.parametrize("users", [[], [{"id": "a"}, {"id": "b"}]])
def test_target_account_refuses_to_guess(users):
    """Misfiling an import into the wrong tenant is not undoable with a flag."""
    from jericho.cli import _resolve_import_user

    class _Storage:
        def list_users(self, **_kwargs):
            return users

    with pytest.raises(ValueError, match="--user"):
        _resolve_import_user(_Storage(), None)


def test_provenance_path_is_absolute(settings, storage, monkeypatch, tmp_path):
    """A relative path recorded as provenance is worthless once the cwd moves."""
    _tree(tmp_path / "corpus")
    monkeypatch.chdir(tmp_path)

    outcomes = asyncio.run(
        run_import(
            _pipeline(settings, storage),
            "alice",
            plan_import(Path("corpus"), max_bytes=settings.max_upload_bytes),
        )
    )

    raw = storage.get_raw_object(outcomes[0].raw_object_id, "alice")
    stored = json.loads(raw["metadata_json"])["import_source_path"]
    assert Path(stored).is_absolute(), stored
    assert Path(stored).is_file()


# --- the review gate under bulk pressure ----------------------------------


def _pending_ids(settings, storage, count: int) -> list[str]:
    pipeline = _pipeline(settings, storage)
    ids = []
    for index in range(count):
        result = asyncio.run(
            pipeline.ingest_file(
                "alice",
                None,
                f"Проект {index} стартует в марте.".encode(),
                filename=f"note-{index}.md",
                force_review=True,
            )
        )
        ids.append(result["inbox_id"])
    return ids


def test_bulk_review_cannot_canonize_with_an_omitted_flag(settings, storage):
    """The minimal body — naming neither status nor promote — used to promote everything.

    `status` defaulted to "classified" and `promote` to None, and classify_inbox_item
    reads exactly that pair as consent. So the laziest possible request canonized every
    item it was handed, without the caller ever typing the word promote.
    """
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    ids = _pending_ids(settings, storage, 5)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/admin/inbox/bulk",
            json={"user_id": "alice", "inbox_ids": ids},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

    assert response.status_code == 400, response.text
    assert storage.list_knowledge_objects("alice") == []


def test_bulk_review_refuses_explicit_promotion_too(settings, storage):
    """Being explicit does not make approving 200 unread items a decision."""
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    ids = _pending_ids(settings, storage, 3)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(create_app(settings)) as client:
        for body in (
            {"user_id": "alice", "inbox_ids": ids, "status": "classified", "promote": True},
            {"user_id": "alice", "inbox_ids": ids, "status": "classified"},
            {"user_id": "alice", "inbox_ids": ids, "status": "ignored", "promote": True},
        ):
            response = client.post("/api/admin/inbox/bulk", json=body, headers=headers)
            assert response.status_code == 400, f"{body} -> {response.status_code}"
    assert storage.list_knowledge_objects("alice") == []


def test_bulk_review_still_dismisses(settings, storage):
    """Dismissal is the point of a bulk action and must keep working."""
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    ids = _pending_ids(settings, storage, 4)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/admin/inbox/bulk",
            json={"user_id": "alice", "inbox_ids": ids, "status": "ignored", "promote": False},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

    assert response.status_code == 200, response.text
    assert len(response.json()["changed"]) == 4
    assert storage.list_knowledge_objects("alice") == []


def test_single_item_promotion_is_untouched(settings, storage):
    """The per-item path shows the content and stays the way knowledge is created."""
    inbox_id = _pending_ids(settings, storage, 1)[0]
    pipeline = _pipeline(settings, storage)

    from jericho.storage.models import InboxStatus

    pipeline.classify_inbox_item("alice", inbox_id, InboxStatus.CLASSIFIED, promote=True, reviewed_by="alice")

    assert len(storage.list_knowledge_objects("alice")) == 1
