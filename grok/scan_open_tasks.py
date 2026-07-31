#!/usr/bin/env python3
"""Deterministic open-G-task scanner for the Grok task watcher.

Source of truth for *assignment* is the TASKS file text, not the LLM.
A heading is **open** unless it carries an explicit done marker.

A task is **not** finished until origin/main says so. Local WIP that already
marks «сделано» while origin still shows open = finish and push, do not idle.

Usage:
  python grok/scan_open_tasks.py                 # working tree grok/TASKS.md
  python grok/scan_open_tasks.py --path PATH
  python grok/scan_open_tasks.py --git-ref origin/main
  python grok/scan_open_tasks.py --watch-status  # effective action for the watcher
  python grok/scan_open_tasks.py --json

Exit codes: 0 on successful parse (even if open list empty).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO / "grok" / "TASKS.md"
STATE_PATH = REPO / "grok" / ".task_watch_state.json"

# ## G7 (#54). title — **сделано**
HEADING_RE = re.compile(
    r"^##\s+(G\d+)\s*(?:\(#(\d+)\))?\.\s*(.+?)\s*$",
    re.MULTILINE,
)

# Assigners sometimes drop the formal «## G15 (#n). …» shape and write prose:
#   # Новое … — G15: «ещё раз» …
#   ## G15: title
# Without this, the watcher idles while work is already on origin/main (G15 lag).
INFORMAL_HEADING_RE = re.compile(
    r"^#+\s+.*?\b(G\d+)\b(?:\s*\(#(\d+)\))?\s*[:.—–-]+\s*(.+?)\s*$",
    re.MULTILINE,
)

# Done markers on the heading line only (body «сделано» must not close a task).
DONE_RE = re.compile(
    r"(?i)(\*\*сделано\*\*|\*\*закрыто\*\*|\bDONE\b|\bCLOSED\b)",
)


def _read_working_tree(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _read_git_ref(ref: str, repo_path: str = "grok/TASKS.md") -> str:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{repo_path}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"git show {ref}:{repo_path} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _git_head() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return (proc.stdout or "").strip() or "unknown"


def _git_dirty() -> bool:
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return bool((proc.stdout or "").strip())


def parse_tasks(text: str) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    seen: set[str] = set()
    for match in HEADING_RE.finditer(text):
        gid = match.group(1)
        issue = match.group(2)
        rest = match.group(3).strip()
        done = bool(DONE_RE.search(match.group(0)))
        title = DONE_RE.sub("", rest).strip(" —-").strip()
        tasks.append(
            {
                "id": gid,
                "issue": int(issue) if issue else None,
                "title": title,
                "open": not done,
                "heading": match.group(0).strip(),
            }
        )
        seen.add(gid)
    # Informal assignments only fill gaps — formal ## G-heading always wins.
    for match in INFORMAL_HEADING_RE.finditer(text):
        gid = match.group(1)
        if gid in seen:
            continue
        issue = match.group(2)
        rest = match.group(3).strip()
        done = bool(DONE_RE.search(match.group(0)))
        title = DONE_RE.sub("", rest).strip(" —-").strip()
        if not title:
            continue
        tasks.append(
            {
                "id": gid,
                "issue": int(issue) if issue else None,
                "title": title,
                "open": not done,
                "heading": match.group(0).strip(),
            }
        )
        seen.add(gid)
    return tasks


def _open_report(text: str, source: str) -> dict[str, object]:
    tasks = parse_tasks(text)
    open_tasks = [t for t in tasks if t["open"]]
    return {
        "source": source,
        "total": len(tasks),
        "open_count": len(open_tasks),
        "open_ids": [str(t["id"]) for t in open_tasks],
        "open_titles": {str(t["id"]): str(t["title"]) for t in open_tasks},
        "all": tasks,
    }


def _load_state() -> dict[str, object]:
    if not STATE_PATH.is_file():
        return {
            "checked_at": None,
            "commit": None,
            "open_ids": [],
            "open_titles": {},
            "in_progress": [],
            "completed_by_watch": [],
            "last_action": None,
            "last_error": None,
        }
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("open_ids", [])
    data.setdefault("open_titles", {})
    data.setdefault("in_progress", [])
    data.setdefault("completed_by_watch", [])
    return data


def _write_state(state: dict[str, object]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def watch_status(*, write_state: bool = True, git_ref: str = "origin/main") -> dict[str, object]:
    """Compute what the watcher must do this fire.

    Rules:
    - Assignment truth = open headings on origin/main (after fetch, if any).
    - Worktree open headings are also work (new local assignment).
    - origin open + worktree closed + dirty tree = unfinished WIP → finish_and_push.
    - A task is only idle when origin and worktree both mark it done.
    """
    prev = _load_state()
    origin = _open_report(_read_git_ref(git_ref), f"git:{git_ref}:grok/TASKS.md")
    local = _open_report(_read_working_tree(DEFAULT_PATH), str(DEFAULT_PATH))
    dirty = _git_dirty()
    head = _git_head()

    origin_open = set(origin["open_ids"])  # type: ignore[arg-type]
    local_open = set(local["open_ids"])  # type: ignore[arg-type]
    titles: dict[str, str] = {}
    titles.update(origin.get("open_titles") or {})  # type: ignore[arg-type]
    titles.update(local.get("open_titles") or {})  # type: ignore[arg-type]

    # WIP: marked done locally while origin still assigns it open.
    unfinished_wip = sorted(origin_open - local_open)
    # Still open somewhere → must execute (implement or continue).
    must_work = sorted(origin_open | local_open)

    if unfinished_wip and dirty:
        action = "finish_and_push"
        action_ids = unfinished_wip
        reason = (
            "origin still has open tasks that worktree already marks done; "
            "dirty tree looks like unfinished push"
        )
    elif must_work and dirty:
        # Partial implement left the tree dirty — do not idle, do not re-plan
        # from zero: finish the WIP, gate, push.
        action = "continue_wip"
        action_ids = must_work
        reason = "open G-tasks and dirty worktree — finish in-flight work and push"
    elif must_work:
        action = "implement"
        action_ids = must_work
        reason = "open G-tasks present on origin and/or worktree"
    else:
        action = "idle"
        action_ids = []
        reason = "no open G-tasks on origin or worktree"

    prev_open = set(prev.get("open_ids") or [])
    newly_seen = sorted(set(must_work) - prev_open)

    now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    state = {
        "checked_at": now,
        "commit": head,
        "git_ref": git_ref,
        "open_ids": must_work,
        "open_titles": {k: titles[k] for k in must_work if k in titles},
        "origin_open_ids": sorted(origin_open),
        "local_open_ids": sorted(local_open),
        "unfinished_wip_ids": unfinished_wip,
        "dirty": dirty,
        "newly_seen": newly_seen,
        "action": action,
        "action_ids": action_ids,
        "reason": reason,
        "in_progress": list(prev.get("in_progress") or []),
        "completed_by_watch": list(prev.get("completed_by_watch") or []),
        "last_action": action,
        "last_error": None,
    }
    if write_state:
        _write_state(state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=None, help="TASKS.md path (default: grok/TASKS.md)")
    parser.add_argument(
        "--git-ref",
        default=None,
        help="Read grok/TASKS.md from this git ref (e.g. origin/main) instead of working tree",
    )
    parser.add_argument(
        "--watch-status",
        action="store_true",
        help="Effective watcher action (origin ∪ local ∪ unfinished WIP); updates state file",
    )
    parser.add_argument(
        "--no-write-state",
        action="store_true",
        help="With --watch-status: print only, do not write .task_watch_state.json",
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    args = parser.parse_args(argv)

    if args.watch_status:
        report = watch_status(
            write_state=not args.no_write_state,
            git_ref=args.git_ref or "origin/main",
        )
        if args.json:
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(
                f"action={report['action']} "
                f"action_ids={','.join(report['action_ids']) or '-'} "
                f"origin_open={','.join(report['origin_open_ids']) or '-'} "
                f"local_open={','.join(report['local_open_ids']) or '-'} "
                f"dirty={report['dirty']} commit={report['commit']}"
            )
            print(f"reason={report['reason']}")
            for gid in report["action_ids"]:
                title = (report.get("open_titles") or {}).get(gid, "")
                print(f"  DO {gid} {title}")
        return 0

    source: str
    if args.git_ref:
        text = _read_git_ref(args.git_ref)
        source = f"git:{args.git_ref}:grok/TASKS.md"
    else:
        path = args.path or DEFAULT_PATH
        text = _read_working_tree(path)
        source = str(path)

    report = _open_report(text, source)

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"open_count={report['open_count']} open_ids={','.join(report['open_ids']) or '-'}")
        for t in report["all"]:
            if not t["open"]:
                continue
            issue = f"#{t['issue']}" if t["issue"] else ""
            print(f"  OPEN {t['id']} {issue} {t['title']}")
        if not report["open_ids"]:
            print("  (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
