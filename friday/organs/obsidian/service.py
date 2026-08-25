"""First-release native note operations for a server-side Obsidian vault."""

from __future__ import annotations

import hashlib
import heapq
import re
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from friday.morphology import LEXICAL_MIN_STEM_INPUT, stem

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
    TemplateSummary,
    VaultDeliveryState,
    VaultLimitError,
    VaultPathError,
    validate_revision,
)
from .frontmatter import parse_frontmatter, set_frontmatter_properties
from .operation_receipts import NoteOperationReceipt, NoteOperationReceiptStore
from .structured_notes import append_section_item
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
_SEARCH_STOPWORDS = frozenset(
    {
        "в",
        "где",
        "и",
        "из",
        "которую",
        "который",
        "мы",
        "на",
        "найди",
        "о",
        "об",
        "по",
        "примерно",
        "про",
        "что",
        "я",
    }
)
_SEMANTIC_GROUPS = (
    frozenset({"документ", "файл", "заметк", "материал"}),
    frozenset({"поиск", "выдач", "наход", "индекс"}),
    frozenset({"список", "набор", "кандидат", "пул", "выборк"}),
    frozenset({"маленьк", "огранич", "недостат", "коротк", "узк"}),
    frozenset({"стар", "давн", "архивн"}),
    frozenset({"исчез", "непопад", "пропуск", "теря"}),
)
_EARLY_MONTH = re.compile(
    r"\b(?:в\s+)?начал\w*\s+"
    r"(?P<month>январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|"
    r"июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\s+"
    r"(?P<year>20\d{2})",
    re.IGNORECASE,
)
_TEXT_CREATED_DATE = re.compile(r"(?P<year>20\d{2})[-_./](?P<month>\d{1,2})[-_./](?P<day>\d{1,2})")
_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


