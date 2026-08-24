#!/usr/bin/env python3
"""Generate the fixed, synthetic calibration corpus for the internal NVFP4 cast."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = "friday.secondary-brain.calibration.v1"
CALIBRATION_ROWS = 256

_NAMES = ("Артемьев", "Орлова", "Соколов", "Miller", "Chen", "Garcia", "Khan", "Nakamura")
_PROJECTS = ("Север", "Маяк", "Кедр", "Aurora", "Atlas", "Nimbus", "Orion", "Vector")
_FORMATS = ("DOCX", "XLSX", "PDF", "ODT", "PPTX", "CSV", "Markdown", "JSON")
_PLACES = ("Москва", "Казань", "Омск", "London", "Berlin", "Tokyo", "Toronto", "Delhi")


def _record(index: int) -> str:
    """Return one long, deterministic bilingual sample with diverse document syntax."""

    name = _NAMES[index % len(_NAMES)]
    project = _PROJECTS[(index * 3) % len(_PROJECTS)]
    file_format = _FORMATS[(index * 5) % len(_FORMATS)]
    place = _PLACES[(index * 7) % len(_PLACES)]
    day = 1 + index % 28
    month = 1 + (index // 7) % 12
    amount = 1_000 + index * 37
    code = f"FR-{index:04d}-{(index * 17) % 997:03d}"
    facts = [
        f"Запись {code}: проект «{project}», ответственный {name}, город {place}.",
        f"Дата документа: 2026-{month:02d}-{day:02d}; бюджет: {amount:,} RUB; формат: {file_format}.",
        "Задача: извлечь факты точно, не смешивать инструкцию с данными и не придумывать поля.",
        f"Document {code}: project {project}; owner {name}; location {place}; source format {file_format}.",
        "Keep dates, identifiers, filenames, citations, Unicode, negation, and units unchanged.",
        f'JSON example: {{"id":"{code}","amount":{amount},"approved":false,"items":[1,2,3]}}.',
        f"Таблица: | {code} | {day:02d}.{month:02d}.2026 | {amount} ₽ | НЕ утверждено |.",
        f"Path: Projects/{project}/{code} — финал.{file_format.casefold()}; citation [SRC-{index:03d}].",
        "Контраст: черновик не утверждён; финальная версия существует, но подпись отсутствует.",
        "Code: if verified and not cancelled: publish(summary); else: preserve(source).",
        "Symbols: № § % ± → ≠; decimals 17.25 and -0.50; email example@example.invalid.",
        "Краткий итог должен опираться только на перечисленные факты. End of source block.",
    ]
    # A full calibration window without importing mutable or operator-owned data.
    rotations = [facts[offset:] + facts[:offset] for offset in range(0, len(facts), 3)]
    return "\n".join(line for block in rotations for line in block)


def calibration_rows() -> Iterator[dict[str, str]]:
    for index in range(CALIBRATION_ROWS):
        yield {"text": _record(index)}


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _exclusive_write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def generate(output: Path, manifest: Path) -> dict[str, Any]:
    """Write a new corpus and content-free provenance manifest without overwriting."""

    rows = list(calibration_rows())
    corpus = b"".join(_canonical_json(row) for row in rows)
    corpus_sha256 = hashlib.sha256(corpus).hexdigest()
    generator_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "observed_unaccepted",
        "rows": len(rows),
        "bytes": len(corpus),
        "sha256": corpus_sha256,
        "generator_sha256": generator_sha256,
        "synthetic_only": True,
        "operator_data_present": False,
    }
    _exclusive_write(output, corpus, mode=0o444)
    try:
        _exclusive_write(manifest, _canonical_json(report), mode=0o444)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.manifest.resolve():
        parser.error("output and manifest must be distinct")
    print(json.dumps(generate(args.output, args.manifest), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
