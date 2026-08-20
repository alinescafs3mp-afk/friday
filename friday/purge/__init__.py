"""Coordinated hard-delete (purge) of a Knowledge Object across DB, files, vault.

Purge is the one operation that intentionally destroys provenance, so it is always
capability-gated and audited by its callers (Admin API, CLI). This module performs
the mechanical cleanup: a targeted optional/legacy Markdown artifact is durably
unlinked first, the storage layer removes every database row and the FTS entry
inside a single transaction, and only after that commit is an orphan raw file
unlinked.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from friday.memory import VaultProjectionBoundaryError

if TYPE_CHECKING:
    from friday.config import FridaySettings
    from friday.storage import FridayStorage


class ProjectionDeletionHandle(Protocol):
    def delete_object(self, ko_id: str, user_id: str) -> int: ...


class VaultProjectionCleanupRequired(RuntimeError):
    """Hard purge stopped before its DB commit because plaintext cleanup was uncertain."""


def public_purge_receipt(
    report: Mapping[str, Any],
    *,
    knowledge_object_id: str,
) -> dict[str, bool | int | str]:
    """Project an internal purge report into a body-, identity-, and path-free receipt.

    Storage reports intentionally carry identifiers and a raw-file path while the
    coordinated cleanup is in progress.  Those fields are operational inputs, not
    API/CLI output.  Public callers get only a one-way object reference, exact
    aggregate counts, and cleanup booleans.
    """

    deleted = report.get("deleted")
    deleted_row_count = 0
    if isinstance(deleted, Mapping):
        for value in deleted.values():
            if type(value) is int and value > 0:
                deleted_row_count += value
    vault_removed_count_value = report.get("vault_removed_count")
    vault_removed_count = (
        vault_removed_count_value
        if type(vault_removed_count_value) is int and vault_removed_count_value >= 0
        else 0
    )
    reference = hashlib.sha256(knowledge_object_id.encode("utf-8", errors="replace")).hexdigest()
    return {
        "knowledge_object_ref_sha256": reference,
        "existed": bool(report.get("existed")),
        "deleted_row_count": deleted_row_count,
        "raw_removed": bool(report.get("raw_removed")),
        "file_unlinked": bool(report.get("file_unlinked")),
        "vault_removed": vault_removed_count > 0,
        "vault_removed_count": vault_removed_count,
    }


def purge_knowledge(
    storage: FridayStorage,
    settings: FridaySettings,
    memory_vault: ProjectionDeletionHandle | None,
    ko_id: str,
    user_id: str | None = None,
    *,
    require_soft_deleted: bool = True,
) -> dict[str, Any]:
    """Hard-delete one Knowledge Object: DB rows + FTS + raw file + vault copy.

    Returns the storage report augmented with exact ``vault_removed_count``,
    ``vault_removed`` and ``file_unlinked`` fields.  A matching plaintext vault
    artifact is removed *before* the DB commit; an unsafe traversal or failed unlink
    aborts the purge so a successful receipt can never strand an unaddressable note.
    An absent projection is an exact zero-removal success.
    """
    vault_removed_count = 0
    # Hold SQLite's BEGIN IMMEDIATE writer boundary across the last live-state
    # check, vault cleanup, and DB deletion.  The full_owner projector takes the
    # same boundary while re-reading a page item immediately before writing it.
    # Therefore a stale worker either finishes before this cleanup (and is then
    # removed) or observes the committed delete afterwards; it cannot recreate a
    # plaintext note between unlink and DB commit.
    with storage.transaction():
        current = storage.get_knowledge_object(ko_id, user_id)
        if current is not None:
            owner = str(current.get("user_id") or user_id or "")
            if require_soft_deleted and not current.get("deleted_at"):
                raise ValueError("Knowledge object must be soft-deleted before it can be purged")
            if memory_vault is not None and owner:
                try:
                    removed = memory_vault.delete_object(ko_id, owner)
                except VaultProjectionBoundaryError as exc:
                    raise VaultProjectionCleanupRequired(
                        "Vault projection cleanup could not be confirmed; database purge was not committed"
                    ) from exc
                if type(removed) is not int or removed < 0:
                    raise VaultProjectionCleanupRequired(
                        "Vault projection cleanup returned an invalid deletion receipt"
                    )
                vault_removed_count = removed

        report = storage.purge_knowledge_object(
            ko_id,
            user_id,
            require_soft_deleted=require_soft_deleted,
        )
    report["vault_removed_count"] = vault_removed_count
    report["vault_removed"] = vault_removed_count > 0
    report.setdefault("file_unlinked", False)
    if not report.get("existed"):
        return report

    # Deduplicated raw file: only when storage confirmed the Raw Object is orphaned
    # and its content-addressed file is unshared, and only when the recorded path is
    # genuinely inside the configured files directory (never trust the stored string
    # to escape it).
    raw_file_path = str(report.pop("raw_file_path", "") or "")
    if report.get("unlink_file") and raw_file_path:
        try:
            files_root = Path(settings.files_dir).resolve()
            resolved = Path(raw_file_path).resolve()
            if resolved.is_file() and resolved.is_relative_to(files_root):
                resolved.unlink()
                report["file_unlinked"] = True
        except (OSError, RuntimeError):
            pass
    return report


__all__ = ["VaultProjectionCleanupRequired", "public_purge_receipt", "purge_knowledge"]
