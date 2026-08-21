"""First-release native note operations for a server-side Obsidian vault."""

from __future__ import annotations

import hashlib
import heapq
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import PurePosixPath

from .contracts import (
    IdempotencyConflictError,
    InvalidOperationIdError,
    InvalidPropertyError,
    NoteAlreadyExistsError,
    NoteDocument,
    NoteNotFoundError,
    NoteSearchResult,
    NoteSummary,
    NoteWriteResult,
    ObsidianVaultConvention,
    PropertyInput,
    PropertyType,
    PropertyValue,
    RevisionConflictError,
    VaultDeliveryState,
    VaultLimitError,
    VaultPathError,
    validate_revision,
)
from .frontmatter import parse_frontmatter, set_frontmatter_properties
from .vault_store import VaultFile, VaultStore

_MAX_QUERY_CHARS = 1_000
_MAX_QUERY_TERMS = 32
_MAX_OPERATION_ID_CHARS = 256
_MAX_PROPERTY_COUNT = 256
_MAX_PROPERTY_LIST_ITEMS = 256
_OPERATION_MARKER = re.compile(
    r'<!-- friday:(?P<method>create|append) operation="(?P<operation>[0-9a-f]{64})" '
    r'arguments="(?P<arguments>[0-9a-f]{64})" -->'
)
_DAILY_TOKEN = re.compile(r"YYYY|YY|MM|DD")


