"""Durable, owner-scoped orchestration around native Obsidian note writes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from friday.storage import FridayStorage

from .base_spec import parse_base
from .contracts import (
    FrontmatterError,
    IdempotencyConflictError,
    InvalidOperationIdError,
    NoteAlreadyExistsError,
    NoteDocument,
    NoteNotFoundError,
    NoteSearchResult,
    NoteSummary,
    NoteWriteResult,
    ObsidianNoteError,
    PropertyInput,
    PropertyValue,
    RevisionConflictError,
    VaultDeliveryState,
    VaultPathError,
    validate_revision,
)
from .note_merge import build_preserve_both_preview
from .service import ObsidianService
from .structured_notes import StructuredNoteError
from .structured_notes import replace_section as replace_markdown_section
from .wikilinks import (
    LinkMovePlan,
    ResolvedLink,
    build_link_graph,
    build_vault_link_graph,
    execute_move_plan,
    plan_move,
)

_RESULT_SCHEMA = "friday.obsidian-note-operation.v1"
_WORKFLOW_RESULT_SCHEMA = "friday.obsidian-workflow-operation.v1"
_MAX_WORKFLOW_RESULT_BYTES = 240 * 1024
_DAILY_TOKEN = re.compile(r"YYYY|YY|MM|DD")
_TERMINAL_FAILURES = frozenset({"failed", "conflict", "cancelled"})
_REPLAYABLE_RESULTS = frozenset(
    {"committed", "scan_pending", "scan_complete", "delivery_pending", "delivered", "reconciled"}
)


class OperationLedgerError(RuntimeError):
    """A durable operation row is missing, corrupt, or in an unsafe state."""


class OperationTerminalError(OperationLedgerError):
    """An identical operation already ended without a successful local result."""

    def __init__(self, operation_id: str, status: str, error: str) -> None:
        self.operation_id = operation_id
        self.status = status
        self.error = error
        super().__init__(f"Obsidian operation {operation_id!r} is {status}: {error}")


class OperationCommitUncertain(OperationLedgerError):
    """The filesystem may be committed but its ledger receipt was not persisted."""


@dataclass(frozen=True, slots=True)
class NoteSyncRequest:
    owner_id: str
    vault_id: str
    folder_id: str
    note_path: str
    revision: str
    deleted: bool = False


class ObsidianSyncAdapter(Protocol):
    """Replaceable boundary for scan dispatch and independently observed delivery."""

    def request_scan(self, request: NoteSyncRequest) -> None: ...

    def observe_delivery(self, request: NoteSyncRequest) -> VaultDeliveryState: ...


@dataclass(frozen=True, slots=True)
class DurableNoteResult:
    operation_id: str
    method: str
    status: str
    path: str
    revision: str
    previous_revision: str | None
    created: bool
    applied: bool
    replayed: bool
    delivery: VaultDeliveryState


@dataclass(frozen=True, slots=True)
class DurablePathChange:
    path: str
    previous_revision: str | None
    revision: str | None


@dataclass(frozen=True, slots=True)
class DurableLinkIssue:
    source_path: str
    target: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DurableWorkflowResult:
    """Durable local facts for a multi-path move or an explicit tombstone."""

    operation_id: str
    method: str
    status: str
    primary_path: str
    primary_revision: str | None
    previous_revision: str
    changes: tuple[DurablePathChange, ...]
    tombstones: tuple[str, ...]
    ambiguous: tuple[DurableLinkIssue, ...]
    unresolved: tuple[DurableLinkIssue, ...]
    dynamic: tuple[DurableLinkIssue, ...]
    applied: bool
    replayed: bool
    delivery: VaultDeliveryState

    @property
    def path(self) -> str:
        return self.primary_path

    @property
    def revision(self) -> str | None:
        return self.primary_revision

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)

    @property
    def changed_revisions(self) -> tuple[tuple[str, str | None], ...]:
        return tuple((change.path, change.revision) for change in self.changes)


@dataclass(frozen=True, slots=True)
class _FrozenMove:
    source_path: str
    destination_path: str
    expected_revision: str
    update_links: bool
    changes: tuple[DurablePathChange, ...]
    ambiguous: tuple[DurableLinkIssue, ...]
    unresolved: tuple[DurableLinkIssue, ...]
    dynamic: tuple[DurableLinkIssue, ...]
    signature: str


class ObsidianOperationService:
    """Bind one native note service to exactly one durable Friday owner."""

    def __init__(
        self,
        storage: FridayStorage,
        note_service: ObsidianService,
        *,
        owner_id: str,
        sync: ObsidianSyncAdapter | None = None,
        clock: Callable[[], date | datetime] | None = None,
    ) -> None:
        vault = storage.get_obsidian_vault(owner_id)
        if vault is None:
            raise ValueError("Obsidian vault not found for owner")
        if not _vault_root_matches(vault, note_service):
            raise ValueError("Obsidian note service root does not match the owner's vault")
        if not _vault_convention_matches(vault, note_service):
            raise ValueError("Obsidian note service convention does not match the owner's vault")
        self._storage = storage
        self._notes = note_service
        self._owner_id = str(owner_id).strip()
        self._vault_id = str(vault["id"])
        self._folder_id = str(vault["folder_id"])
        self._sync = sync
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._lock = threading.RLock()

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def vault_id(self) -> str:
        return self._vault_id

    def list_notes(self) -> tuple[NoteSummary, ...]:
        self._assert_owner_vault()
        return self._notes.list_notes()

    def search_notes(self, query: str, *, limit: int = 20) -> tuple[NoteSearchResult, ...]:
        self._assert_owner_vault()
        return self._notes.search_notes(query, limit=limit)

    def read_note(self, path: str | PurePosixPath) -> NoteDocument:
        self._assert_owner_vault()
        return self._notes.read_note(path)

    def create_note(
        self,
        operation_id: str,
        path: str | PurePosixPath,
        content: str = "",
        *,
        properties: Mapping[str, PropertyInput] | None = None,
        work_item_id: str | None = None,
    ) -> DurableNoteResult:
        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        canonical_path = _note_path(self._notes, path)
        _validate_text(content, label="content")
        typed_properties = _typed_properties(properties or {})
        arguments = {
            "method": "create",
            "path": canonical_path,
            "content": content,
            "properties": typed_properties,
        }

        def mutate(_reconcile: bool) -> NoteWriteResult:
            return self._notes.create_note(
                canonical_path,
                content,
                properties=typed_properties,
                operation_id=operation_id,
            )

        return self._execute(
            operation_id,
            method="create",
            arguments=arguments,
            expected_revision=None,
            work_item_id=work_item_id,
            mutate=mutate,
        )

    def append_note(
        self,
        operation_id: str,
        path: str | PurePosixPath,
        text: str,
        *,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> DurableNoteResult:
        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        canonical_path = _note_path(self._notes, path)
        _validate_text(text, label="text")
        _optional_revision(expected_revision)
        arguments = {"method": "append", "path": canonical_path, "text": text}

        def mutate(reconcile: bool) -> NoteWriteResult:
            try:
                return self._notes.append_note(
                    canonical_path,
                    text,
                    operation_id=operation_id,
                    expected_revision=expected_revision,
                )
            except RevisionConflictError:
                if not reconcile:
                    raise
                # A concurrent executor may have committed the same marker
                # between this service's read and expected-revision replace.
                return self._notes.append_note(
                    canonical_path,
                    text,
                    operation_id=operation_id,
                    expected_revision=expected_revision,
                )

        return self._execute(
            operation_id,
            method="append",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            mutate=mutate,
        )

    def set_properties(
        self,
        operation_id: str,
        path: str | PurePosixPath,
        properties: Mapping[str, PropertyInput],
        *,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> DurableNoteResult:
        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        canonical_path = _note_path(self._notes, path)
        typed_properties = _typed_properties(properties)
        _optional_revision(expected_revision)
        arguments = {
            "method": "set_properties",
            "path": canonical_path,
            "properties": typed_properties,
        }

        def mutate(reconcile: bool) -> NoteWriteResult:
            if reconcile:
                current = self._notes.read_note(canonical_path)
                if all(current.properties.get(key) == value for key, value in typed_properties.items()):
                    return NoteWriteResult(
                        path=current.path,
                        revision=current.revision,
                        previous_revision=expected_revision,
                        created=False,
                        applied=False,
                        operation_id=None,
                        delivery=VaultDeliveryState.local_only(),
                    )
                if expected_revision is None:
                    raise OperationCommitUncertain(
                        "prepared property mutation cannot be retried without an expected revision"
                    )
            return self._notes.set_properties(
                canonical_path,
                typed_properties,
                expected_revision=expected_revision,
            )

        return self._execute(
            operation_id,
            method="set_properties",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            mutate=mutate,
            claim_prepared_reconciliation=True,
        )

    def replace_section(
        self,
        operation_id: str,
        path: str | PurePosixPath,
        section: str,
        text: str,
        *,
        expected_revision: str,
        heading_level: int = 2,
        work_item_id: str | None = None,
    ) -> DurableNoteResult:
        """Replace one exact Markdown section with a replayable CAS write."""

        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        canonical_path = _note_path(self._notes, path)
        validate_revision(expected_revision)
        _validate_text(section, label="section")
        _validate_text(text, label="text")
        if (
            isinstance(heading_level, bool)
            or not isinstance(heading_level, int)
            or not 1 <= heading_level <= 6
        ):
            raise ValueError("heading_level must be between 1 and 6")
        arguments = {
            "method": "replace_section",
            "path": canonical_path,
            "section": section,
            "text": text,
            "heading_level": heading_level,
        }
        existing = self._existing_operation(
            operation_id,
            method="replace",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )
        closed = self._closed_note_result(
            existing,
            operation_id=operation_id,
            method="replace",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )
        if closed is not None:
            return closed

        frozen_revision = _prepared_target_revision(existing)
        current = self._notes.store.read_text(canonical_path)
        if frozen_revision is not None and current.revision == frozen_revision:
            rendered = current.text()
        else:
            if current.revision != expected_revision:
                raise RevisionConflictError(expected_revision, current.revision)
            try:
                rendered = replace_markdown_section(
                    current.text(),
                    section,
                    text,
                    heading_level=heading_level,
                )
            except StructuredNoteError as exc:
                raise FrontmatterError(str(exc)) from exc
        self._notes.store.validate_text_size(rendered)
        target_revision = hashlib.sha256(rendered.encode("utf-8", errors="strict")).hexdigest()
        if frozen_revision is not None and frozen_revision != target_revision:
            raise OperationLedgerError("prepared section replacement target changed")
        context = {"target_revision": target_revision}
        self._freeze_existing_context(existing, operation_id, context)

        def mutate(_reconcile: bool) -> NoteWriteResult:
            observed = self._notes.store.read_text(canonical_path)
            if observed.revision == target_revision:
                return _note_write_result(
                    canonical_path,
                    target_revision,
                    previous_revision=expected_revision,
                    applied=False,
                )
            if observed.revision != expected_revision:
                raise RevisionConflictError(expected_revision, observed.revision)
            written = self._notes.store.write_text(
                canonical_path,
                rendered,
                expected_revision=expected_revision,
            )
            return _note_write_result(
                canonical_path,
                written.revision,
                previous_revision=expected_revision,
                applied=True,
            )

        return self._execute(
            operation_id,
            method="replace",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            mutate=mutate,
            prepared_context=context,
            claim_prepared_reconciliation=True,
        )

    def move_note(
        self,
        operation_id: str,
        source: str | PurePosixPath,
        destination: str | PurePosixPath,
        *,
        expected_revision: str,
        update_links: bool = True,
        work_item_id: str | None = None,
    ) -> DurableWorkflowResult:
        """Move one exact revision and optionally rewrite only resolved links."""

        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        source_path = _note_path(self._notes, source)
        destination_path = _note_path(self._notes, destination)
        validate_revision(expected_revision)
        if not isinstance(update_links, bool):
            raise TypeError("update_links must be a bool")
        arguments = {
            "method": "move_note",
            "source": source_path,
            "destination": destination_path,
            "update_links": update_links,
        }
        live_plan: LinkMovePlan | None = None

        def prepare() -> dict[str, object]:
            nonlocal live_plan
            current = self._notes.store.read(source_path)
            if current.revision != expected_revision:
                raise RevisionConflictError(expected_revision, current.revision)
            if update_links:
                live_plan = plan_move(
                    build_vault_link_graph(self._notes.store),
                    source_path,
                    destination_path,
                )
                if live_plan.moved_revision != expected_revision:
                    raise RevisionConflictError(expected_revision, live_plan.moved_revision)
                context = _move_plan_context(live_plan, update_links=True)
            else:
                _assert_destination_absent(self._notes.store, destination_path)
                context = _simple_move_context(
                    source_path,
                    destination_path,
                    expected_revision,
                )
            _validate_workflow_payload(context)
            return context

        def mutate(reconcile: bool, context: Mapping[str, object]) -> DurableWorkflowResult:
            nonlocal live_plan
            frozen = _frozen_move(context)
            if _workflow_changes_hold(self._notes.store, frozen.changes):
                return _workflow_result_from_frozen_move(
                    operation_id,
                    frozen,
                    status="prepared",
                    applied=False,
                    replayed=reconcile,
                )
            if frozen.update_links:
                if live_plan is None:
                    live_plan = _rebuild_frozen_move_plan(self._notes.store, frozen)
                try:
                    executed = execute_move_plan(self._notes.store, live_plan)
                except RevisionConflictError as exc:
                    if not _source_is_revision(
                        self._notes.store,
                        frozen.source_path,
                        frozen.expected_revision,
                    ):
                        raise OperationCommitUncertain("move committed but a link rewrite raced") from exc
                    raise
                applied = executed.moved_applied or any(
                    change.applied for change in executed.link_rewrites.changes
                )
            else:
                if not _source_is_revision(
                    self._notes.store,
                    frozen.source_path,
                    frozen.expected_revision,
                ):
                    raise OperationCommitUncertain("move reached an unsafe partial postcondition")
                self._notes.store.move(
                    frozen.source_path,
                    frozen.destination_path,
                    expected_revision=frozen.expected_revision,
                )
                applied = True
            if not _workflow_changes_hold(self._notes.store, frozen.changes):
                raise OperationCommitUncertain("move postcondition is incomplete")
            return _workflow_result_from_frozen_move(
                operation_id,
                frozen,
                status="prepared",
                applied=applied,
                replayed=reconcile,
            )

        return self._execute_workflow(
            operation_id,
            method="move",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            prepare=prepare,
            mutate=mutate,
        )

    def apply_conflict_merge(
        self,
        operation_id: str,
        conflict_id: str,
        canonical_path: str | PurePosixPath,
        conflict_path: str | PurePosixPath,
        *,
        expected_revision: str,
        conflict_revision: str,
        work_item_id: str | None = None,
    ) -> DurableNoteResult:
        """CAS-apply one preserve-both preview while retaining its artifact."""

        operation_id = _operation_id(operation_id)
        frozen_conflict_id = _conflict_id(conflict_id)
        self._assert_owner_vault()
        canonical = _note_path(self._notes, canonical_path)
        artifact = _conflict_artifact_path(self._notes, conflict_path)
        if ".sync-conflict-" in PurePosixPath(canonical).name.casefold():
            raise VaultPathError("canonical note cannot be a conflict artifact")
        if canonical == artifact:
            raise VaultPathError("canonical note and conflict artifact must differ")
        validate_revision(expected_revision)
        validate_revision(conflict_revision)
        arguments = {
            "method": "apply_conflict_merge",
            "conflict_id": frozen_conflict_id,
            "canonical_path": canonical,
            "conflict_path": artifact,
            "conflict_revision": conflict_revision,
            "strategy": "preserve_both_v1",
        }
        existing = self._existing_operation(
            operation_id,
            method="conflict_merge",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )
        closed = self._closed_note_result(
            existing,
            operation_id=operation_id,
            method="conflict_merge",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )
        if closed is not None:
            return closed

        canonical_file = self._notes.store.read_text(canonical)
        artifact_file = self._notes.store.read_text(artifact)
        if artifact_file.revision != conflict_revision:
            raise RevisionConflictError(conflict_revision, artifact_file.revision)
        merged_content = _verified_preserve_both_content(
            canonical_file.text(),
            artifact_file.text(),
            canonical_revision=expected_revision,
            conflict_revision=conflict_revision,
        )
        self._notes.store.validate_text_size(merged_content)
        target_revision = hashlib.sha256(merged_content.encode("utf-8", errors="strict")).hexdigest()
        context = {
            "target_revision": target_revision,
            "conflict_id": frozen_conflict_id,
            "conflict_path": artifact,
            "conflict_revision": conflict_revision,
        }
        frozen_revision = _prepared_target_revision(existing)
        if frozen_revision is not None and frozen_revision != target_revision:
            raise OperationLedgerError("prepared conflict merge target changed")
        self._freeze_existing_context(existing, operation_id, context)

        def mutate(_reconcile: bool) -> NoteWriteResult:
            current = self._notes.store.read_text(canonical)
            if current.revision == target_revision:
                _require_conflict_revision(
                    self._notes,
                    artifact,
                    conflict_revision,
                    canonical_may_be_committed=True,
                )
                return _note_write_result(
                    canonical,
                    target_revision,
                    previous_revision=expected_revision,
                    applied=False,
                )
            if current.revision != expected_revision:
                raise RevisionConflictError(expected_revision, current.revision)
            _require_conflict_revision(
                self._notes,
                artifact,
                conflict_revision,
                canonical_may_be_committed=False,
            )
            written = self._notes.store.write_text(
                canonical,
                merged_content,
                expected_revision=expected_revision,
            )
            _require_conflict_revision(
                self._notes,
                artifact,
                conflict_revision,
                canonical_may_be_committed=True,
            )
            return _note_write_result(
                canonical,
                written.revision,
                previous_revision=expected_revision,
                applied=True,
            )

        return self._execute(
            operation_id,
            method="conflict_merge",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            mutate=mutate,
            prepared_context=context,
            claim_prepared_reconciliation=True,
        )

    def delete_note(
        self,
        operation_id: str,
        path: str | PurePosixPath,
        *,
        expected_revision: str,
        work_item_id: str | None = None,
    ) -> DurableWorkflowResult:
        """Delete one revision and persist an explicit, replay-safe tombstone."""

        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        canonical_path = _note_path(self._notes, path)
        validate_revision(expected_revision)
        arguments = {"method": "delete_note", "path": canonical_path}

        def prepare() -> dict[str, object]:
            current = self._notes.store.read(canonical_path)
            if current.revision != expected_revision:
                raise RevisionConflictError(expected_revision, current.revision)
            return {
                "schema": _WORKFLOW_RESULT_SCHEMA,
                "phase": "prepared",
                "kind": "delete",
                "primary_path": canonical_path,
                "primary_revision": None,
                "previous_revision": expected_revision,
                "changes": [
                    {
                        "path": canonical_path,
                        "previous_revision": expected_revision,
                        "revision": None,
                    }
                ],
                "tombstones": [canonical_path],
                "ambiguous": [],
                "unresolved": [],
                "dynamic": [],
            }

        def mutate(reconcile: bool, context: Mapping[str, object]) -> DurableWorkflowResult:
            frozen = _workflow_result_from_payload(
                context,
                operation_id=operation_id,
                method="delete",
                status="prepared",
                replayed=reconcile,
                require_result=False,
            )
            if self._notes.store.delete_postcondition(
                canonical_path,
                expected_revision=expected_revision,
            ):
                return replace(frozen, applied=False)
            self._notes.store.delete(
                canonical_path,
                expected_revision=expected_revision,
            )
            if not self._notes.store.delete_postcondition(
                canonical_path,
                expected_revision=expected_revision,
            ):
                raise OperationCommitUncertain("delete postcondition is incomplete")
            return replace(frozen, applied=True)

        return self._execute_workflow(
            operation_id,
            method="delete",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            prepare=prepare,
            mutate=mutate,
        )

    def create_base(
        self,
        operation_id: str,
        path: str | PurePosixPath,
        content: str,
        *,
        work_item_id: str | None = None,
    ) -> DurableNoteResult:
        """Create one validated Obsidian Base file without replacing an existing file."""

        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        canonical_path = _base_path(self._notes, path)
        _validate_text(content, label="content")
        parse_base(content)
        self._notes.store.validate_text_size(content)
        arguments = {"method": "create_base", "path": canonical_path, "content": content}
        existing = self._existing_operation(
            operation_id,
            method="base",
            arguments=arguments,
            expected_revision=None,
            work_item_id=work_item_id,
        )
        closed = self._closed_note_result(
            existing,
            operation_id=operation_id,
            method="base",
            arguments=arguments,
            expected_revision=None,
            work_item_id=work_item_id,
        )
        if closed is not None:
            return closed
        target_revision = hashlib.sha256(content.encode("utf-8", errors="strict")).hexdigest()
        context = {"target_revision": target_revision}
        self._freeze_existing_context(existing, operation_id, context)

        def mutate(reconcile: bool) -> NoteWriteResult:
            try:
                current = self._notes.store.read_text(canonical_path)
            except NoteNotFoundError:
                written = self._notes.store.write_text(
                    canonical_path,
                    content,
                    create_only=True,
                )
                return _note_write_result(
                    canonical_path,
                    written.revision,
                    previous_revision=None,
                    applied=True,
                    created=True,
                )
            if current.revision != target_revision or not reconcile:
                raise NoteAlreadyExistsError(canonical_path)
            return _note_write_result(
                canonical_path,
                target_revision,
                previous_revision=None,
                applied=False,
                created=True,
            )

        return self._execute(
            operation_id,
            method="base",
            arguments=arguments,
            expected_revision=None,
            work_item_id=work_item_id,
            mutate=mutate,
            prepared_context=context,
        )

    def daily_note(
        self,
        operation_id: str,
        day: date | datetime | None = None,
        *,
        content: str = "",
        section: str | None = None,
        item: str | None = None,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> DurableNoteResult:
        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        selected = day
        if selected is None:
            selected = (
                self._frozen_daily_day(
                    operation_id,
                    content=content,
                    section=section,
                    item=item,
                )
                or self._clock()
            )
        if isinstance(selected, datetime):
            selected = selected.date()
        if not isinstance(selected, date):
            raise TypeError("daily note day must be a date or datetime")
        _optional_revision(expected_revision)
        _validate_text(content, label="content")
        if (section is None) != (item is None):
            raise ValueError("daily note section and item must be supplied together")
        if section is not None and item is not None:
            _validate_text(section, label="section")
            _validate_text(item, label="item")
            if content:
                raise ValueError("daily note accepts either content or a structured section item")
        canonical_path = _daily_path(self._notes, selected)
        arguments = {
            "method": "daily_note",
            "path": canonical_path,
            "day": selected,
            "content": content,
        }
        if section is not None:
            arguments.update({"section": section, "item": item})
        marker_payload = (
            content if section is None else f"section:{len(section)}:{section}item:{len(item or '')}:{item}"
        )

        def mutate(_reconcile: bool) -> NoteWriteResult:
            try:
                current = self._notes.read_note(canonical_path)
            except NoteNotFoundError:
                if expected_revision is not None:
                    raise RevisionConflictError(expected_revision, None) from None
                return self._notes.daily_note(
                    selected,
                    content=content,
                    section=section,
                    item=item,
                    operation_id=operation_id,
                    expected_revision=expected_revision,
                )
            if _core_operation_applied(current.content, operation_id, marker_payload):
                return self._notes.daily_note(
                    selected,
                    content=content,
                    section=section,
                    item=item,
                    operation_id=operation_id,
                    expected_revision=expected_revision,
                )
            if expected_revision is not None and current.revision != expected_revision:
                raise RevisionConflictError(expected_revision, current.revision)
            if not content:
                return self._notes.daily_note(
                    selected,
                    content="",
                    section=section,
                    item=item,
                    operation_id=operation_id,
                    expected_revision=expected_revision,
                )
            return self._notes.append_note(
                canonical_path,
                content,
                operation_id=operation_id,
                expected_revision=expected_revision,
            )

        return self._execute(
            operation_id,
            method="daily_note",
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            mutate=mutate,
            prepared_context={"resolved_day": selected.isoformat()},
        )

    def get_operation(self, operation_id: str) -> DurableNoteResult | DurableWorkflowResult:
        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        row = self._storage.get_obsidian_operation(self._owner_id, operation_id)
        if row is None:
            raise OperationLedgerError("Obsidian operation not found for owner")
        if _row_has_workflow_schema(row):
            return _workflow_result_from_row(row, replayed=True)
        return _result_from_row(row, replayed=True)

    def refresh_delivery(self, operation_id: str) -> DurableNoteResult | DurableWorkflowResult:
        """Persist only delivery facts explicitly observed by the injected adapter."""

        operation_id = _operation_id(operation_id)
        if self._sync is None:
            return self.get_operation(operation_id)
        with self._lock:
            self._assert_owner_vault()
            row = self._storage.get_obsidian_operation(self._owner_id, operation_id)
            if row is None:
                raise OperationLedgerError("Obsidian operation not found for owner")
            if _row_has_workflow_schema(row):
                status = str(row["status"])
                if status in _TERMINAL_FAILURES or status in {"prepared", "uncertain"}:
                    return _workflow_result_from_row(row, replayed=True)
                if status in {"committed", "reconciled"}:
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        "scan_pending",
                    )
                    status = "scan_pending"
                workflow_result = _workflow_result_from_row(row, replayed=True)
                if status == "scan_pending":
                    self._request_workflow_scans(workflow_result)
                observed = self._observe_workflow_delivery(workflow_result)
                _validate_observed_delivery(observed)
                merged = _merge_delivery(workflow_result.delivery, observed)
                delivery = _delivery_json(merged)
                if status == "scan_pending" and merged.server_scan_complete:
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        "scan_complete",
                        delivery=delivery,
                    )
                    status = "scan_complete"
                elif status == "scan_pending":
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        "scan_pending",
                        delivery=delivery,
                    )
                if status in {"scan_complete", "delivery_pending"}:
                    next_state = "delivered" if merged.android_received else "delivery_pending"
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        next_state,
                        delivery=delivery,
                    )
                elif status == "delivered":
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        "delivered",
                        delivery=delivery,
                    )
                return _workflow_result_from_row(row, replayed=True)
            status = str(row["status"])
            if status in _TERMINAL_FAILURES or status in {"prepared", "uncertain"}:
                return _result_from_row(row, replayed=True)
            if status in {"committed", "reconciled"}:
                row = self._storage.transition_obsidian_operation(
                    self._owner_id, operation_id, "scan_pending"
                )
                status = "scan_pending"
            note_result = _result_from_row(row, replayed=True)
            if status == "scan_pending":
                self._request_scan(note_result)
            observed = self._sync.observe_delivery(self._sync_request(note_result))
            _validate_observed_delivery(observed)
            merged = _merge_delivery(note_result.delivery, observed)
            delivery = _delivery_json(merged)
            if status == "scan_pending" and merged.server_scan_complete:
                row = self._storage.transition_obsidian_operation(
                    self._owner_id,
                    operation_id,
                    "scan_complete",
                    delivery=delivery,
                )
                status = "scan_complete"
            elif status == "scan_pending":
                row = self._storage.transition_obsidian_operation(
                    self._owner_id,
                    operation_id,
                    "scan_pending",
                    delivery=delivery,
                )
            if status in {"scan_complete", "delivery_pending"}:
                next_state = "delivered" if merged.android_received else "delivery_pending"
                row = self._storage.transition_obsidian_operation(
                    self._owner_id,
                    operation_id,
                    next_state,
                    delivery=delivery,
                )
            elif status == "delivered":
                row = self._storage.transition_obsidian_operation(
                    self._owner_id,
                    operation_id,
                    "delivered",
                    delivery=delivery,
                )
            return _result_from_row(row, replayed=True)

    def _existing_operation(
        self,
        operation_id: str,
        *,
        method: str,
        arguments: Mapping[str, object],
        expected_revision: str | None,
        work_item_id: str | None,
    ) -> dict[str, Any] | None:
        row = self._storage.get_obsidian_operation(self._owner_id, operation_id)
        if row is None:
            return None
        requested = (
            self._owner_id,
            self._vault_id,
            method,
            canonical_arguments_digest(arguments),
            str(expected_revision or ""),
            str(_optional_work_item_id(work_item_id) or ""),
        )
        actual = (
            str(row.get("user_id") or ""),
            str(row.get("vault_id") or ""),
            str(row.get("method") or ""),
            str(row.get("arguments_digest") or ""),
            str(row.get("expected_revision") or ""),
            str(row.get("work_item_id") or ""),
        )
        if actual != requested:
            raise IdempotencyConflictError("operation_id was already used with different canonical arguments")
        return row

    def _closed_note_result(
        self,
        row: Mapping[str, Any] | None,
        *,
        operation_id: str,
        method: str,
        arguments: Mapping[str, object],
        expected_revision: str | None,
        work_item_id: str | None,
    ) -> DurableNoteResult | None:
        if row is None or str(row.get("status") or "") in {"prepared", "uncertain"}:
            return None

        def unreachable(_reconcile: bool) -> NoteWriteResult:
            raise AssertionError("closed operation unexpectedly reached its mutation")

        return self._execute(
            operation_id,
            method=method,
            arguments=arguments,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            mutate=unreachable,
        )

    def _freeze_existing_context(
        self,
        row: Mapping[str, Any] | None,
        operation_id: str,
        context: Mapping[str, object],
    ) -> None:
        if row is None:
            return
        status = str(row.get("status") or "")
        if status not in {"prepared", "uncertain"}:
            return
        payload = _json_object(row.get("result_json"), label="operation result")
        if all(payload.get(key) == value for key, value in context.items()):
            return
        payload = {"schema": _RESULT_SCHEMA, **payload, **context}
        try:
            self._storage.transition_obsidian_operation(
                self._owner_id,
                operation_id,
                status,
                result=payload,
            )
        except Exception as exc:
            current = self._storage.get_obsidian_operation(self._owner_id, operation_id)
            if current is not None and str(current.get("status") or "") in _REPLAYABLE_RESULTS:
                return
            raise OperationLedgerError("could not durably freeze workflow target") from exc

    def _execute_workflow(
        self,
        operation_id: str,
        *,
        method: str,
        arguments: Mapping[str, object],
        expected_revision: str,
        work_item_id: str | None,
        prepare: Callable[[], dict[str, object]],
        mutate: Callable[[bool, Mapping[str, object]], DurableWorkflowResult],
    ) -> DurableWorkflowResult:
        work_item_id = _optional_work_item_id(work_item_id)
        digest = canonical_arguments_digest(arguments)
        with self._lock:
            try:
                row, prepared = self._storage.prepare_obsidian_operation(
                    self._owner_id,
                    operation_id=operation_id,
                    vault_id=self._vault_id,
                    method=method,
                    arguments_digest=digest,
                    expected_revision=expected_revision,
                    work_item_id=work_item_id,
                )
            except ValueError as exc:
                if "operation_id was already used" in str(exc):
                    raise IdempotencyConflictError(
                        "operation_id was already used with different canonical arguments"
                    ) from exc
                raise
            status = str(row["status"])
            if not prepared:
                if status in _REPLAYABLE_RESULTS:
                    durable = _workflow_result_from_row(row, replayed=True)
                    if status in {"committed", "reconciled", "scan_pending"}:
                        self._request_workflow_scans(durable)
                    return durable
                if status in _TERMINAL_FAILURES:
                    raise OperationTerminalError(operation_id, status, _error_from_row(row))
                if status not in {"prepared", "uncertain"}:
                    raise OperationLedgerError(f"unsupported Obsidian operation state: {status}")

            context = _prepared_workflow_context(row, method=method)
            if context is None:
                try:
                    context = prepare()
                    _validate_workflow_payload(context)
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        status,
                        result=context,
                        delivery=_delivery_json(_uncommitted_delivery()),
                    )
                except RevisionConflictError as exc:
                    self._transition_failure(
                        operation_id,
                        status,
                        "conflict",
                        "revision_conflict",
                        actual_revision=exc.actual_revision,
                        competing=not prepared,
                    )
                    raise
                except (NoteAlreadyExistsError, NoteNotFoundError, VaultPathError) as exc:
                    self._transition_failure(
                        operation_id,
                        status,
                        "failed",
                        _error_code(exc),
                        competing=not prepared,
                    )
                    raise
                except ObsidianNoteError as exc:
                    self._transition_failure(
                        operation_id,
                        status,
                        "failed",
                        _error_code(exc),
                        context=context,
                        competing=not prepared,
                    )
                    raise
                except Exception as exc:
                    self._transition_failure(
                        operation_id,
                        status,
                        "uncertain",
                        "workflow_prepare_uncertain",
                        context=context,
                        competing=not prepared,
                    )
                    raise OperationCommitUncertain("workflow plan could not be durably prepared") from exc

            try:
                result = mutate(not prepared, context)
            except OperationCommitUncertain:
                self._transition_failure(
                    operation_id,
                    status,
                    "uncertain",
                    "local_commit_uncertain",
                    context=context,
                    competing=False,
                )
                raise
            except RevisionConflictError as exc:
                self._transition_failure(
                    operation_id,
                    status,
                    "conflict",
                    "revision_conflict",
                    actual_revision=exc.actual_revision,
                    competing=not prepared,
                )
                raise
            except (NoteAlreadyExistsError, NoteNotFoundError, VaultPathError) as exc:
                self._transition_failure(
                    operation_id,
                    status,
                    "failed",
                    _error_code(exc),
                    competing=not prepared,
                )
                raise
            except ObsidianNoteError as exc:
                self._transition_failure(
                    operation_id,
                    status,
                    "failed",
                    _error_code(exc),
                    competing=not prepared,
                )
                raise
            except Exception as exc:
                self._transition_failure(
                    operation_id,
                    status,
                    "uncertain",
                    "local_commit_uncertain",
                    context=context,
                    competing=False,
                )
                raise OperationCommitUncertain(
                    "workflow mutation may have committed; retry the same operation ID"
                ) from exc

            target_status = "reconciled" if status == "uncertain" else "committed"
            payload = _workflow_result_json(result)
            try:
                row = self._storage.transition_obsidian_operation(
                    self._owner_id,
                    operation_id,
                    target_status,
                    result=payload,
                    delivery=_delivery_json(result.delivery),
                )
            except Exception as exc:
                raise OperationCommitUncertain(
                    "local workflow commit succeeded but its durable receipt is incomplete"
                ) from exc
            durable = replace(
                result,
                status=str(row["status"]),
                replayed=not prepared,
            )
            self._request_workflow_scans(durable)
            return durable

    def _execute(
        self,
        operation_id: str,
        *,
        method: str,
        arguments: Mapping[str, object],
        expected_revision: str | None,
        work_item_id: str | None,
        mutate: Callable[[bool], NoteWriteResult],
        prepared_context: Mapping[str, object] | None = None,
        claim_prepared_reconciliation: bool = False,
    ) -> DurableNoteResult:
        work_item_id = _optional_work_item_id(work_item_id)
        digest = canonical_arguments_digest(arguments)
        with self._lock:
            try:
                row, prepared = self._storage.prepare_obsidian_operation(
                    self._owner_id,
                    operation_id=operation_id,
                    vault_id=self._vault_id,
                    method=method,
                    arguments_digest=digest,
                    expected_revision=expected_revision,
                    work_item_id=work_item_id,
                )
            except ValueError as exc:
                if "operation_id was already used" in str(exc):
                    raise IdempotencyConflictError(
                        "operation_id was already used with different canonical arguments"
                    ) from exc
                raise
            if not prepared:
                status = str(row["status"])
                if status in _REPLAYABLE_RESULTS:
                    if status in {"committed", "reconciled"}:
                        row = self._storage.transition_obsidian_operation(
                            self._owner_id, operation_id, "scan_pending"
                        )
                    durable = _result_from_row(row, replayed=True)
                    if durable.status == "scan_pending":
                        self._request_scan(durable)
                    return durable
                if status in _TERMINAL_FAILURES:
                    error = _error_from_row(row)
                    raise OperationTerminalError(operation_id, status, error)
                if status not in {"prepared", "uncertain"}:
                    raise OperationLedgerError(f"unsupported Obsidian operation state: {status}")
            else:
                status = "prepared"

            if prepared and prepared_context:
                prepared_payload = {"schema": _RESULT_SCHEMA, **prepared_context}
                try:
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        "prepared",
                        result=prepared_payload,
                    )
                except Exception as exc:
                    raise OperationLedgerError(
                        "could not durably freeze Obsidian operation arguments"
                    ) from exc

            if not prepared and status == "prepared" and claim_prepared_reconciliation:
                # Property writes have no in-file operation marker. Close a row
                # without a revision as an explicit conflict; otherwise claim it
                # uncertain before an exact, safely repeatable inspection/retry.
                if expected_revision is None:
                    error = "expected_revision_required_for_recovery"
                    try:
                        self._storage.transition_obsidian_operation(
                            self._owner_id,
                            operation_id,
                            "conflict",
                            result={"schema": _RESULT_SCHEMA, "error": error},
                            delivery=_delivery_json(_uncommitted_delivery()),
                        )
                    except Exception as exc:
                        raise OperationLedgerError(
                            "could not close an unrecoverable prepared property mutation"
                        ) from exc
                    raise OperationTerminalError(operation_id, "conflict", error)
                uncertain_payload: dict[str, object] = {
                    "schema": _RESULT_SCHEMA,
                    "error": "local_commit_uncertain",
                }
                uncertain_payload.update(prepared_context or {})
                try:
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        "uncertain",
                        result=uncertain_payload,
                        delivery=_delivery_json(_uncommitted_delivery()),
                    )
                except Exception as exc:
                    raise OperationLedgerError(
                        "could not durably claim prepared property mutation for reconciliation"
                    ) from exc
                status = str(row["status"])

            competing_attempt = not prepared and status == "prepared"
            try:
                result = mutate(not prepared)
            except RevisionConflictError as exc:
                self._transition_failure(
                    operation_id,
                    status,
                    "conflict",
                    "revision_conflict",
                    actual_revision=exc.actual_revision,
                    context=prepared_context,
                    competing=competing_attempt,
                )
                raise
            except IdempotencyConflictError:
                self._transition_failure(
                    operation_id,
                    status,
                    "conflict",
                    "idempotency_conflict",
                    context=prepared_context,
                    competing=competing_attempt,
                )
                raise
            except (NoteAlreadyExistsError, NoteNotFoundError, VaultPathError, FrontmatterError) as exc:
                self._transition_failure(
                    operation_id,
                    status,
                    "failed",
                    _error_code(exc),
                    context=prepared_context,
                    competing=competing_attempt,
                )
                raise
            except ObsidianNoteError as exc:
                self._transition_failure(
                    operation_id,
                    status,
                    "failed",
                    _error_code(exc),
                    context=prepared_context,
                    competing=competing_attempt,
                )
                raise
            except Exception as exc:
                self._transition_failure(
                    operation_id,
                    status,
                    "uncertain",
                    "local_commit_uncertain",
                    context=prepared_context,
                    competing=competing_attempt,
                )
                raise OperationCommitUncertain(
                    "Obsidian mutation may have committed; retry the same owner and operation ID"
                ) from exc

            payload = _result_json(result)
            payload.update(prepared_context or {})
            delivery = _delivery_json(result.delivery)
            try:
                if status == "uncertain":
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        "reconciled",
                        result=payload,
                        delivery=delivery,
                    )
                else:
                    row = self._storage.transition_obsidian_operation(
                        self._owner_id,
                        operation_id,
                        "committed",
                        result=payload,
                        delivery=delivery,
                    )
                row = self._storage.transition_obsidian_operation(
                    self._owner_id,
                    operation_id,
                    "scan_pending",
                    result=payload,
                    delivery=delivery,
                )
            except Exception as exc:
                raise OperationCommitUncertain(
                    "local note commit succeeded but its durable receipt is incomplete"
                ) from exc
            durable = _result_from_row(row, replayed=not prepared)
            self._request_scan(durable)
            return durable

    def _transition_failure(
        self,
        operation_id: str,
        old_status: str,
        target: str,
        error: str,
        *,
        actual_revision: str | None = None,
        context: Mapping[str, object] | None = None,
        competing: bool = False,
    ) -> None:
        if competing:
            # An existing prepared row has no lease. Another executor may own
            # it, so this contender must never overwrite its eventual receipt.
            return
        current = self._storage.get_obsidian_operation(self._owner_id, operation_id)
        if current is None or str(current.get("status") or "") != old_status:
            return
        result: dict[str, object] = {"schema": _RESULT_SCHEMA, "error": error}
        result.update(context or {})
        if actual_revision is not None:
            result["actual_revision"] = actual_revision
        try:
            self._storage.transition_obsidian_operation(
                self._owner_id,
                operation_id,
                target,
                result=result,
                delivery=_delivery_json(_uncommitted_delivery()),
            )
        except Exception:
            # A racing execution may already have advanced the same durable row.
            return

    def _request_scan(self, result: DurableNoteResult) -> None:
        if self._sync is None:
            return
        try:
            self._sync.request_scan(self._sync_request(result))
        except Exception:
            # The local commit is authoritative. The scan_pending ledger row is
            # intentionally retained for a later reconciliation worker.
            return

    def _request_workflow_scans(self, result: DurableWorkflowResult) -> None:
        if self._sync is None:
            return
        for request in self._workflow_sync_requests(result):
            try:
                self._sync.request_scan(request)
            except Exception:
                # Every changed path remains in the durable result and the
                # reconciler will dispatch it again.
                continue

    def _observe_workflow_delivery(self, result: DurableWorkflowResult) -> VaultDeliveryState:
        if self._sync is None:
            return result.delivery
        observations: list[VaultDeliveryState] = []
        for request in self._workflow_sync_requests(result):
            try:
                observations.append(self._sync.observe_delivery(request))
            except Exception:
                observations.append(VaultDeliveryState.local_only())
        if not observations:
            return VaultDeliveryState.local_only()
        completion_values = [
            item.android_completion for item in observations if item.android_completion is not None
        ]
        return VaultDeliveryState(
            local_write_complete=all(item.local_write_complete for item in observations),
            server_scan_complete=all(item.server_scan_complete for item in observations),
            android_connected=all(item.android_connected for item in observations),
            android_completion=(min(completion_values) if completion_values else None),
            android_received=all(item.android_received for item in observations),
            obsidian_opened=False,
        )

    def _workflow_sync_requests(
        self,
        result: DurableWorkflowResult,
    ) -> tuple[NoteSyncRequest, ...]:
        return tuple(
            NoteSyncRequest(
                owner_id=self._owner_id,
                vault_id=self._vault_id,
                folder_id=self._folder_id,
                note_path=change.path,
                revision=change.revision or change.previous_revision or result.previous_revision,
                deleted=change.revision is None,
            )
            for change in result.changes
        )

    def _sync_request(self, result: DurableNoteResult) -> NoteSyncRequest:
        return NoteSyncRequest(
            owner_id=self._owner_id,
            vault_id=self._vault_id,
            folder_id=self._folder_id,
            note_path=result.path,
            revision=result.revision,
        )

    def _assert_owner_vault(self) -> None:
        vault = self._storage.get_obsidian_vault(self._owner_id)
        if vault is None or str(vault["id"]) != self._vault_id:
            raise OperationLedgerError("owner's Obsidian vault binding is no longer available")
        if not _vault_root_matches(vault, self._notes):
            raise OperationLedgerError("owner's Obsidian vault root changed")
        if not _vault_convention_matches(vault, self._notes):
            raise OperationLedgerError("owner's Obsidian vault convention changed")

    def _frozen_daily_day(
        self,
        operation_id: str,
        *,
        content: str,
        section: str | None = None,
        item: str | None = None,
    ) -> date | None:
        row = self._storage.get_obsidian_operation(self._owner_id, operation_id)
        if row is None or str(row.get("method") or "") != "daily_note":
            return None
        result = _json_object(row.get("result_json"), label="operation result")
        raw_day = result.get("resolved_day")
        if isinstance(raw_day, str):
            try:
                return date.fromisoformat(raw_day)
            except ValueError as exc:
                raise OperationLedgerError("daily operation has an invalid frozen date") from exc
        current = self._clock()
        if isinstance(current, datetime):
            current = current.date()
        if not isinstance(current, date):
            raise TypeError("daily note clock must return a date or datetime")
        return _recover_daily_day_from_digest(
            row,
            self._notes,
            content=content,
            section=section,
            item=item,
            current=current,
        )


def _note_write_result(
    path: str,
    revision: str,
    *,
    previous_revision: str | None,
    applied: bool,
    created: bool = False,
) -> NoteWriteResult:
    return NoteWriteResult(
        path=path,
        revision=revision,
        previous_revision=previous_revision,
        created=created,
        applied=applied,
        operation_id=None,
        delivery=VaultDeliveryState.local_only(),
    )


def _prepared_target_revision(row: Mapping[str, Any] | None) -> str | None:
    if row is None or str(row.get("status") or "") not in {"prepared", "uncertain"}:
        return None
    payload = _json_object(row.get("result_json"), label="operation result")
    target = payload.get("target_revision")
    if target is None:
        return None
    try:
        return validate_revision(target)  # type: ignore[arg-type]
    except ValueError as exc:
        raise OperationLedgerError("prepared target revision is invalid") from exc


def _base_path(notes: ObsidianService, path: str | PurePosixPath) -> str:
    normalized = notes.store.normalize_path(path)
    pure = PurePosixPath(normalized)
    if pure.suffix == "":
        normalized += ".base"
    elif pure.suffix.casefold() != ".base":
        raise VaultPathError("Base path must have the .base extension")
    return notes.store.normalize_path(normalized)


def _conflict_artifact_path(
    notes: ObsidianService,
    path: str | PurePosixPath,
) -> str:
    normalized = _note_path(notes, path)
    if ".sync-conflict-" not in PurePosixPath(normalized).name.casefold():
        raise VaultPathError("conflict artifact must be an explicit sync-conflict Markdown file")
    return normalized


def _require_conflict_revision(
    notes: ObsidianService,
    path: str,
    expected_revision: str,
    *,
    canonical_may_be_committed: bool,
) -> None:
    try:
        current = notes.store.read_text(path)
    except NoteNotFoundError as exc:
        if canonical_may_be_committed:
            raise OperationCommitUncertain(
                "canonical merge may be committed but its conflict artifact disappeared"
            ) from exc
        raise RevisionConflictError(expected_revision, None) from exc
    if current.revision == expected_revision:
        return
    if canonical_may_be_committed:
        raise OperationCommitUncertain("canonical merge may be committed but its conflict artifact changed")
    raise RevisionConflictError(expected_revision, current.revision)


def _verified_preserve_both_content(
    canonical_content: str,
    conflict_content: str,
    *,
    canonical_revision: str,
    conflict_revision: str,
) -> str:
    """Build, or verify and recover, the exact deterministic preserve-both bytes."""

    if hashlib.sha256(conflict_content.encode("utf-8", errors="strict")).hexdigest() != conflict_revision:
        raise RevisionConflictError(conflict_revision, None)
    current_revision = hashlib.sha256(canonical_content.encode("utf-8", errors="strict")).hexdigest()
    if current_revision == canonical_revision:
        preview = build_preserve_both_preview(canonical_content, conflict_content)
        if preview.canonical_revision != canonical_revision or preview.conflict_revision != conflict_revision:
            raise OperationLedgerError("preserve-both preview revisions changed")
        return preview.merged_content

    marker = (
        f'<!-- friday:preserved-conflict canonical="{canonical_revision}" conflict="{conflict_revision}" -->'
    )
    if canonical_content.count(marker) != 1:
        raise RevisionConflictError(canonical_revision, current_revision)
    prefix = canonical_content[: canonical_content.index(marker)]
    candidates = {prefix}
    for separator in ("\r\n\r\n", "\n\n", "\r\n", "\n"):
        if prefix.endswith(separator):
            candidates.add(prefix[: -len(separator)])
    verified = [
        candidate
        for candidate in candidates
        if hashlib.sha256(candidate.encode("utf-8", errors="strict")).hexdigest() == canonical_revision
        and build_preserve_both_preview(candidate, conflict_content).merged_content == canonical_content
    ]
    if len(verified) != 1:
        raise RevisionConflictError(canonical_revision, current_revision)
    return canonical_content


def _assert_destination_absent(store: Any, path: str) -> None:
    try:
        store.read(path)
    except NoteNotFoundError:
        return
    raise NoteAlreadyExistsError(path)


def _source_is_revision(store: Any, path: str, revision: str) -> bool:
    try:
        current = store.read(path)
    except NoteNotFoundError:
        return False
    return current.revision == revision


def _workflow_changes_hold(store: Any, changes: Sequence[DurablePathChange]) -> bool:
    for change in changes:
        try:
            current = store.read(change.path)
        except NoteNotFoundError:
            if change.revision is None:
                continue
            return False
        if change.revision is None or current.revision != change.revision:
            return False
    return True


def _link_issue(link: ResolvedLink) -> dict[str, object]:
    return {
        "source_path": link.source_path,
        "target": link.link.target,
        "candidates": list(link.candidates),
    }


def _move_plan_payload(plan: LinkMovePlan) -> dict[str, object]:
    previous: dict[str, str | None] = {
        plan.source_path: plan.moved_revision,
        plan.destination_path: None,
    }
    for rewrite in plan.rewrites:
        previous[rewrite.output_path] = rewrite.previous_revision
    changes = [
        {
            "path": path,
            "previous_revision": previous.get(path),
            "revision": revision,
        }
        for path, revision in plan.changed_revisions
    ]
    return {
        "source_path": plan.source_path,
        "destination_path": plan.destination_path,
        "expected_revision": plan.moved_revision,
        "update_links": True,
        "changes": changes,
        "ambiguous": [_link_issue(link) for link in plan.ambiguous],
        "unresolved": [_link_issue(link) for link in plan.unresolved],
        "dynamic": [_link_issue(link) for link in plan.dynamic],
    }


def _move_plan_signature(plan: LinkMovePlan) -> str:
    return canonical_arguments_digest(_move_plan_payload(plan))


def _move_plan_context(plan: LinkMovePlan, *, update_links: bool) -> dict[str, object]:
    if not update_links:
        raise ValueError("link move plan context requires update_links")
    payload = _move_plan_payload(plan)
    payload["signature"] = canonical_arguments_digest(payload)
    return {
        "schema": _WORKFLOW_RESULT_SCHEMA,
        "phase": "prepared",
        "kind": "move",
        "primary_path": plan.destination_path,
        "primary_revision": plan.destination_revision,
        "previous_revision": plan.moved_revision,
        "tombstones": [],
        **payload,
    }


def _simple_move_context(source: str, destination: str, revision: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_path": source,
        "destination_path": destination,
        "expected_revision": revision,
        "update_links": False,
        "changes": [
            {"path": source, "previous_revision": revision, "revision": None},
            {"path": destination, "previous_revision": None, "revision": revision},
        ],
        "ambiguous": [],
        "unresolved": [],
        "dynamic": [],
    }
    payload["signature"] = canonical_arguments_digest(payload)
    return {
        "schema": _WORKFLOW_RESULT_SCHEMA,
        "phase": "prepared",
        "kind": "move",
        "primary_path": destination,
        "primary_revision": revision,
        "previous_revision": revision,
        "tombstones": [],
        **payload,
    }


def _frozen_move(payload: Mapping[str, object]) -> _FrozenMove:
    parsed = _workflow_result_from_payload(
        payload,
        operation_id="prepared",
        method="move",
        status="prepared",
        replayed=False,
        require_result=False,
    )
    source = payload.get("source_path")
    destination = payload.get("destination_path")
    update_links = payload.get("update_links")
    signature = payload.get("signature")
    if not isinstance(source, str) or not isinstance(destination, str):
        raise OperationLedgerError("prepared move paths are invalid")
    if not isinstance(update_links, bool):
        raise OperationLedgerError("prepared move update_links is invalid")
    try:
        expected = validate_revision(payload.get("expected_revision"))  # type: ignore[arg-type]
        checked_signature = validate_revision(signature)  # type: ignore[arg-type]
    except ValueError as exc:
        raise OperationLedgerError("prepared move revision or signature is invalid") from exc
    unsigned = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "schema",
            "phase",
            "kind",
            "primary_path",
            "primary_revision",
            "previous_revision",
            "tombstones",
            "signature",
            "applied",
            "error",
        }
    }
    if canonical_arguments_digest(unsigned) != checked_signature:
        raise OperationLedgerError("prepared move signature does not match its plan")
    return _FrozenMove(
        source_path=source,
        destination_path=destination,
        expected_revision=expected,
        update_links=update_links,
        changes=parsed.changes,
        ambiguous=parsed.ambiguous,
        unresolved=parsed.unresolved,
        dynamic=parsed.dynamic,
        signature=checked_signature,
    )


def _rebuild_frozen_move_plan(store: Any, frozen: _FrozenMove) -> LinkMovePlan:
    """Rebuild only still-pending rewrites from exact frozen revisions.

    A process may stop after the source rename but before every CAS-protected
    backlink write.  The destination copy supplies the moved note snapshot in
    that case.  Every reconstructed output must still match the durable final
    revision, so a peer edit is preserved instead of being folded into a new
    plan.
    """

    changes = {change.path: change for change in frozen.changes}
    destination_change = changes.get(frozen.destination_path)
    if destination_change is None or destination_change.revision is None:
        raise OperationLedgerError("prepared move has no destination revision")

    try:
        source_file = store.read_text(frozen.source_path)
    except NoteNotFoundError:
        source_file = None
    try:
        destination_file = store.read_text(frozen.destination_path)
    except NoteNotFoundError:
        destination_file = None

    if source_file is not None and source_file.revision != frozen.expected_revision:
        raise OperationCommitUncertain("move source was changed before reconciliation")
    if source_file is not None:
        if destination_file is not None:
            raise OperationCommitUncertain("move destination appeared before reconciliation")
        rebuilt = plan_move(
            build_vault_link_graph(store),
            frozen.source_path,
            frozen.destination_path,
        )
    else:
        if destination_file is None or destination_file.revision not in {
            frozen.expected_revision,
            destination_change.revision,
        }:
            raise OperationCommitUncertain("moved destination no longer has a frozen revision")
        current = build_vault_link_graph(store)
        snapshots = {note.path: note.content for note in current.notes}
        moved_content = snapshots.pop(frozen.destination_path, None)
        if moved_content is None or frozen.source_path in snapshots:
            raise OperationCommitUncertain("moved note snapshot cannot be reconstructed")
        snapshots[frozen.source_path] = moved_content
        rebuilt = plan_move(
            build_link_graph(snapshots),
            frozen.source_path,
            frozen.destination_path,
        )

    if rebuilt.destination_revision != destination_change.revision:
        raise OperationCommitUncertain("reconstructed move destination changed")
    planned_by_path = {rewrite.output_path: rewrite for rewrite in rebuilt.rewrites}
    if len(planned_by_path) != len(rebuilt.rewrites):
        raise OperationLedgerError("reconstructed move has duplicate rewrite paths")
    for rewrite in rebuilt.rewrites:
        change = changes.get(rewrite.output_path)
        if (
            change is None
            or change.previous_revision != rewrite.previous_revision
            or change.revision != rewrite.revision
        ):
            raise OperationCommitUncertain("reconstructed link rewrite changed")

    for change in frozen.changes:
        if change.path == frozen.source_path:
            continue
        try:
            current_file = store.read_text(change.path)
        except NoteNotFoundError:
            current_file = None
        if current_file is not None and current_file.revision == change.revision:
            continue
        if change.path == frozen.destination_path:
            if source_file is not None and current_file is None:
                continue
            if (
                source_file is None
                and current_file is not None
                and current_file.revision == frozen.expected_revision
                and change.path in planned_by_path
            ):
                continue
            raise OperationCommitUncertain("move destination has an unsafe partial state")
        if (
            current_file is not None
            and current_file.revision == change.previous_revision
            and change.path in planned_by_path
        ):
            continue
        raise OperationCommitUncertain("a frozen link rewrite raced with a peer edit")
    return rebuilt


def _workflow_result_from_frozen_move(
    operation_id: str,
    frozen: _FrozenMove,
    *,
    status: str,
    applied: bool,
    replayed: bool,
) -> DurableWorkflowResult:
    destination_revision = next(
        change.revision for change in frozen.changes if change.path == frozen.destination_path
    )
    return DurableWorkflowResult(
        operation_id=operation_id,
        method="move",
        status=status,
        primary_path=frozen.destination_path,
        primary_revision=destination_revision,
        previous_revision=frozen.expected_revision,
        changes=frozen.changes,
        tombstones=(),
        ambiguous=frozen.ambiguous,
        unresolved=frozen.unresolved,
        dynamic=frozen.dynamic,
        applied=applied,
        replayed=replayed,
        delivery=VaultDeliveryState.local_only(),
    )


def _prepared_workflow_context(
    row: Mapping[str, Any],
    *,
    method: str,
) -> dict[str, object] | None:
    payload = _json_object(row.get("result_json"), label="operation result")
    if not payload:
        return None
    if payload.get("schema") == _RESULT_SCHEMA and payload.get("error") == "workflow_prepare_uncertain":
        return None
    if payload.get("schema") != _WORKFLOW_RESULT_SCHEMA:
        raise OperationLedgerError("prepared workflow result schema is invalid")
    if payload.get("phase") != "prepared" or payload.get("kind") != method:
        raise OperationLedgerError("prepared workflow context is invalid")
    return dict(payload)


def _validate_workflow_payload(payload: Mapping[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    if len(encoded) > _MAX_WORKFLOW_RESULT_BYTES:
        raise VaultPathError("workflow result exceeds the durable result budget")


def _workflow_result_json(result: DurableWorkflowResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": _WORKFLOW_RESULT_SCHEMA,
        "phase": "result",
        "kind": result.method,
        "primary_path": result.primary_path,
        "primary_revision": result.primary_revision,
        "previous_revision": result.previous_revision,
        "changes": [
            {
                "path": change.path,
                "previous_revision": change.previous_revision,
                "revision": change.revision,
            }
            for change in result.changes
        ],
        "tombstones": list(result.tombstones),
        "ambiguous": [
            {
                "source_path": issue.source_path,
                "target": issue.target,
                "candidates": list(issue.candidates),
            }
            for issue in result.ambiguous
        ],
        "unresolved": [
            {
                "source_path": issue.source_path,
                "target": issue.target,
                "candidates": list(issue.candidates),
            }
            for issue in result.unresolved
        ],
        "dynamic": [
            {
                "source_path": issue.source_path,
                "target": issue.target,
                "candidates": list(issue.candidates),
            }
            for issue in result.dynamic
        ],
        "applied": result.applied,
    }
    _validate_workflow_payload(payload)
    return payload


def _workflow_result_from_row(
    row: Mapping[str, Any],
    *,
    replayed: bool,
) -> DurableWorkflowResult:
    status = str(row.get("status") or "")
    operation_id = str(row.get("id") or "")
    if status == "prepared":
        raise OperationLedgerError(f"Obsidian operation {operation_id!r} is prepared without a result")
    if status == "uncertain":
        raise OperationCommitUncertain(
            f"Obsidian operation {operation_id!r} has no proven local workflow result"
        )
    if status in _TERMINAL_FAILURES:
        raise OperationTerminalError(operation_id, status, _error_from_row(row))
    payload = _json_object(row.get("result_json"), label="operation result")
    return _workflow_result_from_payload(
        payload,
        operation_id=operation_id,
        method=str(row.get("method") or ""),
        status=status,
        replayed=replayed,
        require_result=True,
        delivery=_delivery_from_json(_json_object(row.get("delivery_json"), label="operation delivery")),
    )


def _workflow_result_from_payload(
    payload: Mapping[str, object],
    *,
    operation_id: str,
    method: str,
    status: str,
    replayed: bool,
    require_result: bool,
    delivery: VaultDeliveryState | None = None,
) -> DurableWorkflowResult:
    if payload.get("schema") != _WORKFLOW_RESULT_SCHEMA:
        raise OperationLedgerError("workflow result schema is missing or unsupported")
    phase = payload.get("phase")
    if require_result and phase != "result":
        raise OperationLedgerError("workflow result is not committed")
    if not require_result and phase != "prepared":
        raise OperationLedgerError("workflow prepared context is invalid")
    if payload.get("kind") != method:
        raise OperationLedgerError("workflow result method is invalid")
    if method not in {"move", "delete"}:
        raise OperationLedgerError("workflow result method is unsupported")
    primary_path = payload.get("primary_path")
    primary_revision = payload.get("primary_revision")
    previous_revision = payload.get("previous_revision")
    primary = _durable_path(primary_path, label="primary path")
    try:
        previous = validate_revision(previous_revision)  # type: ignore[arg-type]
        if primary_revision is not None:
            validate_revision(primary_revision)  # type: ignore[arg-type]
    except ValueError as exc:
        raise OperationLedgerError("workflow result revision is invalid") from exc
    changes = _path_changes(payload.get("changes"))
    tombstones = _path_tuple(payload.get("tombstones"), label="tombstones")
    ambiguous = _link_issues(payload.get("ambiguous"), label="ambiguous")
    unresolved = _link_issues(payload.get("unresolved"), label="unresolved")
    dynamic = _link_issues(payload.get("dynamic"), label="dynamic")
    applied_raw = payload.get("applied", False)
    if not isinstance(applied_raw, bool):
        raise OperationLedgerError("workflow applied field is not Boolean")
    selected_delivery = delivery or VaultDeliveryState.local_only()
    if not selected_delivery.local_write_complete:
        raise OperationLedgerError("successful workflow has no proven local commit")
    _validate_workflow_result_shape(
        method=method,
        primary_path=primary,
        primary_revision=str(primary_revision) if primary_revision is not None else None,
        changes=changes,
        tombstones=tombstones,
        ambiguous=ambiguous,
        unresolved=unresolved,
        dynamic=dynamic,
    )
    return DurableWorkflowResult(
        operation_id=operation_id,
        method=method,
        status=status,
        primary_path=primary,
        primary_revision=str(primary_revision) if primary_revision is not None else None,
        previous_revision=previous,
        changes=changes,
        tombstones=tombstones,
        ambiguous=ambiguous,
        unresolved=unresolved,
        dynamic=dynamic,
        applied=applied_raw,
        replayed=replayed,
        delivery=selected_delivery,
    )


def _validate_workflow_result_shape(
    *,
    method: str,
    primary_path: str,
    primary_revision: str | None,
    changes: Sequence[DurablePathChange],
    tombstones: Sequence[str],
    ambiguous: Sequence[DurableLinkIssue],
    unresolved: Sequence[DurableLinkIssue],
    dynamic: Sequence[DurableLinkIssue],
) -> None:
    primary_change = next((item for item in changes if item.path == primary_path), None)
    if method == "delete":
        if (
            primary_revision is not None
            or tuple(tombstones) != (primary_path,)
            or len(changes) != 1
            or primary_change is None
            or primary_change.revision is not None
            or ambiguous
            or unresolved
            or dynamic
        ):
            raise OperationLedgerError("delete workflow result shape is invalid")
        return
    if (
        primary_revision is None
        or tombstones
        or primary_change is None
        or primary_change.revision != primary_revision
        or not any(change.revision is None for change in changes)
    ):
        raise OperationLedgerError("move workflow result shape is invalid")


def _path_changes(value: object) -> tuple[DurablePathChange, ...]:
    if not isinstance(value, list) or not value:
        raise OperationLedgerError("workflow changes are invalid")
    changes: list[DurablePathChange] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "previous_revision", "revision"}:
            raise OperationLedgerError("workflow path change is invalid")
        path = item.get("path")
        previous = item.get("previous_revision")
        revision = item.get("revision")
        checked_path = _durable_path(path, label="path change path")
        if checked_path in seen:
            raise OperationLedgerError("workflow path change path is invalid")
        seen.add(checked_path)
        try:
            if previous is not None:
                validate_revision(previous)  # type: ignore[arg-type]
            if revision is not None:
                validate_revision(revision)  # type: ignore[arg-type]
        except ValueError as exc:
            raise OperationLedgerError("workflow path change revision is invalid") from exc
        changes.append(
            DurablePathChange(
                checked_path,
                str(previous) if previous is not None else None,
                str(revision) if revision is not None else None,
            )
        )
    return tuple(changes)


def _path_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise OperationLedgerError(f"workflow {label} are invalid")
    return tuple(_durable_path(item, label=label) for item in value)


def _durable_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise OperationLedgerError(f"workflow {label} is invalid")
    if (
        "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
    ):
        raise OperationLedgerError(f"workflow {label} is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise OperationLedgerError(f"workflow {label} is invalid")
    if PurePosixPath(*parts).as_posix() != value:
        raise OperationLedgerError(f"workflow {label} is invalid")
    return value


def _link_issues(value: object, *, label: str) -> tuple[DurableLinkIssue, ...]:
    if not isinstance(value, list):
        raise OperationLedgerError(f"workflow {label} links are invalid")
    issues: list[DurableLinkIssue] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"source_path", "target", "candidates"}:
            raise OperationLedgerError(f"workflow {label} link is invalid")
        source = item.get("source_path")
        target = item.get("target")
        candidates = item.get("candidates")
        if not isinstance(target, str):
            raise OperationLedgerError(f"workflow {label} link fields are invalid")
        issues.append(
            DurableLinkIssue(
                _durable_path(source, label=f"{label} source path"),
                target,
                _path_tuple(candidates, label=f"{label} candidates"),
            )
        )
    return tuple(issues)


def _row_has_workflow_schema(row: Mapping[str, Any]) -> bool:
    try:
        payload = _json_object(row.get("result_json"), label="operation result")
    except OperationLedgerError:
        return False
    return payload.get("schema") == _WORKFLOW_RESULT_SCHEMA


def canonical_arguments_digest(arguments: Mapping[str, object]) -> str:
    """Hash a closed, deterministic JSON representation of operation arguments."""

    if not isinstance(arguments, Mapping):
        raise TypeError("operation arguments must be a mapping")
    frozen = _canonical_value(arguments)
    encoded = json.dumps(
        frozen,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(encoded).hexdigest()


def _vault_root_matches(vault: Mapping[str, Any], notes: ObsidianService) -> bool:
    raw = str(vault.get("server_path") or "")
    configured = Path(raw)
    return (
        bool(raw)
        and configured.is_absolute()
        and configured == notes.store.root
        and configured.resolve(strict=False) == notes.store.root
        and not configured.is_symlink()
    )


def _vault_convention_matches(vault: Mapping[str, Any], notes: ObsidianService) -> bool:
    raw = vault.get("convention_json")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False
    if not isinstance(value, Mapping):
        return False
    expected = {
        "daily_folder": notes.convention.daily_folder,
        "daily_format": notes.convention.daily_format,
        "template_folder": notes.convention.template_folder,
        "attachment_folder": notes.convention.attachment_folder,
    }
    return all(key not in value or value[key] == expected[key] for key in expected)


def _recover_daily_day_from_digest(
    row: Mapping[str, Any],
    notes: ObsidianService,
    *,
    content: str,
    current: date,
    section: str | None = None,
    item: str | None = None,
) -> date | None:
    """Close the tiny prepare/context crash gap without guessing a new target."""

    candidates = {current + timedelta(days=offset) for offset in range(-2, 3)}
    created_at = row.get("created_at")
    if isinstance(created_at, str):
        try:
            created_day = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
        except ValueError:
            created_day = None
        if created_day is not None:
            candidates.update(created_day + timedelta(days=offset) for offset in range(-2, 3))
    expected_digest = str(row.get("arguments_digest") or "")
    for candidate in sorted(candidates):
        arguments = {
            "method": "daily_note",
            "path": _daily_path(notes, candidate),
            "day": candidate,
            "content": content,
        }
        if section is not None:
            arguments.update({"section": section, "item": item})
        if canonical_arguments_digest(arguments) == expected_digest:
            return candidate
    return None


def _canonical_value(value: object) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("operation arguments contain a non-finite number")
        return value
    if isinstance(value, PropertyValue):
        return {"type": value.type.value, "value": _canonical_value(value.value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("operation argument keys must be strings")
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"unsupported canonical operation argument: {type(value).__name__}")


def _typed_properties(properties: Mapping[str, PropertyInput]) -> dict[str, PropertyValue]:
    if not isinstance(properties, Mapping):
        raise TypeError("properties must be a mapping")
    typed: dict[str, PropertyValue] = {}
    for key, value in properties.items():
        if not isinstance(key, str):
            raise TypeError("property names must be strings")
        typed[key] = PropertyValue.coerce(value)
    return typed


def _validate_text(value: object, *, label: str) -> None:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{label} must be NUL-free text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc


def _optional_revision(expected_revision: str | None) -> None:
    if expected_revision is not None:
        validate_revision(expected_revision)


def _operation_id(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidOperationIdError("operation_id must be a string")
    operation_id = value.strip()
    if not operation_id or len(operation_id) > 200 or "\x00" in operation_id:
        raise InvalidOperationIdError("operation_id must be non-empty and at most 200 characters")
    try:
        operation_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise InvalidOperationIdError("operation_id must be valid UTF-8") from exc
    return operation_id


def _conflict_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("conflict_id must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 200
        or "\x00" in normalized
        or any(character in "\r\n" for character in normalized)
    ):
        raise ValueError("conflict_id must be a bounded single-line string")
    return normalized


def _optional_work_item_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("work_item_id must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 200 or "\x00" in normalized:
        raise ValueError("work_item_id must be non-empty and at most 200 characters")
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("work_item_id must be valid UTF-8") from exc
    return normalized


def _core_operation_applied(content: str, operation_id: str, arguments: str) -> bool:
    operation_digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    arguments_digest = hashlib.sha256(arguments.encode("utf-8")).hexdigest()
    return any(
        (f'<!-- friday:{method} operation="{operation_digest}" arguments="{arguments_digest}" -->') in content
        for method in ("create", "append")
    )


def _note_path(notes: ObsidianService, path: str | PurePosixPath) -> str:
    normalized = notes.store.normalize_path(path)
    pure = PurePosixPath(normalized)
    if pure.suffix == "":
        normalized += ".md"
    elif pure.suffix.casefold() != ".md":
        raise VaultPathError("note path must have the .md extension")
    return notes.store.normalize_path(normalized)


def _daily_path(notes: ObsidianService, day: date) -> str:
    pattern = notes.convention.daily_format
    if "%" in pattern:
        filename = day.strftime(pattern)
    else:
        replacements = {
            "YYYY": f"{day.year:04d}",
            "YY": f"{day.year % 100:02d}",
            "MM": f"{day.month:02d}",
            "DD": f"{day.day:02d}",
        }
        filename = _DAILY_TOKEN.sub(lambda match: replacements[match.group(0)], pattern)
    folder = notes.convention.daily_folder.strip("/")
    path = f"{folder}/{filename}" if folder else filename
    if not path.casefold().endswith(".md"):
        path += ".md"
    return _note_path(notes, path)


def _result_json(result: NoteWriteResult) -> dict[str, object]:
    return {
        "schema": _RESULT_SCHEMA,
        "path": result.path,
        "revision": result.revision,
        "previous_revision": result.previous_revision,
        "created": result.created,
        "applied": result.applied,
    }


def _delivery_json(delivery: VaultDeliveryState) -> dict[str, object]:
    return {
        "local_write_complete": delivery.local_write_complete,
        "server_scan_complete": delivery.server_scan_complete,
        "android_connected": delivery.android_connected,
        "android_completion": delivery.android_completion,
        "android_received": delivery.android_received,
        "obsidian_opened": delivery.obsidian_opened,
    }


def _uncommitted_delivery() -> VaultDeliveryState:
    return VaultDeliveryState(
        local_write_complete=False,
        server_scan_complete=False,
        android_connected=False,
        android_completion=None,
        android_received=False,
        obsidian_opened=False,
    )


def _result_from_row(row: Mapping[str, Any], *, replayed: bool) -> DurableNoteResult:
    status = str(row.get("status") or "")
    operation_id = str(row.get("id") or "")
    if status == "prepared":
        raise OperationLedgerError(f"Obsidian operation {operation_id!r} is prepared without a result")
    if status == "uncertain":
        raise OperationCommitUncertain(
            f"Obsidian operation {operation_id!r} has no proven local commit result"
        )
    if status in _TERMINAL_FAILURES:
        raise OperationTerminalError(operation_id, status, _error_from_row(row))
    result = _json_object(row.get("result_json"), label="operation result")
    delivery_raw = _json_object(row.get("delivery_json"), label="operation delivery")
    if result.get("schema") != _RESULT_SCHEMA:
        raise OperationLedgerError("operation result schema is missing or unsupported")
    path = result.get("path")
    revision = result.get("revision")
    if not isinstance(path, str) or not path:
        raise OperationLedgerError("operation result path is invalid")
    try:
        validate_revision(revision)  # type: ignore[arg-type]
    except ValueError as exc:
        raise OperationLedgerError("operation result revision is invalid") from exc
    previous = result.get("previous_revision")
    if previous is not None:
        try:
            validate_revision(previous)  # type: ignore[arg-type]
        except ValueError as exc:
            raise OperationLedgerError("operation previous revision is invalid") from exc
    delivery = _delivery_from_json(delivery_raw)
    if not delivery.local_write_complete:
        raise OperationLedgerError("successful operation has no proven local commit")
    if status in {"scan_complete", "delivery_pending", "delivered"} and not delivery.server_scan_complete:
        raise OperationLedgerError("operation state claims a scan without scan evidence")
    if status == "delivered" and not delivery.android_received:
        raise OperationLedgerError("delivered operation has no Android receipt evidence")
    return DurableNoteResult(
        operation_id=operation_id,
        method=str(row.get("method") or ""),
        status=status,
        path=path,
        revision=str(revision),
        previous_revision=str(previous) if previous is not None else None,
        created=_strict_bool(result.get("created"), label="created"),
        applied=_strict_bool(result.get("applied"), label="applied"),
        replayed=replayed,
        delivery=delivery,
    )


def _json_object(raw: object, *, label: str) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        raise OperationLedgerError(f"{label} is not JSON")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OperationLedgerError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise OperationLedgerError(f"{label} must be an object")
    return value


def _delivery_from_json(value: Mapping[str, Any]) -> VaultDeliveryState:
    expected = {
        "local_write_complete",
        "server_scan_complete",
        "android_connected",
        "android_completion",
        "android_received",
        "obsidian_opened",
    }
    if set(value) != expected:
        raise OperationLedgerError("operation delivery fields do not match the contract")
    completion = value["android_completion"]
    if completion is not None and (
        isinstance(completion, bool)
        or not isinstance(completion, (int, float))
        or not math.isfinite(float(completion))
        or not 0 <= float(completion) <= 100
    ):
        raise OperationLedgerError("operation Android completion is invalid")
    return VaultDeliveryState(
        local_write_complete=_strict_bool(value["local_write_complete"], label="local_write_complete"),
        server_scan_complete=_strict_bool(value["server_scan_complete"], label="server_scan_complete"),
        android_connected=_strict_bool(value["android_connected"], label="android_connected"),
        android_completion=float(completion) if completion is not None else None,
        android_received=_strict_bool(value["android_received"], label="android_received"),
        obsidian_opened=_strict_bool(value["obsidian_opened"], label="obsidian_opened"),
    )


def _strict_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise OperationLedgerError(f"operation {label} field is not Boolean")
    return value


def _validate_observed_delivery(delivery: VaultDeliveryState) -> None:
    if not isinstance(delivery, VaultDeliveryState):
        raise TypeError("sync adapter must return VaultDeliveryState")
    for field_name in (
        "local_write_complete",
        "server_scan_complete",
        "android_connected",
        "android_received",
        "obsidian_opened",
    ):
        if not isinstance(getattr(delivery, field_name), bool):
            raise OperationLedgerError(f"sync adapter returned non-Boolean {field_name}")
    if not delivery.local_write_complete:
        raise OperationLedgerError("sync adapter cannot revoke a proven local commit")
    completion = delivery.android_completion
    if completion is not None and (
        isinstance(completion, bool)
        or not isinstance(completion, (int, float))
        or not math.isfinite(float(completion))
        or not 0 <= float(completion) <= 100
    ):
        raise OperationLedgerError("sync adapter returned invalid Android completion")
    if delivery.android_received and not delivery.server_scan_complete:
        raise OperationLedgerError("Android receipt requires server scan evidence")


def _merge_delivery(
    previous: VaultDeliveryState,
    observed: VaultDeliveryState,
) -> VaultDeliveryState:
    """Keep revision proofs monotonic while connection state remains current."""

    return VaultDeliveryState(
        local_write_complete=True,
        server_scan_complete=previous.server_scan_complete or observed.server_scan_complete,
        android_connected=observed.android_connected,
        android_completion=observed.android_completion,
        android_received=previous.android_received or observed.android_received,
        obsidian_opened=previous.obsidian_opened or observed.obsidian_opened,
    )


def _error_from_row(row: Mapping[str, Any]) -> str:
    result = _json_object(row.get("result_json"), label="operation result")
    error = result.get("error")
    return str(error) if isinstance(error, str) and error else "operation_not_completed"


def _error_code(exc: Exception) -> str:
    name = type(exc).__name__
    pieces = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", name)
    return "_".join(piece.casefold() for piece in pieces).removesuffix("_error") or "note_error"


__all__ = [
    "DurableLinkIssue",
    "DurableNoteResult",
    "DurablePathChange",
    "DurableWorkflowResult",
    "NoteSyncRequest",
    "ObsidianOperationService",
    "ObsidianSyncAdapter",
    "OperationCommitUncertain",
    "OperationLedgerError",
    "OperationTerminalError",
    "canonical_arguments_digest",
]
