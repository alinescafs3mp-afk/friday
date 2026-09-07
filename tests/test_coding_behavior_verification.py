from __future__ import annotations

import json
from pathlib import Path

from friday.organs.coding.behavior_oracle import admit_coding_behavior_oracle
from friday.organs.coding.verify import (
    CodingBehaviorVerificationReason,
    CodingBehaviorVerificationState,
    observe_coding_behavior_verification,
    verify_creation_payload,
)
from friday.organs.coding.worker_boundary import default_coding_worker_boundary
from friday.organs.coding.worker_spawn import compose_coding_worker_admission, spawn_coding_worker
from friday.organs.coding.workspace_io import publish_members

CSV_TASK = "создай python cli который читает csv из stdin и печатает сводку rows и sum колонки amount"
CORRECT = {
    "main.py": (
        "import csv, io, sys\n"
        "def main() -> None:\n"
        "    text = sys.stdin.read()\n"
        "    if text == '':\n"
        "        raise SystemExit(2)\n"
        "    reader = csv.DictReader(io.StringIO(text))\n"
        "    if reader.fieldnames is None:\n"
        "        raise SystemExit(2)\n"
        "    total = 0\n"
        "    count = 0\n"
        "    for row in reader:\n"
        "        count += 1\n"
        "        total += int(row['amount'])\n"
        "    sys.stdout.write(f'rows={count}\\nsum={total}\\n')\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    ),
    "README.md": "# CSV summary\n",
}
WRONG = {
    "main.py": (
        "def main() -> None:\n"
        "    print('rows=0')\n"
        "    print('sum=0')\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    ),
    "test_main.py": (
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self) -> None:\n        self.assertTrue(True)\n"
    ),
    "README.md": "# wrong\n",
}


def _boundary(tmp_path: Path):
    return default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home" / "data" / "state"),
        worker_root=str(tmp_path / "friday-coding-worker"),
        workspace_path="work/operation.1",
        export_path="out/operation.1",
    )


def test_correct_cli_is_verified_inside_the_proved_tree(tmp_path: Path) -> None:
    oracle = admit_coding_behavior_oracle(CSV_TASK)
    boundary = _boundary(tmp_path)
    admission = compose_coding_worker_admission(
        admission_id="admission.1",
        authenticated_turn_id="turn.1",
        worker_id="worker.1",
        operation_id="operation.1",
        project_id="project.1",
        revision_selector="a" * 64,
        boundary=boundary,
    )
    spawn = spawn_coding_worker(admission, boundary)
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    publish_members(workspace, [(name, text.encode()) for name, text in CORRECT.items()])
    result = observe_coding_behavior_verification(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        oracle=oracle,
        workspace=workspace,
    )
    assert spawn.probe == "confirmed"
    assert result.state is CodingBehaviorVerificationState.VERIFIED
    assert result.reason is CodingBehaviorVerificationReason.ORACLE_OK
    assert result.untrusted_execute is True
    assert result.oracle_id == "csv_summary_v1"


def test_wrong_cli_fails_even_when_project_unittests_pass(tmp_path: Path) -> None:
    oracle = admit_coding_behavior_oracle(CSV_TASK)
    payload = json.dumps({"files": WRONG})
    result = verify_creation_payload(
        payload=payload,
        oracle=oracle,
        worker_boundary=_boundary(tmp_path),
    )
    assert result.state is CodingBehaviorVerificationState.BLOCKED
    assert result.reason is CodingBehaviorVerificationReason.ORACLE_FAILED
    assert result.untrusted_execute is True
    assert result.failed_case == "two_rows"


def test_unproven_tree_does_not_run_the_oracle(tmp_path: Path, monkeypatch) -> None:
    import friday.organs.coding.worker_cgroup as coding_tree

    def forbidden(*args, **kwargs):
        raise AssertionError("oracle started a tree without proof")

    monkeypatch.setattr(coding_tree, "coding_tree_enforcement_available", lambda: False)
    monkeypatch.setattr(coding_tree, "_allocate", forbidden)
    oracle = admit_coding_behavior_oracle(CSV_TASK)
    result = verify_creation_payload(
        payload=json.dumps({"files": CORRECT}),
        oracle=oracle,
        worker_boundary=_boundary(tmp_path),
    )
    assert result.state is CodingBehaviorVerificationState.BLOCKED
    assert result.reason is CodingBehaviorVerificationReason.RESOURCE_ENFORCEMENT_UNAVAILABLE
    assert result.untrusted_execute is False