class ObsidianService:
    """A filesystem-only service; synchronization evidence is added elsewhere."""

    def __init__(
        self,
        store: VaultStore,
        *,
        clock: Callable[[], date | datetime] | None = None,
        convention: ObsidianVaultConvention | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self.convention = convention or ObsidianVaultConvention()
        self._lock = threading.RLock()

    def list_notes(self) -> tuple[NoteSummary, ...]:
        notes: list[NoteSummary] = []
        for stored in self.store.iter_markdown_files(max_results=self.store.limits.max_list_results):
            note = _note_document(stored)
            notes.append(
                NoteSummary(
                    path=note.path,
                    title=note.title,
                    revision=note.revision,
                    size_bytes=note.size_bytes,
                    modified_at=note.modified_at,
                )
            )
        return tuple(notes)

    def search_notes(self, query: str, *, limit: int = 20) -> tuple[NoteSearchResult, ...]:
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS:
            raise ValueError("search query must be non-empty and at most 1000 characters")
        maximum = self.store.limits.max_search_results
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError(f"search limit must be between 1 and {maximum}")
        folded_query = query.strip().casefold()
        terms = tuple(dict.fromkeys(part for part in re.findall(r"\w+", folded_query) if part))
        if len(terms) > _MAX_QUERY_TERMS:
            raise ValueError(f"search query must contain at most {_MAX_QUERY_TERMS} distinct terms")

        def candidates() -> Iterator[NoteSearchResult]:
            for stored in self.store.iter_markdown_files():
                note = _note_document(stored)
                result = _search_match(note, folded_query, terms)
                if result is not None:
                    yield result

        return tuple(
            heapq.nsmallest(
                limit,
                candidates(),
                key=lambda item: (-item.score, item.path.casefold(), item.path),
            )
        )

    def read_note(self, path: str | PurePosixPath) -> NoteDocument:
        note_path = self._note_path(path)
        stored = self.store.read(note_path)
        return _note_document(stored)

    def create_note(
        self,
        path: str | PurePosixPath,
        content: str = "",
        *,
        properties: Mapping[str, PropertyInput] | None = None,
        operation_id: str | None = None,
    ) -> NoteWriteResult:
        note_path = self._note_path(path)
        self.store.validate_text_size(content)
        bounded_properties = _bounded_properties(
            {} if properties is None else properties,
            maximum_bytes=self.store.limits.max_note_bytes // 4,
        )
        rendered = set_frontmatter_properties(content, bounded_properties)
        self.store.validate_text_size(rendered)
        marker: str | None = None
        arguments_digest: str | None = None
        if operation_id is not None:
            operation_digest = _operation_digest(operation_id)
            arguments_digest = _text_digest(rendered)
            marker = _marker("create", operation_digest, arguments_digest)
            rendered = _append_visible_text(rendered, marker)
        self.store.validate_text_size(rendered)
        with self._lock:
            try:
                written = self.store.write_text(note_path, rendered, create_only=True)
            except NoteAlreadyExistsError:
                if marker is None or arguments_digest is None:
                    raise
                assert operation_id is not None  # marker creation proves this branch
                existing = self.store.read_text(note_path)
                replay = _find_operation(existing.text(), operation_id, method="create")
                if replay is None:
                    raise
                if replay != arguments_digest:
                    raise IdempotencyConflictError(
                        "create operation ID was reused with different arguments"
                    ) from None
                return _write_result(
                    existing, previous=None, created=True, applied=False, operation_id=operation_id
                )
        return _write_result(written, previous=None, created=True, applied=True, operation_id=operation_id)

    def append_note(
        self,
        path: str | PurePosixPath,
        text: str,
        *,
        operation_id: str,
        expected_revision: str | None = None,
    ) -> NoteWriteResult:
        note_path = self._note_path(path)
        self.store.validate_text_size(text)
        operation_digest = _operation_digest(operation_id)
        arguments_digest = _text_digest(text)
        marker = _marker("append", operation_digest, arguments_digest)
        with self._lock:
            existing = self.store.read_text(note_path)
            replay = _find_operation(existing.text(), operation_id, method="append")
            if replay is not None:
                if replay != arguments_digest:
                    raise IdempotencyConflictError("append operation ID was reused with different text")
                return _write_result(
                    existing,
                    previous=existing.revision,
                    created=False,
                    applied=False,
                    operation_id=operation_id,
                )
            _assert_expected(existing.revision, expected_revision)
            rendered = _append_visible_text(existing.text(), text)
            rendered = _append_visible_text(rendered, marker)
            self.store.validate_text_size(rendered)
            written = self.store.write_text(
                note_path,
                rendered,
                expected_revision=existing.revision,
            )
        return _write_result(
            written,
            previous=existing.revision,
            created=False,
            applied=True,
            operation_id=operation_id,
        )

    def set_properties(
        self,
        path: str | PurePosixPath,
        properties: Mapping[str, PropertyInput],
        *,
        expected_revision: str | None = None,
    ) -> NoteWriteResult:
        note_path = self._note_path(path)
        bounded_properties = _bounded_properties(
            properties,
            maximum_bytes=self.store.limits.max_note_bytes // 4,
        )
        with self._lock:
            existing = self.store.read_text(note_path)
            _assert_expected(existing.revision, expected_revision)
            rendered = set_frontmatter_properties(existing.text(), bounded_properties)
            self.store.validate_text_size(rendered)
            if rendered == existing.text():
                return _write_result(
                    existing,
                    previous=existing.revision,
                    created=False,
                    applied=False,
                    operation_id=None,
                )
            written = self.store.write_text(
                note_path,
                rendered,
                expected_revision=existing.revision,
            )
        return _write_result(
            written,
            previous=existing.revision,
            created=False,
            applied=True,
            operation_id=None,
        )

    def daily_note(
        self,
        day: date | datetime | None = None,
        *,
        content: str = "",
        operation_id: str | None = None,
    ) -> NoteWriteResult:
        selected = day or self._clock()
        if isinstance(selected, datetime):
            selected = selected.date()
        if not isinstance(selected, date):
            raise TypeError("daily note day must be a date or datetime")
        filename = _daily_filename(selected, self.convention.daily_format)
        folder = self.convention.daily_folder.strip("/")
        path = f"{folder}/{filename}" if folder else filename
        if not path.casefold().endswith(".md"):
            path += ".md"
        note_path = self._note_path(path)

        with self._lock:
            try:
                existing = self.store.read_text(note_path)
            except NoteNotFoundError:
                return self.create_note(note_path, content, operation_id=operation_id)
            if not content:
                return _write_result(
                    existing,
                    previous=existing.revision,
                    created=False,
                    applied=False,
                    operation_id=operation_id,
                )
            if operation_id is not None:
                arguments_digest = _text_digest(content)
                prior = _find_operation(existing.text(), operation_id, method="create")
                if prior is None:
                    prior = _find_operation(existing.text(), operation_id, method="append")
                if prior is not None:
                    if prior != arguments_digest:
                        raise IdempotencyConflictError(
                            "daily-note operation ID was reused with different text"
                        )
                    return _write_result(
                        existing,
                        previous=existing.revision,
                        created=False,
                        applied=False,
                        operation_id=operation_id,
                    )
        if operation_id is None:
            raise InvalidOperationIdError("operation_id is required when appending to a daily note")
        return self.append_note(note_path, content, operation_id=operation_id)

    def _note_path(self, path: str | PurePosixPath) -> str:
        normalized = self.store.normalize_path(path)
        pure = PurePosixPath(normalized)
        if pure.suffix == "":
            normalized += ".md"
        elif pure.suffix.casefold() != ".md":
            raise VaultPathError("note path must have the .md extension")
        return self.store.normalize_ordinary_note_path(normalized)


def _note_document(stored: VaultFile) -> NoteDocument:
    content = stored.text()
    parsed = parse_frontmatter(content)
    return NoteDocument(
        path=stored.path,
        title=_note_title(stored.path, parsed.properties),
        content=content,
        body=parsed.body,
        properties=parsed.properties,
        revision=stored.revision,
        size_bytes=stored.size_bytes,
        modified_at=stored.modified_at,
    )


def _search_match(
    note: NoteDocument,
    folded_query: str,
    terms: tuple[str, ...],
) -> NoteSearchResult | None:
    folded_path = note.path.removesuffix(".md").casefold()
    folded_title = note.title.casefold()
    folded_body = note.body.casefold()
    channels: list[str] = []
    score = 0.0
    if folded_path == folded_query or note.path.casefold() == folded_query:
        channels.append("exact_path")
        score += 100.0
    if folded_title == folded_query:
        channels.append("exact_title")
        score += 90.0
    if folded_query in folded_path or folded_query in folded_title:
        channels.append("path_title")
        score += 35.0
    if folded_query in folded_body:
        channels.append("lexical")
        score += 25.0 + min(folded_body.count(folded_query), 10)
    term_hits = sum((term in folded_path) + (term in folded_title) + (term in folded_body) for term in terms)
    if term_hits:
        if "lexical" not in channels:
            channels.append("lexical")
        score += float(term_hits)
    if not channels:
        return None
    return NoteSearchResult(
        path=note.path,
        title=note.title,
        excerpt=_excerpt(note.body, folded_query, terms),
        revision=note.revision,
        score=score,
        match_channels=tuple(channels),
        modified_at=note.modified_at,
    )


def _bounded_properties(
    properties: Mapping[str, PropertyInput], *, maximum_bytes: int
) -> dict[str, PropertyValue]:
    """Take one bounded immutable snapshot before rendering properties."""

    if not isinstance(properties, Mapping):
        raise TypeError("properties must be a mapping")
    budget = max(1, maximum_bytes)
    remaining = budget

    def charge(amount: int) -> None:
        nonlocal remaining
        if amount > remaining:
            raise VaultLimitError(f"note properties exceed the pre-render budget of {budget} bytes")
        remaining -= amount

    def charge_text(value: str) -> None:
        if len(value) > remaining:
            charge(remaining + 1)
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return  # The typed property contract reports the stable input error.
        charge(len(encoded))

    def snapshot_payload(value: object) -> object:
        if isinstance(value, str):
            charge_text(value)
            return value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            items: list[object] = []
            for index, item in enumerate(value):
                if index >= _MAX_PROPERTY_LIST_ITEMS:
                    raise VaultLimitError(
                        f"note property list exceeds the maximum item count of {_MAX_PROPERTY_LIST_ITEMS}"
                    )
                charge(8)
                if isinstance(item, str):
                    charge_text(item)
                items.append(item)
            return tuple(items)
        if isinstance(value, int) and not isinstance(value, bool):
            charge(max(1, (value.bit_length() * 30_103) // 100_000 + 2))
            return value
        charge(32)
        return value

    snapshot: dict[str, PropertyValue] = {}
    for index, (key, value) in enumerate(properties.items()):
        if index >= _MAX_PROPERTY_COUNT:
            raise VaultLimitError(f"note properties exceed the maximum field count of {_MAX_PROPERTY_COUNT}")
        if not isinstance(key, str):
            raise InvalidPropertyError("property name must be a string")
        charge(16)
        charge_text(key)
        if isinstance(value, PropertyValue):
            snapshot_payload(value.value)
            snapshot[key] = value
            continue
        if isinstance(value, Mapping):
            fields: dict[object, object] = {}
            for field_index, (field, item) in enumerate(value.items()):
                if field_index >= 2:
                    raise VaultLimitError("typed property object exceeds its closed field count")
                fields[field] = item
            if set(fields) != {"type", "value"}:
                raise InvalidPropertyError("typed property object must contain only type and value")
            raw_type = fields["type"]
            if isinstance(raw_type, str):
                charge_text(raw_type)
            raw_value = fields["value"]
            if isinstance(raw_value, Mapping):
                raise InvalidPropertyError("nested typed property values are not supported")
            stable = {"type": raw_type, "value": snapshot_payload(raw_value)}
            snapshot[key] = PropertyValue.coerce(stable)
            continue
        snapshot[key] = PropertyValue.coerce(snapshot_payload(value))  # type: ignore[arg-type]
    return snapshot


def _assert_expected(actual: str, expected: str | None) -> None:
    if expected is None:
        return
    validate_revision(expected)
    if actual != expected:
        raise RevisionConflictError(expected, actual)


def _operation_digest(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > _MAX_OPERATION_ID_CHARS:
        raise InvalidOperationIdError("operation_id must be non-empty and at most 256 characters")
    if "\x00" in operation_id:
        raise InvalidOperationIdError("operation_id must not contain NUL")
    try:
        encoded = operation_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise InvalidOperationIdError("operation_id must be valid UTF-8") from exc
    return hashlib.sha256(encoded).hexdigest()


def _text_digest(text: str) -> str:
    if not isinstance(text, str) or "\x00" in text:
        raise ValueError("note text must be NUL-free text")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("note text must be valid UTF-8") from exc
    return hashlib.sha256(encoded).hexdigest()


def _marker(method: str, operation_digest: str, arguments_digest: str) -> str:
    return f'<!-- friday:{method} operation="{operation_digest}" arguments="{arguments_digest}" -->'


def _find_operation(content: str, operation_id: str, *, method: str) -> str | None:
    digest = _operation_digest(operation_id)
    for match in _OPERATION_MARKER.finditer(content):
        if match.group("method") == method and match.group("operation") == digest:
            return match.group("arguments")
    return None


def _append_visible_text(existing: str, addition: str) -> str:
    _text_digest(addition)
    if not existing:
        combined = addition
    elif existing.endswith(("\n", "\r")) or addition.startswith(("\n", "\r")):
        combined = existing + addition
    else:
        combined = f"{existing}\n{addition}"
    if combined and not combined.endswith(("\n", "\r")):
        combined += "\n"
    return combined


def _write_result(
    stored: VaultFile,
    *,
    previous: str | None,
    created: bool,
    applied: bool,
    operation_id: str | None,
) -> NoteWriteResult:
    return NoteWriteResult(
        path=stored.path,
        revision=stored.revision,
        previous_revision=previous,
        created=created,
        applied=applied,
        operation_id=operation_id,
        delivery=VaultDeliveryState.local_only(),
    )


def _note_title(path: str, properties: Mapping[str, object]) -> str:
    title_property = properties.get("title")
    if getattr(title_property, "type", None) is PropertyType.TEXT:
        value = getattr(title_property, "value", "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return PurePosixPath(path).stem


def _daily_filename(day: date, pattern: str) -> str:
    if not isinstance(pattern, str) or not pattern or len(pattern) > 128:
        raise VaultPathError("daily note format must be non-empty and bounded")
    if "%" in pattern:
        rendered = day.strftime(pattern)
    else:
        replacements = {
            "YYYY": f"{day.year:04d}",
            "YY": f"{day.year % 100:02d}",
            "MM": f"{day.month:02d}",
            "DD": f"{day.day:02d}",
        }
        rendered = _DAILY_TOKEN.sub(lambda match: replacements[match.group(0)], pattern)
    if not rendered or rendered in {".", ".."} or "/" in rendered or "\\" in rendered:
        raise VaultPathError("daily note format produced an unsafe filename")
    return rendered


def _excerpt(body: str, folded_query: str, terms: tuple[str, ...], *, width: int = 240) -> str:
    visible_body = _OPERATION_MARKER.sub("", body)
    flattened = re.sub(r"\s+", " ", visible_body).strip()
    folded = flattened.casefold()
    index = folded.find(folded_query)
    if index < 0:
        indexes = [folded.find(term) for term in terms if folded.find(term) >= 0]
        index = min(indexes, default=0)
    start = max(0, index - width // 3)
    end = min(len(flattened), start + width)
    excerpt = flattened[start:end]
    if start:
        excerpt = f"…{excerpt}"
    if end < len(flattened):
        excerpt = f"{excerpt}…"
    return excerpt


__all__ = ["ObsidianService"]
