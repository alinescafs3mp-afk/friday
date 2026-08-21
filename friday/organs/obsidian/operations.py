"""Durable, owner-scoped orchestration around native Obsidian note writes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from friday.storage import FridayStorage

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
from .service import ObsidianService

_RESULT_SCHEMA = "friday.obsidian-note-operation.v1"
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

    def daily_note(
        self,
        operation_id: str,
        day: date | datetime | None = None,
        *,
        content: str = "",
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> DurableNoteResult:
        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        selected = day
        if selected is None:
            selected = self._frozen_daily_day(operation_id, content=content) or self._clock()
        if isinstance(selected, datetime):
            selected = selected.date()
        if not isinstance(selected, date):
            raise TypeError("daily note day must be a date or datetime")
        _optional_revision(expected_revision)
        _validate_text(content, label="content")
        canonical_path = _daily_path(self._notes, selected)
        arguments = {
            "method": "daily_note",
            "path": canonical_path,
            "day": selected,
            "content": content,
        }

        def mutate(_reconcile: bool) -> NoteWriteResult:
            try:
                current = self._notes.read_note(canonical_path)
            except NoteNotFoundError:
                if expected_revision is not None:
                    raise RevisionConflictError(expected_revision, None) from None
                return self._notes.daily_note(selected, content=content, operation_id=operation_id)
            if _core_operation_applied(current.content, operation_id, content):
                return self._notes.daily_note(selected, content=content, operation_id=operation_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise RevisionConflictError(expected_revision, current.revision)
            if not content:
                return self._notes.daily_note(selected, content="", operation_id=operation_id)
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

    def get_operation(self, operation_id: str) -> DurableNoteResult:
        operation_id = _operation_id(operation_id)
        self._assert_owner_vault()
        row = self._storage.get_obsidian_operation(self._owner_id, operation_id)
        if row is None:
            raise OperationLedgerError("Obsidian operation not found for owner")
        return _result_from_row(row, replayed=True)

    def refresh_delivery(self, operation_id: str) -> DurableNoteResult:
        """Persist only delivery facts explicitly observed by the injected adapter."""

        operation_id = _operation_id(operation_id)
        if self._sync is None:
            return self.get_operation(operation_id)
        with self._lock:
            self._assert_owner_vault()
            row = self._storage.get_obsidian_operation(self._owner_id, operation_id)
            if row is None:
                raise OperationLedgerError("Obsidian operation not found for owner")
            status = str(row["status"])
            if status in _TERMINAL_FAILURES or status in {"prepared", "uncertain"}:
                return _result_from_row(row, replayed=True)
            if status in {"committed", "reconciled"}:
                row = self._storage.transition_obsidian_operation(
                    self._owner_id, operation_id, "scan_pending"
                )
                status = "scan_pending"
            result = _result_from_row(row, replayed=True)
            if status == "scan_pending":
                self._request_scan(result)
            observed = self._sync.observe_delivery(self._sync_request(result))
            _validate_observed_delivery(observed)
            merged = _merge_delivery(result.delivery, observed)
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

    def _frozen_daily_day(self, operation_id: str, *, content: str) -> date | None:
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
        return _recover_daily_day_from_digest(row, self._notes, content=content, current=current)


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
    "DurableNoteResult",
    "NoteSyncRequest",
    "ObsidianOperationService",
    "ObsidianSyncAdapter",
    "OperationCommitUncertain",
    "OperationLedgerError",
    "OperationTerminalError",
    "canonical_arguments_digest",
]
