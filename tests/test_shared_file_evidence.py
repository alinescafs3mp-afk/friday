from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from friday.evidence_bundle import CitationBinding, EvidenceBundle, EvidencePart, HierarchyEvidence
from friday.file_evidence import (
    FileBodyKind,
    FileEvidenceSet,
    FileEvidenceView,
    FileRegistrationKind,
)


def _view(identity: str = "1" * 64) -> FileEvidenceView:
    return FileEvidenceView(
        raw_id="raw_0123456789abcdef",
        source_identity_sha256=identity,
        registration=FileRegistrationKind.VALID,
        disk_verified=True,
        workspace_relative_path=None,
        workspace_sha256=None,
        workspace_source_sha256=None,
        body_kind=FileBodyKind.EXTRACTED,
        source_complete=True,
        projection_applied=False,
        projection_empty_no_match=False,
        source_readable=True,
        verification_eligible=True,
    )


def test_legacy_and_shared_file_evidence_imports_are_identical() -> None:
    import friday.agent_runtime as legacy

    assert legacy.FileBodyKind is FileBodyKind
    assert legacy.FileEvidenceSet is FileEvidenceSet
    assert legacy.FileEvidenceView is FileEvidenceView
    assert legacy.FileRegistrationKind is FileRegistrationKind


def test_file_evidence_set_identity_binds_order_cardinality_and_fields() -> None:
    first = _view("1" * 64)
    second = replace(first, raw_id="raw_fedcba9876543210", source_identity_sha256="2" * 64)
    evidence = FileEvidenceSet((first, second), expected_count=2)
    digest = evidence.identity_sha256()

    assert evidence.context_complete is True
    assert evidence.coverage_complete is True
    assert evidence.verification_complete is True
    assert FileEvidenceSet((second, first), expected_count=2).identity_sha256() != digest
    assert FileEvidenceSet((first, second), expected_count=3).identity_sha256() != digest
    assert FileEvidenceSet((replace(first, disk_verified=False), second), 2).identity_sha256() != digest
    with pytest.raises(ValueError, match="immutable tuple"):
        FileEvidenceSet([first], expected_count=1)  # type: ignore[arg-type]


def test_evidence_bundle_is_one_closed_projection_for_synthesis_and_verification() -> None:
    evidence = FileEvidenceSet((_view(),), expected_count=1)
    part = EvidencePart(
        label="A1",
        display_name="report.txt",
        media_type="text/plain",
        source_identity_sha256="1" * 64,
        text="Точное содержимое отчёта.",
    )
    bundle = EvidenceBundle(
        parts=(part,),
        citations=(CitationBinding("A1", "1" * 64),),
        file_evidence_set_sha256=evidence.identity_sha256(),
    )

    assert bundle.citation_labels == ("A1",)
    assert bundle.model_payload()["parts"][0]["text"] == part.text
    assert len(bundle.identity_sha256()) == 64
    with pytest.raises(ValueError, match="match"):
        EvidenceBundle(
            parts=(part,),
            citations=(CitationBinding("A1", "2" * 64),),
            file_evidence_set_sha256=evidence.identity_sha256(),
        )
    with pytest.raises(ValueError, match="foreign"):
        EvidenceBundle(
            parts=(part,),
            citations=(CitationBinding("A1", "1" * 64),),
            file_evidence_set_sha256=evidence.identity_sha256(),
            hierarchy=HierarchyEvidence("1" * 64, ("A2",), "3" * 64),
        )
    with pytest.raises(ValueError, match="immutable tuple"):
        EvidenceBundle(
            parts=[part],  # type: ignore[arg-type]
            citations=(CitationBinding("A1", "1" * 64),),
            file_evidence_set_sha256=evidence.identity_sha256(),
        )
    with pytest.raises(ValueError, match="immutable tuple"):
        HierarchyEvidence("1" * 64, ["A1"], "3" * 64)  # type: ignore[arg-type]


def test_shared_evidence_import_does_not_load_runtime_or_heavy_organs() -> None:
    root = Path(os.environ.get("FRIDAY_QUALITY_GATE_INSTALLED_SITE", Path(__file__).parents[1])).resolve()
    code = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import friday.file_evidence, friday.evidence_bundle
assert all(pathlib.Path(sys.modules[name].__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1])) for name in ('friday.file_evidence', 'friday.evidence_bundle'))
blocked = [name for name in sys.modules if name.startswith(('friday.agent_runtime', 'friday.ingestion', 'friday.orchestration', 'friday.retrieval'))]
print(json.dumps(blocked))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []
