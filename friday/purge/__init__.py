"""Coordinated hard-delete (purge) of a Knowledge Object across DB, files, vault.

Purge is the one operation that intentionally destroys provenance, so it is always
capability-gated and audited by its callers (Admin API, CLI). This module performs
the mechanical cleanup: the storage layer removes every database row and the FTS
entry inside a single transaction, and only after that commit are the caller-owned
on-disk raw file and the Markdown vault copy unlinked.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from friday.config import FridaySettings
    from friday.memory import MemoryVault
    from friday.storage import FridayStorage


def purge_knowledge(
    storage: FridayStorage,
    settings: FridaySettings,
    memory_vault: MemoryVault | None,
    ko_id: str,
    user_id: str | None = None,
    *,
    require_soft_deleted: bool = True,
) -> dict[str, Any]:
    """Hard-delete one Knowledge Object: DB rows + FTS + raw file + vault copy.

    Returns the storage report augmented with ``vault_removed`` and ``file_unlinked``
    flags. Raises ``ValueError`` if the object is not soft-deleted and the guardrail
    is not overridden. File and vault removal happen after the DB commit and are
    best-effort/idempotent, so a missing artifact never fails the purge.
    """
    report = storage.purge_knowledge_object(ko_id, user_id, require_soft_deleted=require_soft_deleted)
    report.setdefault("vault_removed", False)
    report.setdefault("file_unlinked", False)
    if not report.get("existed"):
        return report
    owner = str(report.get("user_id") or user_id or "")

    # Vault Markdown projection — the DB deletion is already committed.
    if memory_vault is not None and owner:
        with suppress(OSError):
            memory_vault.delete_object(ko_id, owner)
            report["vault_removed"] = True

    # Deduplicated raw file: only when storage confirmed the Raw Object is orphaned
    # and its content-addressed file is unshared, and only when the recorded path is
    # genuinely inside the configured files directory (never trust the stored string
    # to escape it).
    raw_file_path = str(report.get("raw_file_path") or "")
    if report.get("unlink_file") and raw_file_path:
        files_root = Path(settings.files_dir).resolve()
        with suppress(OSError):
            resolved = Path(raw_file_path).resolve()
            if resolved.is_file() and resolved.is_relative_to(files_root):
                resolved.unlink()
                report["file_unlinked"] = True
    return report