class ObsidianService:
    """A filesystem-only service; synchronization evidence is added elsewhere."""

    def __init__(
        self,
        store: VaultStore,
        *,
        clock: Callable[[], date | datetime] | None = None,
        convention: ObsidianVaultConvention | None = None,
        operation_receipt_root: str | Path | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self.convention = convention or ObsidianVaultConvention()
        self._lock = threading.RLock()
        root_stat = self.store.root.stat()
        receipt_identity = f"{self.store.root.resolve()}\0{root_stat.st_dev}\0{root_stat.st_ino}"
        identity = hashlib.sha256(receipt_identity.encode("utf-8", errors="strict")).hexdigest()[:24]
        self._operation_receipt_root = (
            Path(operation_receipt_root)
            if operation_receipt_root is not None
            else self.store.root.parent / ".friday-obsidian-operation-receipts" / identity
        )
        self._operation_receipts: NoteOperationReceiptStore | None = None

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

    def list_templates(self) -> tuple[TemplateSummary, ...]:
        """List body-free metadata for templates in the configured folder."""

        folder = self.store.normalize_path(self.convention.template_folder)
        prefix = f"{folder}/"
        templates: list[TemplateSummary] = []
        for stored in self.store.iter_markdown_files_under(
            folder,
            max_results=self.store.limits.max_list_results,
        ):
            note = _note_document(stored)
            if not note.path.startswith(prefix):
                raise VaultPathError("template escaped the configured folder")
            relative = note.path[len(prefix) :]
            name = relative[:-3]
            if not name:
                continue
            templates.append(
                TemplateSummary(
                    name=name,
                    path=note.path,
                    title=note.title,
                    revision=note.revision,
                    modified_at=note.modified_at,
                )
            )
        return tuple(templates)

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
        query_features = _semantic_features(folded_query)
        date_window = _approximate_date_window(folded_query)

        def candidates() -> Iterator[NoteSearchResult]:
            for stored in self.store.iter_markdown_files():
                note = _note_document(stored)
                result = _search_match(
                    note,
                    folded_query,
                    terms,
                    query_features=query_features,
                    date_window=date_window,
                )
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

    def render_create_content(
        self,
        content: str,
        *,
        properties: Mapping[str, PropertyInput] | None = None,
    ) -> str:
        """Render the exact user-visible bytes for a create before publication."""

        self.store.validate_text_size(content)
        bounded_properties = _bounded_properties(
            {} if properties is None else properties,
            maximum_bytes=self.store.limits.max_note_bytes // 4,
        )
        rendered = set_frontmatter_properties(content, bounded_properties)
        self.store.validate_text_size(rendered)
        return rendered

    def render_append_content(self, content: str, text: str) -> str:
        """Render one append without adding Friday's legacy operation comment."""

        self.store.validate_text_size(content)
        self.store.validate_text_size(text)
        rendered = _append_visible_text(content, text)
        self.store.validate_text_size(rendered)
        return rendered

    def render_prepend_content(self, content: str, text: str) -> str:
        """Prepend to the Markdown body while retaining frontmatter at byte zero."""

        self.store.validate_text_size(content)
        self.store.validate_text_size(text)
        parsed = parse_frontmatter(content)
        rendered_body = _prepend_visible_text(parsed.body, text, newline=parsed.newline)
        if parsed.has_frontmatter:
            header_end = len(content) - len(parsed.body)
            header = content[:header_end]
            if not header.endswith(("\n", "\r")):
                header += parsed.newline
            rendered = header + rendered_body
        else:
            rendered = rendered_body
        self.store.validate_text_size(rendered)
        return rendered

    def render_replace_content(self, content: str) -> str:
        """Validate and return the exact bytes requested for a full replacement."""

        self.store.validate_text_size(content)
        return content

    def render_append_section_content(self, content: str, section: str, item: str) -> str:
        """Render one structured append without an in-note operation comment."""

        self.store.validate_text_size(content)
        edit = append_section_item(content, section, item)
        self.store.validate_text_size(edit.content)
        return edit.content

    def _idempotent_update(
        self,
        note_path: str,
        operation_id: str,
        *,
        receipt_method: str,
        arguments_payload: str,
        render: Callable[[str], str],
        expected_revision: str | None,
        create_if_missing: bool,
        legacy_methods: Sequence[str] = ("append",),
    ) -> NoteWriteResult:
        operation_digest = _operation_digest(operation_id)
        arguments_digest = _text_digest(arguments_payload)
        with self._lock:
            prior = self._receipt_store().lookup(
                operation_digest=operation_digest,
                method=receipt_method,
                arguments_digest=arguments_digest,
                note_path=note_path,
            )
            if (
                prior is not None
                and expected_revision is not None
                and prior.base_revision != expected_revision
            ):
                raise IdempotencyConflictError(
                    f"{receipt_method} operation ID was reused with a different expected revision"
                )
            if prior is not None and prior.state == "committed":
                return _receipt_result(prior, operation_id=operation_id, applied=False)
            try:
                observed = self.store.read_text(note_path)
            except NoteNotFoundError:
                observed = None
            legacy_clean: str | None = None
            legacy_method: str | None = None
            if observed is not None:
                legacy_method, replay = _find_operation_any(
                    observed.text(),
                    operation_id,
                    methods=legacy_methods,
                )
                if replay is not None and replay != arguments_digest:
                    raise IdempotencyConflictError(
                        f"{receipt_method} operation ID was reused with different arguments"
                    )
                if legacy_method is not None:
                    legacy_clean = _remove_operation_marker(
                        observed.text(),
                        operation_id,
                        method=legacy_method,
                        arguments_digest=arguments_digest,
                        target_revision=None,
                    )
                    if legacy_clean is None:
                        raise IdempotencyConflictError("legacy operation marker is ambiguous")

            if prior is not None:
                receipt = prior
                prepared = False
                base_revision = receipt.base_revision
                target_revision = receipt.target_revision
                target_content = ""
            elif observed is None:
                if not create_if_missing:
                    raise NoteNotFoundError(note_path)
                if expected_revision is not None:
                    raise RevisionConflictError(expected_revision, None)
                base_revision = None
                target_content = render("")
                created = True
            elif legacy_clean is not None:
                base_revision = expected_revision
                target_content = legacy_clean
                created = False
            else:
                _assert_expected(observed.revision, expected_revision)
                base_revision = observed.revision
                target_content = render(observed.text())
                created = False
            if prior is None:
                self.store.validate_text_size(target_content)
                target_revision = _text_digest(target_content)
                receipt, prepared = self._receipt_store().prepare(
                    operation_digest=operation_digest,
                    method=receipt_method,
                    arguments_digest=arguments_digest,
                    note_path=note_path,
                    base_revision=base_revision,
                    target_revision=target_revision,
                    created=created,
                )

            if not prepared:
                try:
                    observed = self.store.read_text(note_path)
                except NoteNotFoundError:
                    observed = None
                if observed is not None and observed.revision == receipt.target_revision:
                    committed = self._receipt_store().commit(operation_digest)
                    return _receipt_result(committed, operation_id=operation_id, applied=False)
                if observed is not None:
                    legacy_method, replay = _find_operation_any(
                        observed.text(),
                        operation_id,
                        methods=legacy_methods,
                    )
                    if replay is not None and replay != arguments_digest:
                        raise IdempotencyConflictError(
                            f"{receipt_method} operation ID was reused with different arguments"
                        )
                    if legacy_method is not None:
                        cleaned = _remove_operation_marker(
                            observed.text(),
                            operation_id,
                            method=legacy_method,
                            arguments_digest=arguments_digest,
                            target_revision=receipt.target_revision,
                        )
                        if cleaned is not None:
                            written = self.store.write_text(
                                note_path,
                                cleaned,
                                expected_revision=observed.revision,
                            )
                            committed = self._receipt_store().commit(operation_digest)
                            if written.revision != committed.target_revision:
                                raise RevisionConflictError(
                                    committed.target_revision,
                                    written.revision,
                                )
                            return _receipt_result(
                                committed,
                                operation_id=operation_id,
                                applied=False,
                            )
                if receipt.base_revision is None:
                    if observed is not None:
                        raise NoteAlreadyExistsError(note_path)
                    target_content = render("")
                    if _text_digest(target_content) != receipt.target_revision:
                        raise IdempotencyConflictError("prepared operation target changed")
                    written = self.store.write_text(note_path, target_content, create_only=True)
                else:
                    if observed is None or observed.revision != receipt.base_revision:
                        raise RevisionConflictError(
                            receipt.base_revision,
                            None if observed is None else observed.revision,
                        )
                    target_content = render(observed.text())
                    if _text_digest(target_content) != receipt.target_revision:
                        raise IdempotencyConflictError("prepared operation target changed")
                    written = self.store.write_text(
                        note_path,
                        target_content,
                        expected_revision=receipt.base_revision,
                    )
                committed = self._receipt_store().commit(operation_digest)
                if written.revision != committed.target_revision:
                    raise RevisionConflictError(committed.target_revision, written.revision)
                return _receipt_result(committed, operation_id=operation_id, applied=True)

            if legacy_clean is not None and observed is not None:
                written = self.store.write_text(
                    note_path,
                    legacy_clean,
                    expected_revision=observed.revision,
                )
                applied = False
            elif base_revision is None:
                written = self.store.write_text(note_path, target_content, create_only=True)
                applied = True
            else:
                written = self.store.write_text(
                    note_path,
                    target_content,
                    expected_revision=base_revision,
                )
                applied = True
            if written.revision != receipt.target_revision:
                raise RevisionConflictError(receipt.target_revision, written.revision)
            committed = self._receipt_store().commit(operation_digest)
            return _receipt_result(committed, operation_id=operation_id, applied=applied)

    def create_note(
        self,
        path: str | PurePosixPath,
        content: str = "",
        *,
        properties: Mapping[str, PropertyInput] | None = None,
        operation_id: str | None = None,
    ) -> NoteWriteResult:
        note_path = self._note_path(path)
        rendered = self.render_create_content(content, properties=properties)
        if operation_id is None:
            with self._lock:
                written = self.store.write_text(note_path, rendered, create_only=True)
            return _write_result(
                written,
                previous=None,
                created=True,
                applied=True,
                operation_id=None,
            )
        operation_digest = _operation_digest(operation_id)
        arguments_digest = _text_digest(rendered)
        target_revision = _text_digest(rendered)
        with self._lock:
            prior = self._receipt_store().lookup(
                operation_digest=operation_digest,
                method="create",
                arguments_digest=arguments_digest,
                note_path=note_path,
            )
            if prior is not None and prior.state == "committed":
                return _receipt_result(prior, operation_id=operation_id, applied=False)
            try:
                observed = self.store.read_text(note_path)
            except NoteNotFoundError:
                observed = None
            legacy_clean: str | None = None
            if observed is not None:
                replay = _find_operation(observed.text(), operation_id, method="create")
                if replay is not None and replay != arguments_digest:
                    raise IdempotencyConflictError("create operation ID was reused with different arguments")
                if replay is not None:
                    legacy_clean = _remove_operation_marker(
                        observed.text(),
                        operation_id,
                        method="create",
                        arguments_digest=arguments_digest,
                        target_revision=None if prior is None else prior.target_revision,
                    )
                    if legacy_clean is None:
                        raise IdempotencyConflictError("legacy create operation marker is ambiguous")
            if prior is None and observed is not None and legacy_clean is None:
                raise NoteAlreadyExistsError(note_path)
            base_revision = None
            if prior is None and legacy_clean is not None:
                assert observed is not None
                base_revision = observed.revision
            if prior is None and legacy_clean is not None:
                target_revision = _text_digest(legacy_clean)
            if prior is None:
                receipt, _prepared = self._receipt_store().prepare(
                    operation_digest=operation_digest,
                    method="create",
                    arguments_digest=arguments_digest,
                    note_path=note_path,
                    base_revision=base_revision,
                    target_revision=target_revision,
                    created=True,
                )
            else:
                receipt = prior
                base_revision = receipt.base_revision
                target_revision = receipt.target_revision
            _assert_receipt_target(
                receipt,
                base_revision=base_revision,
                target_revision=target_revision,
            )
            if receipt.base_revision is not None and observed is None:
                raise RevisionConflictError(receipt.base_revision, None)
            applied = False
            if legacy_clean is not None and observed is not None:
                written = self.store.write_text(
                    note_path,
                    legacy_clean,
                    expected_revision=observed.revision,
                )
            elif observed is None:
                written = self.store.write_text(note_path, rendered, create_only=True)
                applied = True
            elif observed.revision == target_revision:
                written = observed
            else:
                raise NoteAlreadyExistsError(note_path)
            if written.revision != target_revision:
                raise RevisionConflictError(target_revision, written.revision)
            receipt = self._receipt_store().commit(operation_digest)
            return _receipt_result(receipt, operation_id=operation_id, applied=applied)

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
        return self._idempotent_update(
            note_path,
            operation_id,
            receipt_method="append",
            arguments_payload=text,
            render=lambda current: self.render_append_content(current, text),
            expected_revision=expected_revision,
            create_if_missing=False,
        )

    def prepend_note(
        self,
        path: str | PurePosixPath,
        text: str,
        *,
        operation_id: str,
        expected_revision: str | None = None,
    ) -> NoteWriteResult:
        note_path = self._note_path(path)
        self.store.validate_text_size(text)
        return self._idempotent_update(
            note_path,
            operation_id,
            receipt_method="prepend",
            arguments_payload=text,
            render=lambda current: self.render_prepend_content(current, text),
            expected_revision=expected_revision,
            create_if_missing=False,
            legacy_methods=(),
        )

    def replace_note(
        self,
        path: str | PurePosixPath,
        content: str,
        *,
        operation_id: str,
        expected_revision: str,
    ) -> NoteWriteResult:
        """Replace the complete note only under an explicit revision CAS."""

        note_path = self._note_path(path)
        validate_revision(expected_revision)
        rendered = self.render_replace_content(content)
        return self._idempotent_update(
            note_path,
            operation_id,
            receipt_method="replace",
            arguments_payload=rendered,
            render=lambda _current: rendered,
            expected_revision=expected_revision,
            create_if_missing=False,
            legacy_methods=(),
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

    def append_section(
        self,
        path: str | PurePosixPath,
        section: str,
        item: str,
        *,
        operation_id: str,
        expected_revision: str | None = None,
        create_if_missing: bool = False,
    ) -> NoteWriteResult:
        """Idempotently add one item under one exact Markdown section."""

        note_path = self._note_path(path)
        marker_payload = _section_marker_payload(section, item)
        return self._idempotent_update(
            note_path,
            operation_id,
            receipt_method="append_section",
            arguments_payload=marker_payload,
            render=lambda current: self.render_append_section_content(current, section, item),
            expected_revision=expected_revision,
            create_if_missing=create_if_missing,
        )

    def daily_note(
        self,
        day: date | datetime | None = None,
        *,
        content: str = "",
        section: str | None = None,
        item: str | None = None,
        operation_id: str | None = None,
        expected_revision: str | None = None,
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

        if (section is None) != (item is None):
            raise ValueError("daily note section and item must be supplied together")
        if section is not None and item is not None:
            if content:
                raise ValueError("daily note accepts either content or a structured section item")
            if operation_id is None:
                raise InvalidOperationIdError("operation_id is required for a daily-note section")
            return self.append_section(
                note_path,
                section,
                item,
                operation_id=operation_id,
                expected_revision=expected_revision,
                create_if_missing=True,
            )
        try:
            existing = self.store.read_text(note_path)
        except NoteNotFoundError:
            existing = None
        if not content:
            if existing is None:
                return self.create_note(note_path, "", operation_id=operation_id)
            return _write_result(
                existing,
                previous=existing.revision,
                created=False,
                applied=False,
                operation_id=operation_id,
            )
        if operation_id is None:
            raise InvalidOperationIdError("operation_id is required when writing a daily note")

        def render(current: str) -> str:
            if not current:
                return self.render_create_content(content)
            return self.render_append_content(current, content)

        return self._idempotent_update(
            note_path,
            operation_id,
            receipt_method="daily_note",
            arguments_payload=content,
            render=render,
            expected_revision=expected_revision,
            create_if_missing=True,
            legacy_methods=("create", "append"),
        )

    def reconcile_operation_receipt(
        self,
        operation_id: str,
        *,
        method: str,
        path: str | PurePosixPath,
        base_revision: str | None,
        target_revision: str,
    ) -> NoteOperationReceipt | None:
        """Observe and settle a local receipt without replaying the note write.

        A committed sidecar remains historical proof after a later user edit.
        A prepared sidecar is committed only when the current file is still the
        exact frozen target.  Missing or mismatched evidence causes no vault
        write and remains available to the caller as unresolved uncertainty.
        """

        if method not in {"create", "append"}:
            raise ValueError("only create and append receipts are reconcilable")
        note_path = self._note_path(path)
        if base_revision is not None:
            validate_revision(base_revision)
        validate_revision(target_revision)
        operation_digest = _operation_digest(operation_id)
        with self._lock:
            receipt = self._receipt_store().inspect(operation_digest)
            if receipt is None:
                return None
            expected_created = method == "create"
            if (
                receipt.method != method
                or receipt.note_path != note_path
                or receipt.base_revision != base_revision
                or receipt.target_revision != target_revision
                or receipt.created is not expected_created
            ):
                raise IdempotencyConflictError(
                    "operation receipt does not match the frozen ledger target"
                )
            if receipt.state == "committed":
                return receipt
            try:
                current = self.store.read_text(note_path)
            except NoteNotFoundError:
                return receipt
            if current.revision != target_revision:
                return receipt
            return self._receipt_store().commit(operation_digest)

    def _note_path(self, path: str | PurePosixPath) -> str:
        normalized = self.store.normalize_path(path)
        pure = PurePosixPath(normalized)
        if pure.suffix == "":
            normalized += ".md"
        elif pure.suffix.casefold() != ".md":
            raise VaultPathError("note path must have the .md extension")
        return self.store.normalize_ordinary_note_path(normalized)

    def _receipt_store(self) -> NoteOperationReceiptStore:
        if self._operation_receipts is None:
            self._operation_receipts = NoteOperationReceiptStore(
                self._operation_receipt_root,
                vault_root=self.store.root,
            )
        return self._operation_receipts


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
    *,
    query_features: frozenset[str] = frozenset(),
    date_window: tuple[date, date] | None = None,
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
    note_features = _semantic_features(f"{note.path} {note.title} {note.body}")
    semantic_overlap = query_features & note_features
    if semantic_overlap:
        coverage = len(semantic_overlap) / max(1, len(query_features))
        # A single ordinary word remains lexical evidence.  Two independent
        # concepts or strong coverage make the approximate lane explicit.
        if len(semantic_overlap) >= 2 or coverage >= 0.6:
            channels.append("semantic")
            score += 18.0 * coverage + 2.5 * len(semantic_overlap)
    if date_window is not None:
        created = _created_property(note)
        if created is not None and date_window[0] <= created <= date_window[1]:
            channels.append("property_date_created")
            score += 40.0
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


def _semantic_features(text: str) -> frozenset[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[\w-]+", unicodedata.normalize("NFC", text).casefold()):
        if len(raw) < 2 or raw in _SEARCH_STOPWORDS:
            continue
        tokens.add(stem(raw, LEXICAL_MIN_STEM_INPUT))
    features = set(tokens)
    for index, group in enumerate(_SEMANTIC_GROUPS):
        if tokens & group:
            features.add(f"$concept:{index}")
    return frozenset(features)


def _approximate_date_window(query: str) -> tuple[date, date] | None:
    match = _EARLY_MONTH.search(query)
    if match is None:
        return None
    raw_month = match.group("month").casefold().replace("ё", "е")
    month = next((number for prefix, number in _MONTHS.items() if raw_month.startswith(prefix)), None)
    if month is None:
        return None
    year = int(match.group("year"))
    return date(year, month, 1), date(year, month, 10)


def _created_property(note: NoteDocument) -> date | None:
    value = note.properties.get("created")
    if not isinstance(value, PropertyValue):
        return None
    raw = value.value
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if value.type is not PropertyType.TEXT or not isinstance(raw, str):
        return None
    match = _TEXT_CREATED_DATE.fullmatch(raw.strip())
    if match is None:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


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


def _section_marker_payload(section: str, item: str) -> str:
    if not isinstance(section, str) or not isinstance(item, str):
        raise TypeError("section and item must be text")
    return f"section:{len(section)}:{section}item:{len(item)}:{item}"


def _marker(method: str, operation_digest: str, arguments_digest: str) -> str:
    return f'<!-- friday:{method} operation="{operation_digest}" arguments="{arguments_digest}" -->'


def _find_operation(content: str, operation_id: str, *, method: str) -> str | None:
    digest = _operation_digest(operation_id)
    for match in _OPERATION_MARKER.finditer(content):
        if match.group("method") == method and match.group("operation") == digest:
            return match.group("arguments")
    return None


def _find_operation_any(
    content: str,
    operation_id: str,
    *,
    methods: Sequence[str],
) -> tuple[str | None, str | None]:
    digest = _operation_digest(operation_id)
    for match in _OPERATION_MARKER.finditer(content):
        if match.group("method") in methods and match.group("operation") == digest:
            return match.group("method"), match.group("arguments")
    return None, None


def _remove_operation_marker(
    content: str,
    operation_id: str,
    *,
    method: str,
    arguments_digest: str,
    target_revision: str | None,
) -> str | None:
    marker = _marker(method, _operation_digest(operation_id), arguments_digest)
    if content.count(marker) != 1:
        return None
    start = content.index(marker)
    end = start + len(marker)
    if start and content[start - 1] not in "\r\n":
        return None
    if end < len(content) and content[end] not in "\r\n":
        return None
    after = end
    if content.startswith("\r\n", after):
        after += 2
    elif content.startswith(("\n", "\r"), after):
        after += 1
    candidates = [content[:start] + content[after:]]
    if start >= 2 and content[start - 2 : start] == "\r\n":
        candidates.append(content[: start - 2] + content[after:])
    elif start and content[start - 1] in "\r\n":
        candidates.append(content[: start - 1] + content[after:])
    if target_revision is None:
        return candidates[0]
    return next((item for item in candidates if _text_digest(item) == target_revision), None)


def _assert_receipt_target(
    receipt: NoteOperationReceipt,
    *,
    base_revision: str | None,
    target_revision: str,
) -> None:
    if receipt.base_revision != base_revision or receipt.target_revision != target_revision:
        raise IdempotencyConflictError("prepared operation target does not match its durable receipt")


def _receipt_result(
    receipt: NoteOperationReceipt,
    *,
    operation_id: str,
    applied: bool,
) -> NoteWriteResult:
    return NoteWriteResult(
        path=receipt.note_path,
        revision=receipt.target_revision,
        previous_revision=receipt.base_revision,
        created=receipt.created,
        applied=applied,
        operation_id=operation_id,
        delivery=VaultDeliveryState.local_only(),
    )


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


def _prepend_visible_text(existing: str, addition: str, *, newline: str) -> str:
    _text_digest(addition)
    if not existing:
        combined = addition
    elif addition.endswith(("\n", "\r")) or existing.startswith(("\n", "\r")):
        combined = addition + existing
    else:
        combined = f"{addition}{newline}{existing}"
    if combined and not combined.endswith(("\n", "\r")):
        combined += newline
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
