"""Prompt-to-small-project observer for Coding Mode.

Normalize, plan, scaffold and admit, then write bounded relative files into an
already-admitted isolated workspace.  Never executes the generated program.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from friday.orchestration.coding_create_admission import (
    CodingCreateAdmissionState,
    CodingCreateAdmissionV1,
    build_coding_create_admission,
)
from friday.orchestration.coding_implementation_plan import build_coding_implementation_plan
from friday.orchestration.coding_project_identity import (
    CodingProjectIdentityState,
    CodingProjectIdentityV1,
    build_coding_project_identity,
)
from friday.orchestration.coding_project_isolation_admission import (
    CodingProjectIsolationAdmissionState,
    build_coding_project_isolation_admission,
)
from friday.orchestration.coding_project_scaffold import build_coding_project_scaffold
from friday.orchestration.coding_prompt_normalization import (
    CodingPromptNormalizationState,
    build_coding_prompt_normalization,
)
from friday.private_fs import ensure_private_directory, prepare_private_file, restrict_private_file

_CREATE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:create|scaffold|generate)\b"
    r"|создай|напиши|сделай\s+проект|новый\s+проект"
    r")"
)
_LANG = (
    (re.compile(r"(?i)\b(?:javascript|js)\b"), "js", "main.js"),
    (re.compile(r"(?i)\b(?:typescript|ts)\b"), "ts", "main.ts"),
    (re.compile(r"(?i)\brust\b"), "rust", "main.rs"),
    (re.compile(r"(?i)\bgo(?:lang)?\b"), "go", "main.go"),
    (re.compile(r"(?i)\b(?:python|py)\b"), "python", "main.py"),
)


class CodingCreateObserveState(StrEnum):
    EMPTY = "empty"
    WRITTEN = "written"
    BLOCKED = "blocked"


class CodingCreateObserveReason(StrEnum):
    NO_PROMPT = "no_prompt"
    WRITTEN = "written"
    NOT_CREATE = "not_create"
    ADMISSION_NOT_GRANTED = "admission_not_granted"
    WORKER_NOT_ADMITTED = "worker_not_admitted"
    ISOLATION_NOT_GRANTED = "isolation_not_granted"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True, slots=True)
class CodingCreateObserveV1:
    """Closed create observation.  Untrusted execute is never attempted."""

    state: CodingCreateObserveState
    reason: CodingCreateObserveReason
    admission: CodingCreateAdmissionV1
    identity: CodingProjectIdentityV1
    written_count: int
    files: tuple[str, ...]
    untrusted_execute: bool = False


def create_requested(message: str, *, has_members: bool) -> bool:
    """True only for a prompt-to-project request without uploaded members."""

    if has_members or not (message or "").strip():
        return False
    return _CREATE_RE.search(message) is not None


def _language(message: str) -> tuple[str | None, str]:
    for pattern, hint, filename in _LANG:
        if pattern.search(message) is not None:
            return hint, filename
    return "python", "main.py"


def _bodies(title: str, goal: str, source_name: str) -> dict[str, bytes]:
    readme = f"# {title}\n\n{goal}\n\nBounded Coding Mode scaffold. Generated programs are not executed.\n"
    files = {"README.md": readme.encode()}
    if source_name == "main.py":
        files["main.py"] = (
            '"""Bounded Coding Mode scaffold. This program is not executed."""\n\n'
            f"TITLE = {title!r}\n"
            f"GOAL = {goal!r}\n\n\n"
            "def main() -> None:\n"
            "    return None\n\n\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        ).encode()
        files["test_main.py"] = (
            b"import unittest\n\n"
            b"from main import main\n\n\n"
            b"class TestScaffold(unittest.TestCase):\n"
            b"    def test_main_returns_none(self) -> None:\n"
            b"        self.assertIsNone(main())\n"
        )
        return files
    files[source_name] = (
        f"// Bounded Coding Mode scaffold. This program is not executed.\n"
        f"// title: {title}\n"
        f"// goal: {goal}\n"
    ).encode()
    return files


def _empty(turn_id: str, reason: CodingCreateObserveReason) -> CodingCreateObserveV1:
    return CodingCreateObserveV1(
        CodingCreateObserveState.EMPTY,
        reason,
        build_coding_create_admission(f"{turn_id}-create", turn_id),
        build_coding_project_identity(f"{turn_id}-ident", turn_id),
        0,
        (),
        False,
    )


def _blocked(
    turn_id: str,
    reason: CodingCreateObserveReason,
    *,
    admission: CodingCreateAdmissionV1 | None = None,
    identity: CodingProjectIdentityV1 | None = None,
) -> CodingCreateObserveV1:
    return CodingCreateObserveV1(
        CodingCreateObserveState.BLOCKED,
        reason,
        admission if admission is not None else build_coding_create_admission(f"{turn_id}-create", turn_id),
        identity if identity is not None else build_coding_project_identity(f"{turn_id}-ident", turn_id),
        0,
        (),
        False,
    )


def observe_coding_create(
    *,
    turn_id: str,
    project_id: str,
    message: str,
    workspace: Path,
    worker_admitted: bool,
    has_members: bool,
) -> CodingCreateObserveV1:
    """Admit a bounded scaffold and write it.  Never run the generated program."""

    if not create_requested(message, has_members=has_members):
        return _empty(turn_id, CodingCreateObserveReason.NOT_CREATE)
    goal = (message or "").strip()[:500]
    if not goal:
        return _empty(turn_id, CodingCreateObserveReason.NO_PROMPT)
    hint, source_name = _language(goal)
    prompt = build_coding_prompt_normalization(
        f"{turn_id}-prompt",
        turn_id,
        goal=goal,
        language_hint=hint,
    )
    if (
        prompt.prompt is not CodingPromptNormalizationState.NORMALIZED
        or prompt.title is None
        or prompt.goal is None
    ):
        admission = build_coding_create_admission(
            f"{turn_id}-create",
            turn_id,
            identity=build_coding_project_identity(
                f"{turn_id}-ident", turn_id, project_id=project_id, revision_selector="rev-blocked"
            ),
            prompt=prompt,
            plan=build_coding_implementation_plan(f"{turn_id}-plan", turn_id),
            scaffold=build_coding_project_scaffold(f"{turn_id}-scaffold", turn_id),
        )
        return _blocked(
            turn_id,
            CodingCreateObserveReason.ADMISSION_NOT_GRANTED,
            admission=admission,
            identity=build_coding_project_identity(f"{turn_id}-ident", turn_id),
        )
    bodies = _bodies(prompt.title, prompt.goal, source_name)
    paths = tuple(sorted(bodies))
    steps = tuple(
        {
            "step_id": re.sub(r"[^a-z0-9]+", "_", path.rsplit(".", 1)[0].casefold()).strip("_") or "file",
            "action": "create",
            "target_path": path,
        }
        for path in paths
    )
    seen: dict[str, int] = {}
    unique_steps: list[dict[str, str]] = []
    for step in steps:
        key = step["step_id"]
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            step = {**step, "step_id": f"{key}_{seen[key]}"}
        unique_steps.append(step)
    revision = hashlib.sha256(
        json.dumps(
            {path: hashlib.sha256(bodies[path]).hexdigest() for path in paths}, sort_keys=True
        ).encode()
    ).hexdigest()
    identity = build_coding_project_identity(
        f"{turn_id}-ident",
        turn_id,
        project_id=project_id,
        revision_selector=revision,
    )
    plan = build_coding_implementation_plan(f"{turn_id}-plan", turn_id, unique_steps)
    scaffold = build_coding_project_scaffold(f"{turn_id}-scaffold", turn_id, list(paths))
    admission = build_coding_create_admission(
        f"{turn_id}-create",
        turn_id,
        identity=identity,
        prompt=prompt,
        plan=plan,
        scaffold=scaffold,
    )
    if admission.admission is not CodingCreateAdmissionState.ADMITTED:
        return _blocked(
            turn_id,
            CodingCreateObserveReason.ADMISSION_NOT_GRANTED,
            admission=admission,
            identity=identity
            if identity.identity is CodingProjectIdentityState.IDENTIFIED
            else build_coding_project_identity(f"{turn_id}-ident", turn_id),
        )
    if not worker_admitted:
        return _blocked(
            turn_id,
            CodingCreateObserveReason.WORKER_NOT_ADMITTED,
            admission=admission,
            identity=identity,
        )
    try:
        ensure_private_directory(workspace)
        root = str(workspace.resolve())
    except (OSError, ValueError):
        return _blocked(
            turn_id, CodingCreateObserveReason.WRITE_FAILED, admission=admission, identity=identity
        )
    pending: list[tuple[Path, bytes]] = []
    for path in paths:
        isolation = build_coding_project_isolation_admission(
            f"{turn_id}-i-{path.replace('/', '-').replace('.', '-')}",
            turn_id,
            project_root=root,
            destination=path,
        )
        if isolation.admission is not CodingProjectIsolationAdmissionState.ADMITTED:
            return _blocked(
                turn_id,
                CodingCreateObserveReason.ISOLATION_NOT_GRANTED,
                admission=admission,
                identity=identity,
            )
        dest = (workspace / path).resolve()
        try:
            dest.relative_to(workspace.resolve())
        except ValueError:
            return _blocked(
                turn_id,
                CodingCreateObserveReason.ISOLATION_NOT_GRANTED,
                admission=admission,
                identity=identity,
            )
        pending.append((dest, bodies[path]))
    try:
        for dest, body in pending:
            ensure_private_directory(dest.parent)
            prepare_private_file(dest)
            dest.write_bytes(body)
            restrict_private_file(dest)
    except (OSError, ValueError):
        return _blocked(
            turn_id, CodingCreateObserveReason.WRITE_FAILED, admission=admission, identity=identity
        )
    return CodingCreateObserveV1(
        CodingCreateObserveState.WRITTEN,
        CodingCreateObserveReason.WRITTEN,
        admission,
        identity,
        len(pending),
        paths,
        False,
    )
