"""Pure digest binding a selected archive source to its exact passage bytes."""

from __future__ import annotations

import hashlib
import json

from friday.retrieval.contracts import PassageRef, ResolvedSource


class ArchiveEvidenceSnapshotError(ValueError):
    """The selected evidence is outside the closed snapshot contract."""


def archive_selected_evidence_snapshot_sha256(
    resolved_source: ResolvedSource,
    passage_refs: tuple[PassageRef, ...],
    excerpts: tuple[str, ...],
) -> str:
    """Bind the source graph, exact locators and selected passage contents."""

    if (
        type(resolved_source) is not ResolvedSource
        or type(passage_refs) is not tuple
        or type(excerpts) is not tuple
        or not 1 <= len(passage_refs) <= 8
        or len(excerpts) != len(passage_refs)
        or any(type(item) is not PassageRef for item in passage_refs)
        or any(item.source_ref != resolved_source.source_ref for item in passage_refs)
        or any(type(item) is not str or not item for item in excerpts)
    ):
        raise ArchiveEvidenceSnapshotError("selected archive evidence snapshot is invalid")
    identities = tuple(item.to_private_json() for item in passage_refs)
    if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
        raise ArchiveEvidenceSnapshotError("selected archive evidence snapshot is not canonical")
    try:
        evidence = [
            {
                "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8", errors="strict")).hexdigest(),
                "passage_ref": passage_ref.to_private_payload(),
            }
            for passage_ref, excerpt in zip(passage_refs, excerpts, strict=True)
        ]
        material = json.dumps(
            {
                "evidence": evidence,
                "resolved_source": resolved_source.to_private_payload(),
                "schema": "friday.selected-archive-evidence-snapshot.private.v1",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except Exception:
        raise ArchiveEvidenceSnapshotError("selected archive evidence snapshot is invalid") from None
    return hashlib.sha256(material).hexdigest()


__all__ = [
    "ArchiveEvidenceSnapshotError",
    "archive_selected_evidence_snapshot_sha256",
]
