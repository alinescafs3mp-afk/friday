"""Bounded Obsidian link parsing, resolution, backlinks, and move rewrites."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import quote, unquote

from .contracts import NoteAlreadyExistsError, NoteNotFoundError, RevisionConflictError, VaultLimitError
from .vault_store import VaultStore

_MAX_PATH_CHARS = 2_048
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DYNAMIC_MARKERS = ("{{", "}}", "<%", "%>", "$(", "${")
_RESERVED_ROOTS = frozenset({".obsidian", ".stfolder", ".stignore", ".stversions", ".trash"})


@dataclass(frozen=True, slots=True)
class LinkLimits:
    """Hard ceilings for parsing a synchronized, therefore untrusted, vault."""

    max_text_chars: int = 4 * 1024 * 1024
    max_total_text_bytes: int = 32 * 1024 * 1024
    max_notes: int = 5_000
    max_links: int = 20_000
    max_link_chars: int = 4_096
    max_target_chars: int = 2_048

    def __post_init__(self) -> None:
        for name in (
            "max_text_chars",
            "max_total_text_bytes",
            "max_notes",
            "max_links",
            "max_link_chars",
            "max_target_chars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


class LinkSyntax(StrEnum):
    WIKILINK = "wikilink"
    MARKDOWN = "markdown"


class LinkResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    DYNAMIC = "dynamic"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class ParsedLink:
    """One syntactic link with stable character offsets into its source text."""

    syntax: LinkSyntax
    raw: str
    target: str
    target_path: str
    fragment: str
    alias: str | None
    embed: bool
    start: int
    end: int
    target_start: int
    target_end: int
    angle_destination: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedLink:
    source_path: str
    link: ParsedLink
    status: LinkResolutionStatus
    resolved_path: str | None
    candidates: tuple[str, ...] = ()

    @property
    def target(self) -> str:
        return self.link.target


@dataclass(frozen=True, slots=True)
class GraphNote:
    path: str
    content: str
    revision: str


@dataclass(frozen=True, slots=True)
class LinkGraph:
    """An immutable snapshot; link identity never substitutes for note identity."""

    notes: tuple[GraphNote, ...]
    links: tuple[ResolvedLink, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(note.path for note in self.notes)

    def outgoing(self, source_path: str, *, resolved_only: bool = False) -> tuple[ResolvedLink, ...]:
        source = _canonical_note_path(source_path)
        return tuple(
            item
            for item in self.links
            if item.source_path == source
            and (not resolved_only or item.status is LinkResolutionStatus.RESOLVED)
        )

    outgoing_links = outgoing

    def backlinks(self, target_path: str) -> tuple[ResolvedLink, ...]:
        target = _canonical_note_path(target_path)
        return tuple(
            item
            for item in self.links
            if item.status is LinkResolutionStatus.RESOLVED and item.resolved_path == target
        )

    get_backlinks = backlinks

    @property
    def unresolved(self) -> tuple[ResolvedLink, ...]:
        return tuple(item for item in self.links if item.status is LinkResolutionStatus.UNRESOLVED)

    @property
    def ambiguous(self) -> tuple[ResolvedLink, ...]:
        return tuple(item for item in self.links if item.status is LinkResolutionStatus.AMBIGUOUS)

    @property
    def dynamic(self) -> tuple[ResolvedLink, ...]:
        return tuple(item for item in self.links if item.status is LinkResolutionStatus.DYNAMIC)

    def plan_move(self, source_path: str, destination_path: str) -> LinkMovePlan:
        return plan_move(self, source_path, destination_path)


@dataclass(frozen=True, slots=True)
class PlannedLinkRewrite:
    source_path: str
    output_path: str
    previous_revision: str
    content: str
    revision: str
    rewritten_links: int

    @property
    def path(self) -> str:
        return self.output_path


@dataclass(frozen=True, slots=True)
class LinkMovePlan:
    source_path: str
    destination_path: str
    moved_revision: str
    rewrites: tuple[PlannedLinkRewrite, ...]
    ambiguous: tuple[ResolvedLink, ...]
    unresolved: tuple[ResolvedLink, ...]
    dynamic: tuple[ResolvedLink, ...]

    @property
    def destination_revision(self) -> str:
        for rewrite in self.rewrites:
            if rewrite.output_path == self.destination_path:
                return rewrite.revision
        return self.moved_revision

    @property
    def changed_paths(self) -> tuple[str, ...]:
        paths = [self.source_path, self.destination_path]
        paths.extend(rewrite.output_path for rewrite in self.rewrites)
        return tuple(dict.fromkeys(paths))

    @property
    def changed_revisions(self) -> tuple[tuple[str, str | None], ...]:
        revisions: dict[str, str | None] = {
            self.source_path: None,
            self.destination_path: self.destination_revision,
        }
        for rewrite in self.rewrites:
            revisions[rewrite.output_path] = rewrite.revision
        return tuple(revisions.items())

    @property
    def skipped(self) -> tuple[ResolvedLink, ...]:
        return (*self.ambiguous, *self.unresolved, *self.dynamic)


@dataclass(frozen=True, slots=True)
class ChangedPathRevision:
    path: str
    previous_revision: str
    revision: str
    applied: bool


@dataclass(frozen=True, slots=True)
class LinkRewriteResult:
    changes: tuple[ChangedPathRevision, ...]
    ambiguous: tuple[ResolvedLink, ...]
    unresolved: tuple[ResolvedLink, ...]
    dynamic: tuple[ResolvedLink, ...]

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)

    @property
    def changed_revisions(self) -> tuple[tuple[str, str], ...]:
        return tuple((change.path, change.revision) for change in self.changes)


@dataclass(frozen=True, slots=True)
class MoveExecutionResult:
    plan: LinkMovePlan
    moved_applied: bool
    link_rewrites: LinkRewriteResult

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return self.plan.changed_paths

    @property
    def changed_revisions(self) -> tuple[tuple[str, str | None], ...]:
        return self.plan.changed_revisions

    @property
    def ambiguous(self) -> tuple[ResolvedLink, ...]:
        return self.plan.ambiguous

    @property
    def unresolved(self) -> tuple[ResolvedLink, ...]:
        return self.plan.unresolved


class LinkResolver:
    """Resolve exact vault paths and exact note titles without fuzzy guessing."""

    def __init__(
        self,
        note_paths: Iterable[str],
        *,
        titles: Mapping[str, str] | None = None,
        limits: LinkLimits | None = None,
    ) -> None:
        self.limits = limits or LinkLimits()
        paths: list[str] = []
        seen_paths: set[str] = set()
        for index, path in enumerate(note_paths):
            if index >= self.limits.max_notes:
                raise VaultLimitError(
                    f"link resolver exceeds the maximum note count of {self.limits.max_notes}"
                )
            normalized = _canonical_note_path(path)
            if normalized in seen_paths:
                raise ValueError(f"duplicate note path {normalized!r}")
            seen_paths.add(normalized)
            paths.append(normalized)
        self.paths = tuple(sorted(paths, key=lambda item: (item.casefold(), item)))
        self._path_index: dict[str, tuple[str, ...]] = _candidate_index(
            (key, path) for path in self.paths for key in (path, path[:-3])
        )
        title_pairs: list[tuple[str, str]] = [(PurePosixPath(path).stem, path) for path in self.paths]
        if titles is not None:
            for raw_path, title in titles.items():
                path = _canonical_note_path(raw_path)
                if path not in self.paths:
                    raise ValueError(f"title supplied for unknown note {path!r}")
                if not isinstance(title, str) or not title or len(title) > self.limits.max_target_chars:
                    raise ValueError("note title must be non-empty and bounded")
                title_pairs.append((title, path))
        self._title_index = _candidate_index(title_pairs)

    def resolve(self, source_path: str, link: ParsedLink) -> ResolvedLink:
        source = _canonical_note_path(source_path)
        raw_target = _unescape_target(link.target_path)
        if any(marker in raw_target for marker in _DYNAMIC_MARKERS):
            return ResolvedLink(source, link, LinkResolutionStatus.DYNAMIC, None)
        if link.syntax is LinkSyntax.MARKDOWN:
            if raw_target.startswith("//") or _SCHEME.match(raw_target):
                return ResolvedLink(source, link, LinkResolutionStatus.EXTERNAL, None)
            try:
                raw_target = unquote(raw_target, encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                return ResolvedLink(source, link, LinkResolutionStatus.UNRESOLVED, None)
        elif _SCHEME.match(raw_target):
            return ResolvedLink(source, link, LinkResolutionStatus.EXTERNAL, None)

        if not raw_target:
            if link.fragment and source in self.paths:
                return ResolvedLink(source, link, LinkResolutionStatus.RESOLVED, source, (source,))
            return ResolvedLink(source, link, LinkResolutionStatus.UNRESOLVED, None)
        if len(raw_target) > self.limits.max_target_chars or "\x00" in raw_target:
            return ResolvedLink(source, link, LinkResolutionStatus.UNRESOLVED, None)

        if link.syntax is LinkSyntax.MARKDOWN:
            candidate = _markdown_candidate(source, raw_target)
            if candidate is None:
                return ResolvedLink(source, link, LinkResolutionStatus.UNRESOLVED, None)
            return self._resolution_from_candidates(source, link, self._path_matches(candidate))

        candidate = _wikilink_candidate(raw_target)
        if candidate is None:
            return ResolvedLink(source, link, LinkResolutionStatus.UNRESOLVED, None)
        path_matches = self._path_matches(candidate)
        if path_matches:
            return self._resolution_from_candidates(source, link, path_matches)
        if "/" in raw_target:
            return ResolvedLink(source, link, LinkResolutionStatus.UNRESOLVED, None)
        title_matches = self._title_index.get(raw_target.casefold(), ())
        return self._resolution_from_candidates(source, link, title_matches)

    def _path_matches(self, candidate: str) -> tuple[str, ...]:
        return self._path_index.get(candidate.casefold(), ())

    @staticmethod
    def _resolution_from_candidates(
        source: str,
        link: ParsedLink,
        candidates: tuple[str, ...],
    ) -> ResolvedLink:
        if len(candidates) == 1:
            return ResolvedLink(
                source,
                link,
                LinkResolutionStatus.RESOLVED,
                candidates[0],
                candidates,
            )
        if candidates:
            return ResolvedLink(source, link, LinkResolutionStatus.AMBIGUOUS, None, candidates)
        return ResolvedLink(source, link, LinkResolutionStatus.UNRESOLVED, None)


def parse_links(markdown: str, *, limits: LinkLimits | None = None) -> tuple[ParsedLink, ...]:
    """Parse inline Markdown links and Obsidian wikilinks in linear bounded space."""

    selected = limits or LinkLimits()
    if not isinstance(markdown, str) or "\x00" in markdown:
        raise ValueError("Markdown link source must be NUL-free text")
    if len(markdown) > selected.max_text_chars:
        raise VaultLimitError(f"Markdown link source exceeds {selected.max_text_chars} characters")
    links: list[ParsedLink] = []
    length = len(markdown)
    index = 0
    fence: tuple[str, int] | None = None
    while index < length:
        at_line_start = index == 0 or markdown[index - 1] == "\n"
        if at_line_start:
            marker = _fence_at(markdown, index)
            if fence is not None:
                if marker is not None and marker[0] == fence[0] and marker[1] >= fence[1]:
                    fence = None
                index = _next_line(markdown, index)
                continue
            if marker is not None:
                fence = marker
                index = _next_line(markdown, index)
                continue
        if markdown.startswith("<!--", index):
            closing = markdown.find("-->", index + 4)
            index = length if closing < 0 else closing + 3
            continue
        if markdown[index] == "`" and not _is_escaped(markdown, index):
            run = _delimiter_run(markdown, index, "`")
            delimiter = "`" * run
            closing = markdown.find(delimiter, index + run)
            if closing < 0 or closing + run - index > selected.max_link_chars:
                index = _next_line(markdown, index)
            else:
                index = closing + run
            continue

        parsed: ParsedLink | None = None
        if markdown.startswith("![[", index) or markdown.startswith("[[", index):
            parsed = _parse_wikilink_at(markdown, index, selected)
            if parsed is None and not _is_escaped(markdown, index) and markdown.find("]]", index + 2) < 0:
                break
        elif markdown.startswith("![", index) or markdown[index] == "[":
            parsed = _parse_markdown_link_at(markdown, index, selected)
        if parsed is None:
            index += 1
            continue
        links.append(parsed)
        if len(links) > selected.max_links:
            raise VaultLimitError(f"Markdown exceeds the maximum link count of {selected.max_links}")
        index = parsed.end
    return tuple(links)


parse_markdown_links = parse_links


def build_link_graph(
    notes: Mapping[str, str],
    *,
    titles: Mapping[str, str] | None = None,
    limits: LinkLimits | None = None,
) -> LinkGraph:
    """Snapshot bounded note text and resolve all syntactic links exactly once."""

    if not isinstance(notes, Mapping):
        raise TypeError("notes must be a path-to-text mapping")
    selected = limits or LinkLimits()
    snapshots: list[GraphNote] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, (raw_path, content) in enumerate(notes.items()):
        if index >= selected.max_notes:
            raise VaultLimitError(f"link graph exceeds the maximum note count of {selected.max_notes}")
        path = _canonical_note_path(raw_path)
        if path in seen:
            raise ValueError(f"duplicate note path {path!r}")
        seen.add(path)
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("note content must be NUL-free text")
        if len(content) > selected.max_text_chars:
            raise VaultLimitError(f"note {path!r} exceeds {selected.max_text_chars} characters")
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("note content must be valid UTF-8") from exc
        total_bytes += len(encoded)
        if total_bytes > selected.max_total_text_bytes:
            raise VaultLimitError(
                f"link graph exceeds the aggregate Markdown byte budget of {selected.max_total_text_bytes}"
            )
        snapshots.append(GraphNote(path, content, hashlib.sha256(encoded).hexdigest()))
    snapshots.sort(key=lambda note: (note.path.casefold(), note.path))
    resolver = LinkResolver((note.path for note in snapshots), titles=titles, limits=selected)
    resolved: list[ResolvedLink] = []
    for note in snapshots:
        parsed = parse_links(note.content, limits=selected)
        if len(resolved) + len(parsed) > selected.max_links:
            raise VaultLimitError(f"link graph exceeds the maximum link count of {selected.max_links}")
        resolved.extend(resolver.resolve(note.path, link) for link in parsed)
    return LinkGraph(tuple(snapshots), tuple(resolved))


def build_vault_link_graph(
    store: VaultStore,
    *,
    titles: Mapping[str, str] | None = None,
    limits: LinkLimits | None = None,
) -> LinkGraph:
    selected = limits or LinkLimits(
        max_text_chars=store.limits.max_note_bytes,
        max_total_text_bytes=store.limits.max_total_markdown_bytes,
        max_notes=store.limits.max_markdown_paths,
    )
    notes = {stored.path: stored.text() for stored in store.iter_markdown_files()}
    return build_link_graph(notes, titles=titles, limits=selected)


def plan_move(graph: LinkGraph, source_path: str, destination_path: str) -> LinkMovePlan:
    """Plan exact inbound-link rewrites; never guess an ambiguous target."""

    source = _canonical_note_path(source_path)
    destination = _canonical_note_path(destination_path)
    if source == destination:
        raise ValueError("source and destination paths must differ")
    notes = {note.path: note for note in graph.notes}
    moved = notes.get(source)
    if moved is None:
        raise NoteNotFoundError(source)
    if any(path.casefold() == destination.casefold() for path in notes):
        raise NoteAlreadyExistsError(destination)

    by_source: dict[str, list[ResolvedLink]] = {}
    for resolution in graph.links:
        if (
            resolution.status is LinkResolutionStatus.RESOLVED
            and resolution.link.target_path
            and (
                resolution.resolved_path == source
                or (resolution.source_path == source and resolution.link.syntax is LinkSyntax.MARKDOWN)
            )
        ):
            by_source.setdefault(resolution.source_path, []).append(resolution)

    rewrites: list[PlannedLinkRewrite] = []
    for note_path, resolutions in sorted(by_source.items()):
        note = notes[note_path]
        output_path = destination if note_path == source else note_path
        replacements: list[tuple[int, int, str]] = []
        for resolution in resolutions:
            assert resolution.resolved_path is not None
            rewritten_target = destination if resolution.resolved_path == source else resolution.resolved_path
            replacement = _move_target(
                resolution.link,
                rewritten_target,
                output_source_path=output_path,
            )
            if replacement != resolution.link.target:
                replacements.append((resolution.link.target_start, resolution.link.target_end, replacement))
        if not replacements:
            continue
        rendered = note.content
        for start, end, replacement in reversed(replacements):
            rendered = f"{rendered[:start]}{replacement}{rendered[end:]}"
        encoded = rendered.encode("utf-8", errors="strict")
        rewrites.append(
            PlannedLinkRewrite(
                source_path=note_path,
                output_path=output_path,
                previous_revision=note.revision,
                content=rendered,
                revision=hashlib.sha256(encoded).hexdigest(),
                rewritten_links=len(replacements),
            )
        )

    return LinkMovePlan(
        source_path=source,
        destination_path=destination,
        moved_revision=moved.revision,
        rewrites=tuple(rewrites),
        ambiguous=graph.ambiguous,
        unresolved=graph.unresolved,
        dynamic=graph.dynamic,
    )


plan_move_rewrites = plan_move


def apply_move_rewrites(store: VaultStore, plan: LinkMovePlan) -> LinkRewriteResult:
    """Apply or resume the CAS-protected link portion of a move plan."""

    changes: list[ChangedPathRevision] = []
    for rewrite in plan.rewrites:
        current = store.read_text(rewrite.output_path)
        if current.revision == rewrite.revision:
            changes.append(
                ChangedPathRevision(
                    rewrite.output_path,
                    rewrite.previous_revision,
                    rewrite.revision,
                    False,
                )
            )
            continue
        if current.revision != rewrite.previous_revision:
            raise RevisionConflictError(rewrite.previous_revision, current.revision)
        written = store.write_text(
            rewrite.output_path,
            rewrite.content,
            expected_revision=rewrite.previous_revision,
        )
        changes.append(
            ChangedPathRevision(
                rewrite.output_path,
                rewrite.previous_revision,
                written.revision,
                True,
            )
        )
    return LinkRewriteResult(
        changes=tuple(changes),
        ambiguous=plan.ambiguous,
        unresolved=plan.unresolved,
        dynamic=plan.dynamic,
    )


apply_move_plan_rewrites = apply_move_rewrites


def move_rewrite_postcondition(store: VaultStore, plan: LinkMovePlan) -> bool:
    """Reconcile all link writes without replaying any write."""

    for rewrite in plan.rewrites:
        try:
            current = store.read(rewrite.output_path)
        except NoteNotFoundError:
            return False
        if current.revision != rewrite.revision:
            return False
    return True


def move_plan_postcondition(store: VaultStore, plan: LinkMovePlan) -> bool:
    return store.move_postcondition(
        plan.source_path,
        plan.destination_path,
        expected_revision=plan.moved_revision,
    ) and move_rewrite_postcondition(store, plan)


def execute_move_plan(store: VaultStore, plan: LinkMovePlan) -> MoveExecutionResult:
    """Execute or resume a move and its independently reconcilable link writes."""

    moved_applied = False
    if not store.move_postcondition(
        plan.source_path,
        plan.destination_path,
        expected_revision=plan.moved_revision,
    ):
        store.move(
            plan.source_path,
            plan.destination_path,
            expected_revision=plan.moved_revision,
        )
        moved_applied = True
    rewrites = apply_move_rewrites(store, plan)
    return MoveExecutionResult(plan=plan, moved_applied=moved_applied, link_rewrites=rewrites)


def _parse_wikilink_at(text: str, start: int, limits: LinkLimits) -> ParsedLink | None:
    if _is_escaped(text, start):
        return None
    embed = text.startswith("![[", start)
    opening = start + (3 if embed else 2)
    closing = text.find("]]", opening)
    if closing < 0:
        return None
    if closing + 2 - start > limits.max_link_chars:
        raise VaultLimitError(f"wikilink exceeds {limits.max_link_chars} characters")
    inner = text[opening:closing]
    alias_offset = _unescaped_index(inner, "|")
    target_region = inner if alias_offset < 0 else inner[:alias_offset]
    leading = len(target_region) - len(target_region.lstrip())
    trailing_end = max(leading, len(target_region.rstrip()))
    target_start = opening + leading
    target_end = opening + trailing_end
    target = text[target_start:target_end]
    if len(target) > limits.max_target_chars:
        raise VaultLimitError(f"wikilink target exceeds {limits.max_target_chars} characters")
    target_path, fragment = _split_fragment(target, allow_block=True)
    alias = None if alias_offset < 0 else inner[alias_offset + 1 :].strip()
    end = closing + 2
    return ParsedLink(
        syntax=LinkSyntax.WIKILINK,
        raw=text[start:end],
        target=target,
        target_path=target_path,
        fragment=fragment,
        alias=alias,
        embed=embed,
        start=start,
        end=end,
        target_start=target_start,
        target_end=target_end,
    )


def _parse_markdown_link_at(text: str, start: int, limits: LinkLimits) -> ParsedLink | None:
    if _is_escaped(text, start):
        return None
    embed = text.startswith("![", start)
    label_open = start + 1 if embed else start
    if text.startswith("[[", label_open):
        return None
    maximum = min(len(text), start + limits.max_link_chars)
    label_close = _closing_bracket(text, label_open, maximum)
    if label_close < 0 and maximum < len(text):
        raise VaultLimitError(f"Markdown link exceeds {limits.max_link_chars} characters")
    if label_close < 0 or label_close + 1 >= len(text) or text[label_close + 1] != "(":
        return None
    cursor = label_close + 2
    while cursor < maximum and text[cursor] in " \t":
        cursor += 1
    angle = cursor < maximum and text[cursor] == "<"
    if angle:
        target_start = cursor + 1
        target_end = _unescaped_closing(text, target_start, maximum, ">")
        if target_end < 0:
            return None
        closing = _markdown_outer_close(text, target_end + 1, maximum)
    else:
        target_start = cursor
        target_end, closing = _markdown_destination_end(text, cursor, maximum)
    if target_end < 0 or closing < 0:
        return None
    target = text[target_start:target_end]
    if len(target) > limits.max_target_chars:
        raise VaultLimitError(f"Markdown link target exceeds {limits.max_target_chars} characters")
    target_path, fragment = _split_fragment(target, allow_block=False)
    end = closing + 1
    return ParsedLink(
        syntax=LinkSyntax.MARKDOWN,
        raw=text[start:end],
        target=target,
        target_path=target_path,
        fragment=fragment,
        alias=text[label_open + 1 : label_close],
        embed=embed,
        start=start,
        end=end,
        target_start=target_start,
        target_end=target_end,
        angle_destination=angle,
    )


def _closing_bracket(text: str, opening: int, maximum: int) -> int:
    depth = 1
    cursor = opening + 1
    while cursor < maximum:
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == "[":
            depth += 1
        elif text[cursor] == "]":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return -1


def _markdown_destination_end(text: str, cursor: int, maximum: int) -> tuple[int, int]:
    target_end = -1
    depth = 0
    title_quote: str | None = None
    title_parentheses = 0
    while cursor < maximum:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if target_end >= 0 and character in {'"', "'"}:
            if title_quote is None:
                title_quote = character
            elif title_quote == character:
                title_quote = None
            cursor += 1
            continue
        if character == "(":
            if target_end < 0:
                depth += 1
            elif title_quote is None:
                title_parentheses += 1
        elif character == ")" and title_quote is None:
            if title_parentheses:
                title_parentheses -= 1
                cursor += 1
                continue
            if depth == 0:
                return (cursor if target_end < 0 else target_end), cursor
            depth -= 1
        elif character in " \t\n" and depth == 0 and target_end < 0:
            target_end = cursor
        cursor += 1
    return -1, -1


def _markdown_outer_close(text: str, cursor: int, maximum: int) -> int:
    quote_character: str | None = None
    title_parentheses = 0
    while cursor < maximum:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character in {'"', "'"} and quote_character is None:
            quote_character = character
        elif character == quote_character:
            quote_character = None
        elif character == "(" and quote_character is None:
            title_parentheses += 1
        elif character == ")" and quote_character is None:
            if title_parentheses:
                title_parentheses -= 1
                cursor += 1
                continue
            return cursor
        cursor += 1
    return -1


def _unescaped_closing(text: str, cursor: int, maximum: int, delimiter: str) -> int:
    while cursor < maximum:
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == delimiter:
            return cursor
        cursor += 1
    return -1


def _split_fragment(target: str, *, allow_block: bool) -> tuple[str, str]:
    offsets = [
        offset for marker in (("#", "^") if allow_block else ("#",)) if (offset := target.find(marker)) >= 0
    ]
    if not offsets:
        return target, ""
    offset = min(offsets)
    return target[:offset], target[offset:]


def _fence_at(text: str, line_start: int) -> tuple[str, int] | None:
    cursor = line_start
    spaces = 0
    while cursor < len(text) and spaces < 4 and text[cursor] == " ":
        cursor += 1
        spaces += 1
    if spaces > 3 or cursor >= len(text) or text[cursor] not in {"`", "~"}:
        return None
    marker = text[cursor]
    run = _delimiter_run(text, cursor, marker)
    return (marker, run) if run >= 3 else None


def _delimiter_run(text: str, start: int, delimiter: str) -> int:
    cursor = start
    while cursor < len(text) and text[cursor] == delimiter:
        cursor += 1
    return cursor - start


def _next_line(text: str, start: int) -> int:
    newline = text.find("\n", start)
    return len(text) if newline < 0 else newline + 1


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _unescaped_index(text: str, delimiter: str) -> int:
    for index, character in enumerate(text):
        if character == delimiter and not _is_escaped(text, index):
            return index
    return -1


def _unescape_target(target: str) -> str:
    return re.sub(r"\\([\\()<>|#^\[\]])", r"\1", target.strip())


def _canonical_note_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > _MAX_PATH_CHARS:
        raise ValueError("note path must be non-empty and bounded")
    if "\x00" in path or "\\" in path or path.startswith("/") or _SCHEME.match(path):
        raise ValueError("note path must be a relative POSIX path")
    try:
        path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("note path must be valid UTF-8") from exc
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("note path contains an unsafe segment")
    folded = tuple(part.casefold() for part in parts)
    if folded[0] in _RESERVED_ROOTS or ".obsidian" in folded:
        raise ValueError("note path enters a reserved vault directory")
    if ".sync-conflict-" in folded[-1]:
        raise ValueError("note path names a synchronization conflict copy")
    pure = PurePosixPath(*parts)
    if pure.suffix == "":
        pure = PurePosixPath(f"{pure.as_posix()}.md")
    elif pure.suffix.casefold() != ".md":
        raise ValueError("note path must have the .md extension")
    return pure.as_posix()


def _wikilink_candidate(target: str) -> str | None:
    if target.startswith("/") or "\\" in target:
        return None
    try:
        return _canonical_note_path(target)
    except ValueError:
        return None


def _markdown_candidate(source_path: str, target: str) -> str | None:
    if target.startswith("/") or "\\" in target:
        return None
    joined = posixpath.normpath(posixpath.join(PurePosixPath(source_path).parent.as_posix(), target))
    if joined in {".", ".."} or joined.startswith("../"):
        return None
    try:
        return _canonical_note_path(joined)
    except ValueError:
        return None


def _candidate_index(pairs: Iterable[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    pending: dict[str, set[str]] = {}
    for key, path in pairs:
        pending.setdefault(key.casefold(), set()).add(path)
    return {
        key: tuple(sorted(paths, key=lambda item: (item.casefold(), item))) for key, paths in pending.items()
    }


def _move_target(link: ParsedLink, destination: str, *, output_source_path: str) -> str:
    decoded_original = _unescape_target(link.target_path)
    include_extension = decoded_original.casefold().endswith(".md")
    if link.syntax is LinkSyntax.WIKILINK:
        rendered = destination if include_extension else destination[:-3]
    else:
        base = PurePosixPath(output_source_path).parent.as_posix()
        rendered = posixpath.relpath(destination, start=base)
        if not include_extension and rendered.casefold().endswith(".md"):
            rendered = rendered[:-3]
        if decoded_original.startswith("./") and not rendered.startswith(("./", "../")):
            rendered = f"./{rendered}"
        rendered = quote(rendered, safe="/@:+-._~!$&'*,;=")
    return f"{rendered}{link.fragment}"


__all__ = [
    "ChangedPathRevision",
    "GraphNote",
    "LinkGraph",
    "LinkLimits",
    "LinkMovePlan",
    "LinkResolutionStatus",
    "LinkResolver",
    "LinkRewriteResult",
    "LinkSyntax",
    "MoveExecutionResult",
    "ParsedLink",
    "PlannedLinkRewrite",
    "ResolvedLink",
    "apply_move_plan_rewrites",
    "apply_move_rewrites",
    "build_link_graph",
    "build_vault_link_graph",
    "execute_move_plan",
    "move_plan_postcondition",
    "move_rewrite_postcondition",
    "parse_links",
    "parse_markdown_links",
    "plan_move",
    "plan_move_rewrites",
]
