"""Walk a directory and feed every file through the review-gated ingestion pipeline.

Jericho could always take a file — one at a time, from Telegram or the API. What it
could not do is take a *folder*, which is the shape personal knowledge actually has on
disk. This closes that.

Two properties come free from ``ingest_file`` and are worth stating, because the whole
design leans on them. It derives ``source_ref`` from the content hash when the caller
gives none, so importing the same tree twice ingests nothing the second time: a run
interrupted at file 4000 of 9000 resumes by simply being run again. The same mechanism
collapses byte-identical files at different paths into one object.

Nothing here promotes anything. Every file lands in the Inbox exactly as a message
would, because a directory the user pointed at is still not a decision the user made
about each file in it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Directories that are never personal knowledge: build output, dependency trees and
# VCS internals. Walking them is slow and everything found is noise.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".bzr",
        ".cache",
        ".git",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".terraform",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "target",
        "venv",
    }
)

SKIP_EMPTY = "empty file"
SKIP_HIDDEN = "hidden"
SKIP_OFFICE_LOCK = "Office lock file"
SKIP_NOT_REGULAR = "not a regular file"
SKIP_SUFFIX = "suffix not selected"
SKIP_SYMLINK = "symlink"
SKIP_TOO_LARGE = "exceeds the upload limit"
SKIP_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Candidate:
    """A file the walk selected, with the size the plan was costed on."""

    path: Path
    size: int


@dataclass(frozen=True)
class Skipped:
    path: Path
    reason: str


@dataclass
class ImportPlan:
    """What a run would do, computed without writing anything."""

    root: Path
    candidates: list[Candidate] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.candidates)

    def by_suffix(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.candidates:
            counts[item.path.suffix.lower() or "(no suffix)"] = (
                counts.get(item.path.suffix.lower() or "(no suffix)", 0) + 1
            )
        return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))

    def skip_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.skipped:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


@dataclass
class Outcome:
    """What became of one file. ``status`` is one of ingested/duplicate/failed."""

    path: Path
    status: str
    detail: str = ""
    raw_object_id: str = ""
    inbox_id: str = ""


def plan_import(
    root: Path,
    *,
    max_bytes: int,
    suffixes: Iterable[str] | None = None,
    include_hidden: bool = False,
    follow_symlinks: bool = False,
    limit: int | None = None,
) -> ImportPlan:
    """Decide what to import, touching nothing.

    The walk is sorted at every level so two runs over an unchanged tree produce the
    same order — which is what makes ``--limit`` a usable way to import a large tree in
    batches rather than a random sample.

    ⚠️ `limit` здесь — потолок ПЛАНА, а не партии. Планирование ничего не знает о том,
    что уже загружено (в этом его смысл: оно не трогает хранилище), поэтому одного
    упорядоченного обхода для «партий» НЕ ХВАТАЕТ: второй запуск с тем же `--limit`
    выберет ровно те же первые N файлов, все они отыграются как дубликаты, и прогресс
    будет нулевым. Партии делает `run_import`, считая по ЗАГРУЖЕННЫМ, — см. его
    параметр `stop_after`.
    """
    # Resolved, not just expanded: the stored path is provenance a reader consults
    # later, and a relative one is worthless once the working directory has moved.
    root = root.expanduser().resolve()
    plan = ImportPlan(root=root)
    wanted = {s if s.startswith(".") else f".{s}" for s in (suffixes or ())}
    wanted = {s.lower() for s in wanted}

    if root.is_file():
        _consider(plan, root, max_bytes, wanted, include_hidden, follow_symlinks)
        return plan

    for current_dir, dir_names, file_names in os.walk(root, topdown=True, followlinks=follow_symlinks):
        here = Path(current_dir)
        # Pruning in place is what keeps a node_modules tree from being walked at all,
        # rather than walked and then discarded file by file.
        dir_names[:] = sorted(
            name
            for name in dir_names
            if name not in EXCLUDED_DIR_NAMES
            and (include_hidden or not name.startswith("."))
            and (follow_symlinks or not (here / name).is_symlink())
        )
        for name in sorted(file_names):
            if limit is not None and len(plan.candidates) >= limit:
                return plan
            _consider(plan, here / name, max_bytes, wanted, include_hidden, follow_symlinks)
    return plan


def _consider(
    plan: ImportPlan,
    path: Path,
    max_bytes: int,
    wanted: set[str],
    include_hidden: bool,
    follow_symlinks: bool,
) -> None:
    if not include_hidden and path.name.startswith("."):
        plan.skipped.append(Skipped(path, SKIP_HIDDEN))
        return
    # Word и Excel держат рядом с открытым документом файл-замок `~$имя.docx`: то же
    # расширение, десяток байт, не zip. По расширению он неотличим от документа, и
    # 39 таких файлов приехали в реальном импорте — каждый как «поступление», каждый
    # с нечитаемым содержимым, каждый требующий решения человека. Это не документ и
    # не выбор пользователя, а служебный мусор офисного пакета.
    if path.name.startswith("~$"):
        plan.skipped.append(Skipped(path, SKIP_OFFICE_LOCK))
        return
    if not follow_symlinks and path.is_symlink():
        plan.skipped.append(Skipped(path, SKIP_SYMLINK))
        return
    try:
        stat = path.stat()
    except OSError:
        plan.skipped.append(Skipped(path, SKIP_UNREADABLE))
        return
    if not path.is_file():
        # Sockets, FIFOs and device nodes: opening one can block forever.
        plan.skipped.append(Skipped(path, SKIP_NOT_REGULAR))
        return
    if wanted and path.suffix.lower() not in wanted:
        plan.skipped.append(Skipped(path, SKIP_SUFFIX))
        return
    if stat.st_size == 0:
        plan.skipped.append(Skipped(path, SKIP_EMPTY))
        return
    if stat.st_size > max_bytes:
        plan.skipped.append(Skipped(path, SKIP_TOO_LARGE))
        return
    plan.candidates.append(Candidate(path, stat.st_size))


async def run_import(
    pipeline: Any,
    user_id: str,
    plan: ImportPlan,
    *,
    on_progress: Callable[[int, int, Outcome], None] | None = None,
    stop_after: int | None = None,
) -> list[Outcome]:
    """Ingest every candidate, one at a time, reporting what happened to each.

    Sequential on purpose: the pipeline writes to one SQLite database, and the point of
    an import is that it finishes and can be resumed, not that it saturates the disk.

    One file failing must not end the run — a corpus of real files will contain
    something unreadable, and losing the other 8999 to it would be absurd. Failures are
    collected and reported; re-running retries them and skips everything that landed.

    `stop_after` считает ЗАГРУЖЕННЫЕ, а не просмотренные, и в этом вся разница. Партии
    обещаны в докстринге `plan_import` и в руководстве оператора, но одним потолком
    плана они не получались: второй запуск с тем же числом брал те же первые N файлов,
    все они отыгрывались как дубликаты, и человек, следующий рецепту из OPERATIONS.md,
    не продвигался дальше первой партии никогда. Дубликат — это признак, что здесь уже
    были; он не должен занимать место в партии.
    """
    outcomes: list[Outcome] = []
    total = len(plan.candidates)
    ingested = 0
    for index, candidate in enumerate(plan.candidates, start=1):
        outcome = await _ingest_one(pipeline, user_id, candidate)
        outcomes.append(outcome)
        if on_progress is not None:
            on_progress(index, total, outcome)
        if outcome.status == "ingested":
            ingested += 1
            if stop_after is not None and ingested >= stop_after:
                break
    return outcomes


async def _ingest_one(pipeline: Any, user_id: str, candidate: Candidate) -> Outcome:
    try:
        content = candidate.path.read_bytes()
    except OSError as exc:
        return Outcome(candidate.path, "failed", f"{type(exc).__name__}: {exc}")
    try:
        result = await pipeline.ingest_file(
            user_id,
            candidate.path,
            content,
            filename=candidate.path.name,
            # The absolute path is provenance, not identity: identity stays the content
            # hash so the run resumes and duplicates collapse. Passing the path as
            # source_ref instead would make an edited file a hard conflict.
            metadata={"import_source_path": str(candidate.path)},
            # Pointing at a folder is one action; it is not a judgement on each file.
            force_review=True,
        )
    except Exception as exc:  # one bad file must not end the run
        return Outcome(candidate.path, "failed", f"{type(exc).__name__}: {exc}")
    return Outcome(
        candidate.path,
        "duplicate" if result.get("idempotent_replay") else "ingested",
        raw_object_id=str(result.get("raw_object_id") or ""),
        inbox_id=str(result.get("inbox_id") or ""),
    )


def summarise(outcomes: list[Outcome]) -> dict[str, int]:
    counts = {"ingested": 0, "duplicate": 0, "failed": 0}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return counts
