"""Incremental, revision-pinned index projection for synchronized vault notes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from .contracts import NoteDocument, PropertyValue
from .service import ObsidianService
from .wikilinks import LinkResolutionStatus, LinkSyntax, build_link_graph


class ObsidianIndexStorage(Protocol):
    def list_obsidian_note_bindings(
        self,
        user_id: str,
        *,
        vault_id: str | None = None,
        include_deleted: bool = False,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]: ...

    def upsert_obsidian_note_binding(self, user_id: str, **values: Any) -> dict[str, Any]: ...

    def tombstone_obsidian_note_binding(
        self, user_id: str, integration_id: str, **values: Any
    ) -> dict[str, Any]: ...

    def get_obsidian_note_index(
        self, user_id: str, binding_id: str, *, include_stale: bool = False
    ) -> dict[str, Any] | None: ...

    def upsert_obsidian_note_index(self, user_id: str, **values: Any) -> dict[str, Any]: ...

    def replace_obsidian_note_links(self, user_id: str, **values: Any) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class IncrementalIndexResult:
    observed: int
    indexed: int
    unchanged: int
    tombstoned: int
    links_published: int
    changed_paths: tuple[str, ...]


def refresh_incremental_index(
    storage: ObsidianIndexStorage,
    notes: ObsidianService,
    *,
    owner_id: str,
    vault_id: str,
    discovered_origin: str = "android",
) -> IncrementalIndexResult:
    """Index only new revisions, then publish their resolved link snapshots.

    Directory enumeration is necessarily vault-wide, but unchanged note bodies
    are neither reparsed nor rewritten in SQLite.  Tombstones are published only
    after a complete bounded snapshot has been read successfully.
    """

    bindings = storage.list_obsidian_note_bindings(
        owner_id,
        vault_id=vault_id,
        include_deleted=True,
        limit=5_000,
    )
    active_by_path = {str(item["current_path"]): item for item in bindings if item.get("deleted_at") is None}
    active_integration_ids = {
        str(item["integration_id"]): item for item in bindings if item.get("deleted_at") is None
    }

    documents: dict[str, NoteDocument] = {}
    for summary in notes.list_notes():
        documents[summary.path] = notes.read_note(summary.path)

    changed: list[tuple[NoteDocument, dict[str, Any]]] = []
    unchanged = 0
    current_bindings: dict[str, dict[str, Any]] = {}
    for path, document in documents.items():
        existing = active_by_path.get(path)
        if existing is None:
            integration_id = _discovered_integration_id(vault_id, path, document.revision)
            # A truncated hash collision must never silently retarget an identity.
            while integration_id in active_integration_ids:
                integration_id = _discovered_integration_id(
                    vault_id,
                    path,
                    hashlib.sha256((integration_id + document.revision).encode()).hexdigest(),
                )
            binding = storage.upsert_obsidian_note_binding(
                owner_id,
                vault_id=vault_id,
                integration_id=integration_id,
                current_path=path,
                current_revision=document.revision,
                ownership_mode="user_owned",
                origin=discovered_origin,
            )
        elif str(existing["current_revision"]) != document.revision:
            binding = storage.upsert_obsidian_note_binding(
                owner_id,
                vault_id=vault_id,
                integration_id=str(existing["integration_id"]),
                current_path=path,
                current_revision=document.revision,
                ownership_mode=str(existing["ownership_mode"]),
                origin=str(existing["origin"]),
                projection_kind=existing.get("projection_kind"),
                projection=_json_object(existing.get("projection_json")),
                friday_object_kind=existing.get("friday_object_kind"),
                friday_object_id=existing.get("friday_object_id"),
                expected_current_revision=str(existing["current_revision"]),
            )
        else:
            binding = existing
        current_bindings[path] = binding
        index_row = storage.get_obsidian_note_index(
            owner_id,
            str(binding["id"]),
            include_stale=True,
        )
        if (
            index_row is not None
            and str(index_row.get("state")) == "ready"
            and str(index_row.get("revision")) == document.revision
            and str(index_row.get("path")) == path
        ):
            unchanged += 1
            continue
        storage.upsert_obsidian_note_index(
            owner_id,
            binding_id=str(binding["id"]),
            revision=document.revision,
            metadata={key: _json_value(value) for key, value in document.properties.items()},
            metadata_coverage="complete",
            body_text=document.content,
            body_coverage="complete",
            source_size_bytes=document.size_bytes,
            title=document.title,
            source_modified_at=document.modified_at.isoformat(),
        )
        changed.append((document, binding))

    tombstoned = 0
    for path, binding in active_by_path.items():
        if path in documents:
            continue
        storage.tombstone_obsidian_note_binding(
            owner_id,
            str(binding["integration_id"]),
            vault_id=vault_id,
            expected_revision=str(binding["current_revision"]),
        )
        tombstoned += 1

    links_published = 0
    if changed:
        graph = build_link_graph(
            {path: document.content for path, document in documents.items()},
            titles={path: document.title for path, document in documents.items()},
        )
        binding_by_path = {path: str(binding["id"]) for path, binding in current_bindings.items()}
        for document, binding in changed:
            payload: list[dict[str, Any]] = []
            for link in graph.outgoing(document.path):
                state = link.status.value
                if link.status is LinkResolutionStatus.DYNAMIC:
                    state = "unresolved"
                resolved_id = (
                    binding_by_path.get(str(link.resolved_path))
                    if link.status is LinkResolutionStatus.RESOLVED
                    else None
                )
                payload.append(
                    {
                        "link_kind": (
                            "embed"
                            if link.link.embed
                            else "wikilink"
                            if link.link.syntax is LinkSyntax.WIKILINK
                            else "markdown"
                        ),
                        "target_text": link.target,
                        "target_path": link.resolved_path,
                        "target_subpath": link.link.fragment or None,
                        "resolution_state": state,
                        "resolved_binding_id": resolved_id,
                        "metadata": {
                            "raw": link.link.raw,
                            "dynamic": link.status is LinkResolutionStatus.DYNAMIC,
                            "candidates": list(link.candidates),
                        },
                    }
                )
            storage.replace_obsidian_note_links(
                owner_id,
                binding_id=str(binding["id"]),
                revision=document.revision,
                links=payload,
            )
            links_published += len(payload)

    return IncrementalIndexResult(
        observed=len(documents),
        indexed=len(changed),
        unchanged=unchanged,
        tombstoned=tombstoned,
        links_published=links_published,
        changed_paths=tuple(document.path for document, _binding in changed),
    )


def _discovered_integration_id(vault_id: str, path: str, first_revision: str) -> str:
    digest = hashlib.sha256(
        f"friday.obsidian.discovered.v1\0{vault_id}\0{path}\0{first_revision}".encode()
    ).hexdigest()
    return f"obsnote_{digest[:32]}"


def _json_value(value: Any) -> Any:
    if isinstance(value, PropertyValue):
        return _json_value(value.as_python())
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _json_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


__all__ = ["IncrementalIndexResult", "ObsidianIndexStorage", "refresh_incremental_index"]
