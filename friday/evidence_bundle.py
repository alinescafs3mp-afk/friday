"""Closed, model-visible evidence assembled from authorized source views."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

_MAX_PARTS = 12
_MAX_PART_CHARS = 48_000
_MAX_TOTAL_CHARS = 120_000
_LABEL_RE = re.compile(r"A[1-9][0-9]{0,2}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _valid_utf8(value: str, *, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc


@dataclass(frozen=True, slots=True)
class EvidencePart:
    label: str
    display_name: str
    media_type: str
    source_identity_sha256: str
    text: str

    def __post_init__(self) -> None:
        if _LABEL_RE.fullmatch(self.label) is None:
            raise ValueError("evidence label has an invalid shape")
        if _SHA256_RE.fullmatch(self.source_identity_sha256) is None:
            raise ValueError("evidence source identity is not a SHA-256 digest")
        _valid_utf8(self.display_name, label="evidence display name")
        _valid_utf8(self.media_type, label="evidence media type")
        _valid_utf8(self.text, label="evidence text")
        if not self.display_name or len(self.display_name) > 180:
            raise ValueError("evidence display name has an invalid length")
        if len(self.media_type) > 120 or len(self.text) > _MAX_PART_CHARS:
            raise ValueError("evidence part exceeds its bounded projection")

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def identity_payload(self) -> dict[str, object]:
        return {
            "display_name": self.display_name,
            "label": self.label,
            "media_type": self.media_type,
            "source_identity_sha256": self.source_identity_sha256,
            "text_sha256": self.text_sha256,
        }

    def model_payload(self) -> dict[str, object]:
        return {
            "display_name": self.display_name,
            "label": self.label,
            "media_type": self.media_type,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class CitationBinding:
    label: str
    source_identity_sha256: str

    def __post_init__(self) -> None:
        if _LABEL_RE.fullmatch(self.label) is None:
            raise ValueError("citation label has an invalid shape")
        if _SHA256_RE.fullmatch(self.source_identity_sha256) is None:
            raise ValueError("citation source identity is not a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class HierarchyEvidence:
    source_identity_sha256: str
    ordered_part_labels: tuple[str, ...]
    hierarchy_sha256: str

    def __post_init__(self) -> None:
        if (
            _SHA256_RE.fullmatch(self.source_identity_sha256) is None
            or _SHA256_RE.fullmatch(self.hierarchy_sha256) is None
        ):
            raise ValueError("hierarchy evidence digest is invalid")
        if not isinstance(self.ordered_part_labels, tuple):
            raise ValueError("hierarchy evidence labels must be an immutable tuple")
        if len(set(self.ordered_part_labels)) != len(self.ordered_part_labels):
            raise ValueError("hierarchy evidence labels must be unique")
        if not self.ordered_part_labels or any(
            _LABEL_RE.fullmatch(label) is None for label in self.ordered_part_labels
        ):
            raise ValueError("hierarchy evidence labels are invalid")


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    parts: tuple[EvidencePart, ...]
    citations: tuple[CitationBinding, ...]
    file_evidence_set_sha256: str
    hierarchy: HierarchyEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.parts, tuple) or any(type(part) is not EvidencePart for part in self.parts):
            raise ValueError("evidence parts must be an immutable tuple of exact parts")
        if not isinstance(self.citations, tuple) or any(
            type(binding) is not CitationBinding for binding in self.citations
        ):
            raise ValueError("citation bindings must be an immutable tuple of exact bindings")
        if not 1 <= len(self.parts) <= _MAX_PARTS:
            raise ValueError("evidence bundle cardinality is outside 1..12")
        if _SHA256_RE.fullmatch(self.file_evidence_set_sha256) is None:
            raise ValueError("file evidence set identity is invalid")
        labels = tuple(part.label for part in self.parts)
        if len(set(labels)) != len(labels):
            raise ValueError("evidence part labels must be unique")
        if sum(len(part.text) for part in self.parts) > _MAX_TOTAL_CHARS:
            raise ValueError("evidence bundle exceeds its total text budget")
        bindings = {item.label: item.source_identity_sha256 for item in self.citations}
        if len(bindings) != len(self.citations) or set(bindings) != set(labels):
            raise ValueError("citation bindings must cover each evidence part exactly once")
        if tuple(item.label for item in self.citations) != labels:
            raise ValueError("citation binding order must match evidence part order")
        if any(bindings[part.label] != part.source_identity_sha256 for part in self.parts):
            raise ValueError("citation binding does not match its evidence source")
        if self.hierarchy is not None:
            if set(self.hierarchy.ordered_part_labels) - set(labels):
                raise ValueError("hierarchy refers to a foreign evidence label")
            matching_sources = {
                part.source_identity_sha256
                for part in self.parts
                if part.label in self.hierarchy.ordered_part_labels
            }
            if matching_sources != {self.hierarchy.source_identity_sha256}:
                raise ValueError("hierarchy source does not match its evidence parts")

    @property
    def citation_labels(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.citations)

    def identity_sha256(self) -> str:
        payload: dict[str, object] = {
            "citations": [
                {"label": item.label, "source_identity_sha256": item.source_identity_sha256}
                for item in self.citations
            ],
            "file_evidence_set_sha256": self.file_evidence_set_sha256,
            "parts": [part.identity_payload() for part in self.parts],
            "schema": "friday.evidence-bundle.v1",
        }
        if self.hierarchy is not None:
            payload["hierarchy"] = {
                "hierarchy_sha256": self.hierarchy.hierarchy_sha256,
                "ordered_part_labels": list(self.hierarchy.ordered_part_labels),
                "source_identity_sha256": self.hierarchy.source_identity_sha256,
            }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def model_payload(self) -> dict[str, object]:
        return {
            "schema": "friday.evidence-bundle.v1",
            "parts": [part.model_payload() for part in self.parts],
        }


__all__ = [
    "CitationBinding",
    "EvidenceBundle",
    "EvidencePart",
    "HierarchyEvidence",
]
