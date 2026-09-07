"""Independent, frozen behavior oracles for a bounded Coding create family.

The model's own tests are not this proof.  Cases are Friday-owned and cannot
be deleted or weakened by a generated project.  This is not a safety
certification and is not user-requested EXECUTE.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

ORACLE_ID = "csv_summary_v1"
ORACLE_SCHEMA = "friday.coding-behavior-oracle.v1"
CLI_CONTRACT = (
    "The program is a Python 3 standard-library CLI at main.py. "
    "It reads UTF-8 CSV from stdin. A header row is required and must include "
    "an amount column. Write only these two stdout lines, in order: "
    "rows=<non-negative integer of data rows> and sum=<integer sum of amount>. "
    "Exit 0 on success. Exit 2 when stdin is empty or has no header. "
    "Ignore unknown columns. Do not print other stdout. "
    "Do not invent execution results or copy a scaffold."
)
_CSV_RE = re.compile(r"(?i)(?:\bcsv\b|\.csv\b)")
_SUMMARY_RE = re.compile(r"(?i)(?:summar(?:y|ize)|сводк|итог(?:ов)?(?:ая|ую)?)")


class CodingBehaviorOracleState(StrEnum):
    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingBehaviorOracleReason(StrEnum):
    NO_FAMILY = "no_family"
    ADMITTED = "admitted"


@dataclass(frozen=True, slots=True)
class CodingBehaviorOracleCaseV1:
    case_id: str
    argv: tuple[str, ...]
    stdin: str
    stdout: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class CodingBehaviorOracleV1:
    """Closed family admission.  Cases never come from the model."""

    state: CodingBehaviorOracleState
    reason: CodingBehaviorOracleReason
    oracle_id: str | None
    oracle_sha256: str | None
    cases: tuple[CodingBehaviorOracleCaseV1, ...]
    cli_contract: str


CSV_SUMMARY_CASES: tuple[CodingBehaviorOracleCaseV1, ...] = (
    CodingBehaviorOracleCaseV1("two_rows", (), "name,amount\nalice,10\nbob,20\n", "rows=2\nsum=30\n", 0),
    CodingBehaviorOracleCaseV1("header_only", (), "name,amount\n", "rows=0\nsum=0\n", 0),
    CodingBehaviorOracleCaseV1("extra_column", (), "name,amount,note\nalice,10,x\n", "rows=1\nsum=10\n", 0),
    CodingBehaviorOracleCaseV1("empty", (), "", "", 2),
)


def _cases_payload(cases: tuple[CodingBehaviorOracleCaseV1, ...]) -> list[dict[str, object]]:
    return [
        {
            "case_id": case.case_id,
            "argv": list(case.argv),
            "exit": case.exit_code,
            "stdin": case.stdin,
            "stdout": case.stdout,
        }
        for case in cases
    ]


def oracle_sha256(oracle_id: str, cases: tuple[CodingBehaviorOracleCaseV1, ...]) -> str:
    payload = {
        "cli_contract": CLI_CONTRACT,
        "cases": _cases_payload(cases),
        "oracle_id": oracle_id,
        "schema": ORACLE_SCHEMA,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    ).hexdigest()


def admit_coding_behavior_oracle(message: str) -> CodingBehaviorOracleV1:
    """Admit only the frozen CSV-summary CLI family.  Other creates stay unverified."""

    text = message or ""
    if not text.strip() or _CSV_RE.search(text) is None or _SUMMARY_RE.search(text) is None:
        return CodingBehaviorOracleV1(
            CodingBehaviorOracleState.EMPTY,
            CodingBehaviorOracleReason.NO_FAMILY,
            None,
            None,
            (),
            "",
        )
    digest = oracle_sha256(ORACLE_ID, CSV_SUMMARY_CASES)
    return CodingBehaviorOracleV1(
        CodingBehaviorOracleState.ADMITTED,
        CodingBehaviorOracleReason.ADMITTED,
        ORACLE_ID,
        digest,
        CSV_SUMMARY_CASES,
        CLI_CONTRACT,
    )


def oracle_worker_program() -> str:
    """Code-owned ORACLE program.  Cases are frozen here, not read from the project."""

    cases = json.dumps(_cases_payload(CSV_SUMMARY_CASES), ensure_ascii=True, separators=(",", ":"))
    return (
        "import json,pathlib,subprocess,sys\n"
        f"CASES=json.loads({cases!r})\n"
        "root=pathlib.Path(sys.argv[1])\n"
        "if not root.is_dir():\n"
        "    raise SystemExit(3)\n"
        "root=root.resolve()\n"
        "main=root/'main.py'\n"
        "if not main.is_file():\n"
        "    raise SystemExit(3)\n"
        "try:\n"
        "    main.resolve().relative_to(root)\n"
        "except ValueError:\n"
        "    raise SystemExit(4)\n"
        "for case in CASES:\n"
        "    if type(case) is not dict:\n"
        "        raise SystemExit(3)\n"
        "    argv=case.get('argv')\n"
        "    stdin=case.get('stdin')\n"
        "    stdout=case.get('stdout')\n"
        "    ident=case.get('case_id')\n"
        "    exit_code=case.get('exit')\n"
        "    if (\n"
        "        type(argv) is not list or any(type(part) is not str for part in argv)\n"
        "        or type(stdin) is not str or type(stdout) is not str\n"
        "        or type(ident) is not str or type(exit_code) is not int\n"
        "    ):\n"
        "        raise SystemExit(3)\n"
        "    try:\n"
        "        proc=subprocess.run(\n"
        "            [sys.executable,'-I',str(main),*argv],\n"
        "            input=stdin.encode(),\n"
        "            stdout=subprocess.PIPE,\n"
        "            stderr=subprocess.DEVNULL,\n"
        "            cwd=str(root),\n"
        "            timeout=5,\n"
        "            env={'PATH':'/usr/bin:/bin','HOME':'/tmp','LANG':'C','PYTHONSAFEPATH':'1'},\n"
        "            check=False,\n"
        "        )\n"
        "    except subprocess.TimeoutExpired:\n"
        "        sys.stderr.write(ident+'\\ntimeout\\n')\n"
        "        raise SystemExit(1)\n"
        "    if proc.returncode!=exit_code or proc.stdout!=stdout.encode():\n"
        "        sys.stderr.write(ident+'\\n')\n"
        "        sys.stderr.write(str(proc.returncode)+'\\n')\n"
        "        sys.stderr.buffer.write(proc.stdout[:512])\n"
        "        raise SystemExit(1)\n"
        "raise SystemExit(0)\n"
    )
