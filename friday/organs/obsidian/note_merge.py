"""Explicit preserve-both conflict previews for synchronized Markdown notes."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass


class NoteMergeError(ValueError):
    """Conflict material is invalid or exceeds the merge boundary."""


@dataclass(frozen=True, slots=True)
class ConflictMergePreview:
    canonical_revision: str
    conflict_revision: str
    unified_diff: str
    merged_content: str
    identical: bool


def build_preserve_both_preview(
    canonical: str,
    conflict: str,
    *,
    canonical_label: str = "Friday version",
    conflict_label: str = "Android conflict version",
) -> ConflictMergePreview:
    """Produce a non-mutating preview that contains every byte of both sides."""

    left = _content(canonical, "canonical")
    right = _content(conflict, "conflict")
    left_label = _label(canonical_label)
    right_label = _label(conflict_label)
    left_revision = hashlib.sha256(left.encode("utf-8")).hexdigest()
    right_revision = hashlib.sha256(right.encode("utf-8")).hexdigest()
    identical = left == right
    diff = "".join(
        difflib.unified_diff(
            left.splitlines(keepends=True),
            right.splitlines(keepends=True),
            fromfile=left_label,
            tofile=right_label,
            n=3,
        )
    )
    if identical:
        merged = left
    else:
        newline = "\r\n" if "\r\n" in left else "\n"
        separator = "" if not left else newline if left.endswith(("\n", "\r")) else newline * 2
        merged = (
            f"{left}{separator}<!-- friday:preserved-conflict "
            f'canonical="{left_revision}" conflict="{right_revision}" -->{newline}'
            f"## {right_label}{newline}{newline}{right.rstrip(chr(13) + chr(10))}{newline}"
        )
    return ConflictMergePreview(left_revision, right_revision, diff, merged, identical)


def _content(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 4 * 1024 * 1024 or "\x00" in value:
        raise NoteMergeError(f"{label} content is invalid or too large")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise NoteMergeError(f"{label} content must be valid UTF-8") from exc
    return value


def _label(value: object) -> str:
    if not isinstance(value, str):
        raise NoteMergeError("merge label must be text")
    label = value.strip()
    if not label or len(label) > 200 or any(character in "\r\n\x00" for character in label):
        raise NoteMergeError("merge label is invalid")
    return label


__all__ = ["ConflictMergePreview", "NoteMergeError", "build_preserve_both_preview"]
