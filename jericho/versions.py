"""Structured diff between two Knowledge Object version snapshots.

Every edit snapshots the full row into ``knowledge_object_versions``, but until
now nothing could show *what changed*. This turns two snapshots into a
human-meaningful diff: scalar fields as before→after, long text as a unified
line diff, tags as added/removed sets, and metadata as key-level changes — so a
system that promises "ничего не теряется" can also show its history.

Pure and dependency-free (stdlib ``difflib``); the storage layer fetches the
two snapshots and calls :func:`diff_snapshots`.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

_SCALAR_FIELDS = (
    "title",
    "knowledge_kind",
    "importance",
    "quality_score",
    "promotion_score",
    "lifecycle_stage",
    "superseded_by_id",
)
_TEXT_FIELDS = ("summary", "content")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _unified(old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile="before",
        tofile="after",
        lineterm="",
    )
    return "\n".join(diff)


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields that differ between two snapshots, structured by kind."""
    changes: dict[str, Any] = {}

    for field in _SCALAR_FIELDS:
        before, after = old.get(field), new.get(field)
        if before != after:
            changes[field] = {"kind": "scalar", "from": before, "to": after}

    for field in _TEXT_FIELDS:
        before, after = str(old.get(field) or ""), str(new.get(field) or "")
        if before != after:
            changes[field] = {
                "kind": "text",
                "from": before,
                "to": after,
                "unified": _unified(before, after),
            }

    old_tags = set(_as_list(old.get("tags_json")))
    new_tags = set(_as_list(new.get("tags_json")))
    if old_tags != new_tags:
        changes["tags"] = {
            "kind": "set",
            "added": sorted(new_tags - old_tags),
            "removed": sorted(old_tags - new_tags),
        }

    old_meta = _as_dict(old.get("metadata_json"))
    new_meta = _as_dict(new.get("metadata_json"))
    if old_meta != new_meta:
        added = {key: new_meta[key] for key in new_meta.keys() - old_meta.keys()}
        removed = {key: old_meta[key] for key in old_meta.keys() - new_meta.keys()}
        changed = {
            key: {"from": old_meta[key], "to": new_meta[key]}
            for key in old_meta.keys() & new_meta.keys()
            if old_meta[key] != new_meta[key]
        }
        changes["metadata"] = {"kind": "map", "added": added, "removed": removed, "changed": changed}

    return changes
