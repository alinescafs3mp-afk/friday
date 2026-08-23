"""Principal-scoped, SELECT-only Obsidian archive lanes.

The caller owns the SQLite transaction.  Binding membership is materialized
before counts, matching, ranking and limits.  Index text is only a private
candidate projection: factual text must cross ``verify_*`` against the exact
vault bytes immediately before model admission and again before publication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from threading import Lock
from typing import Any, Final, NoReturn, Protocol, SupportsIndex, TypeVar, cast

from friday.retrieval._keyboard import looks_mistyped, switched
from friday.retrieval._repair import _edit_distance
from friday.retrieval.archive_search_contract import (
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    LifecycleState,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
)

MAX_ARCHIVE_OBSIDIAN_RESULTS: Final = 20
MAX_ARCHIVE_OBSIDIAN_IDENTITY_SCAN: Final = 5_000
MAX_ARCHIVE_OBSIDIAN_ALIASES: Final = 64
MAX_ARCHIVE_OBSIDIAN_BODY_BYTES: Final = 4 * 1024 * 1024
MAX_ARCHIVE_OBSIDIAN_LIVE_CARRIERS: Final = 1_024

_PROCESS_KEY = secrets.token_bytes(32)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BODY_CARRIER_KIND = hashlib.sha256(b"friday/archive-obsidian-body-carrier/v1").digest()
_NAVIGATION_CARRIER_KIND = hashlib.sha256(b"friday/archive-obsidian-navigation-carrier/v1").digest()
_CARRIER_LOCK = Lock()
_CARRIER_RECORDS: OrderedDict[bytes, tuple[bytes, bytes]] = OrderedDict()
_ResultT = TypeVar("_ResultT", covariant=True)
_SUPPORTED_LANES = frozenset(
    {
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
    }
)


class ArchiveObsidianStorageError(RuntimeError):
    """Body-free failure at the private Obsidian archive storage seam."""


class ArchiveObsidianIndexState(StrEnum):
    MISSING = "missing"
    READY = "ready"
    STALE = "stale"


class ArchiveObsidianCoverage(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    COMPLETE = "complete"


class ArchiveObsidianMatchKind(StrEnum):
    EXACT = "exact"
    PREFIX = "prefix"
    SUBSTRING = "substring"
    TYPO = "typo"
    KEYBOARD_LAYOUT = "keyboard_layout"
    LEXICAL_PHRASE = "lexical_phrase"
    LEXICAL_TERMS = "lexical_terms"


class ArchiveObsidianReadPhase(StrEnum):
    BEFORE_MODEL = "before_model"
    BEFORE_PUBLICATION = "before_publication"


class ArchiveObsidianUnavailableReason(StrEnum):
    PRINCIPAL_DENIED = "principal_denied"
    VAULT_UNAVAILABLE = "vault_unavailable"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    INDEX_UNAVAILABLE = "index_unavailable"
    TEMPORAL_UNSUPPORTED = "temporal_unsupported"
    LIFECYCLE_UNSUPPORTED = "lifecycle_unsupported"
    LANE_UNSUPPORTED = "lane_unsupported"


class ArchiveObsidianExactFileReader(Protocol):
    def __call__(
        self,
        vault_id: str,
        path: str,
        expected_sha256: str,
        /,
    ) -> bytes: ...


class ArchiveObsidianNavigationConsumer(Protocol[_ResultT]):
    def __call__(
        self,
        *,
        binding_id: str,
        vault_id: str,
        path: str,
        title: str,
        aliases: tuple[str, ...],
        current_revision: str,
        lifecycle: LifecycleState,
        index_state: ArchiveObsidianIndexState,
        index_revision_current: bool,
        index_path_current: bool,
        metadata_coverage: ArchiveObsidianCoverage,
        body_coverage: ArchiveObsidianCoverage,
        lane: SearchLane,
        match_kind: ArchiveObsidianMatchKind,
        rank: int,
    ) -> _ResultT: ...


class ArchiveObsidianBodyConsumer(Protocol[_ResultT]):
    def __call__(self, text: str, /) -> _ResultT: ...


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("archive Obsidian value is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive Obsidian value is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive Obsidian value is process-private")


def _fail(message: str) -> ArchiveObsidianStorageError:
    return ArchiveObsidianStorageError(message)


def _principal(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail("archive Obsidian principal is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail("archive Obsidian principal is invalid") from None
    if len(encoded) > 200 or any(unicodedata.category(char).startswith("C") for char in value):
        raise _fail("archive Obsidian principal is invalid")
    return value


def _tenant(value: object) -> str:
    try:
        return _principal(value)
    except ArchiveObsidianStorageError:
        raise _fail("archive Obsidian tenant is invalid") from None


def _bounded_text(value: object, *, label: str, maximum: int, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value) or len(value) > maximum:
        raise _fail(f"archive Obsidian {label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail(f"archive Obsidian {label} is invalid") from None
    if len(encoded) > maximum * 4 or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise _fail(f"archive Obsidian {label} is invalid")
    return value


def _binding_id(value: object) -> str:
    return _bounded_text(value, label="binding identity", maximum=200)


def _vault_id(value: object) -> str:
    return _bounded_text(value, label="vault identity", maximum=200)


def _path(value: object) -> str:
    path = _bounded_text(value, label="path", maximum=1_024)
    if path.startswith("/") or "\\" in path:
        raise _fail("archive Obsidian path is invalid")
    parts = PurePosixPath(path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _fail("archive Obsidian path is invalid")
    canonical = PurePosixPath(*parts).as_posix()
    if canonical != path:
        raise _fail("archive Obsidian path is invalid")
    return canonical


def _revision(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _fail("archive Obsidian revision is invalid")
    return value


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold().replace("ё", "е")


def _limit(value: object, request: ArchiveSearchRequest) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ARCHIVE_OBSIDIAN_RESULTS
        or value > request.limit
    ):
        raise _fail("archive Obsidian page limit is invalid")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise _fail("archive Obsidian private material is invalid") from None


def _mac(domain: bytes, value: bytes) -> bytes:
    return hmac.digest(_PROCESS_KEY, domain + b"\0" + value, "sha256")


def _request_handle(request: object) -> bytes:
    if type(request) is not ArchiveSearchRequest:
        raise _fail("archive Obsidian request is invalid")
    try:
        material = cast(ArchiveSearchRequest, request).to_identity_json().encode("ascii", errors="strict")
    except Exception:
        raise _fail("archive Obsidian request identity is invalid") from None
    return _mac(b"friday/archive-obsidian-request/v2", material)


def _principal_handle(principal_id: object) -> bytes:
    principal = _principal(principal_id)
    return _mac(
        b"friday/archive-obsidian-principal/v2",
        principal.encode("utf-8", errors="strict"),
    )


def _tenant_handle(tenant_id: object) -> bytes:
    tenant = _tenant(tenant_id)
    return _mac(
        b"friday/archive-obsidian-tenant/v2",
        tenant.encode("utf-8", errors="strict"),
    )


def _snapshot_handle(snapshot_discriminator: object) -> bytes:
    snapshot = _bounded_text(
        snapshot_discriminator,
        label="snapshot discriminator",
        maximum=256,
    )
    return _mac(
        b"friday/archive-obsidian-snapshot/v2",
        snapshot.encode("utf-8", errors="strict"),
    )


def _scope_handles(
    *,
    tenant_id: object,
    principal_id: object,
    request: object,
    snapshot_discriminator: object,
) -> tuple[bytes, bytes, bytes, bytes]:
    return (
        _tenant_handle(tenant_id),
        _principal_handle(principal_id),
        _request_handle(request),
        _snapshot_handle(snapshot_discriminator),
    )


def _scope_matches(
    *,
    tenant_handle: bytes,
    principal_handle: bytes,
    request_handle: bytes,
    snapshot_handle: bytes,
    tenant_id: object,
    principal_id: object,
    request: object,
    snapshot_discriminator: object,
) -> bool:
    try:
        expected = _scope_handles(
            tenant_id=tenant_id,
            principal_id=principal_id,
            request=request,
            snapshot_discriminator=snapshot_discriminator,
        )
        return all(
            hmac.compare_digest(actual, wanted)
            for actual, wanted in zip(
                (tenant_handle, principal_handle, request_handle, snapshot_handle),
                expected,
                strict=True,
            )
        )
    except Exception:
        return False


def _execution_binding_attests(
    execution_binding: object,
    *,
    tenant_id: object,
    principal_id: object,
    request: object,
    snapshot_discriminator: object,
    lane: object,
) -> bool:
    try:
        return bool(
            type(execution_binding) is SearchExecutionBinding
            and type(request) is ArchiveSearchRequest
            and type(lane) is SearchLane
            and execution_binding.is_live_private_request_binding
            and execution_binding.authority_scope is AuthorityScope.TENANT_PRINCIPAL
            and (SearchCorpus.OBSIDIAN, lane) in execution_binding.requested_targets
            and execution_binding.attests_private_request(request.to_identity_json())
            and execution_binding.attests_authority(
                authority_scope=AuthorityScope.TENANT_PRINCIPAL,
                tenant_id=_tenant(tenant_id),
                principal_id=_principal(principal_id),
            )
            and execution_binding.attests_snapshot(snapshot_discriminator)
        )
    except Exception:
        return False


def _register_carrier(*, nonce: bytes, kind: bytes, seal: bytes) -> None:
    if not all(type(item) is bytes and len(item) == 32 for item in (nonce, kind, seal)):
        raise _fail("archive Obsidian carrier is invalid")
    with _CARRIER_LOCK:
        if nonce in _CARRIER_RECORDS:
            raise _fail("archive Obsidian carrier is invalid")
        _CARRIER_RECORDS[nonce] = (kind, seal)
        while len(_CARRIER_RECORDS) > MAX_ARCHIVE_OBSIDIAN_LIVE_CARRIERS:
            _CARRIER_RECORDS.popitem(last=False)


def _claim_carrier(*, nonce: bytes, kind: bytes, seal: bytes) -> bool:
    if not all(type(item) is bytes and len(item) == 32 for item in (nonce, kind, seal)):
        return False
    with _CARRIER_LOCK:
        expected = _CARRIER_RECORDS.pop(nonce, None)
    return bool(
        expected is not None
        and hmac.compare_digest(expected[0], kind)
        and hmac.compare_digest(expected[1], seal)
    )


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ArchiveObsidianHit(_ProcessPrivate):
    """Private storage hit; factual body remains provisional until exact read."""

    binding_id: str
    vault_id: str
    path: str
    title: str
    aliases: tuple[str, ...]
    current_revision: str
    lifecycle: LifecycleState
    index_state: ArchiveObsidianIndexState
    index_revision_current: bool
    index_path_current: bool
    metadata_coverage: ArchiveObsidianCoverage
    body_coverage: ArchiveObsidianCoverage
    lane: SearchLane
    match_kind: ArchiveObsidianMatchKind
    rank: int
    factual: bool
    _execution_handle: str
    _indexed_body: str | None
    _principal_handle: bytes
    _request_handle: bytes
    _seal: bytes
    _snapshot_handle: bytes
    _tenant_handle: bytes

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail("archive Obsidian hits require storage authority")

    def __repr__(self) -> str:
        return "<ArchiveObsidianHit sealed private>"

    @property
    def requires_exact_file_reauthorization(self) -> bool:
        return type(self.factual) is bool and self.factual


def _hit_material(hit: ArchiveObsidianHit) -> bytes:
    return _canonical_json(
        {
            "aliases": list(hit.aliases),
            "binding_id": hit.binding_id,
            "body_coverage": hit.body_coverage.value,
            "body_sha256": (
                None
                if hit._indexed_body is None
                else hashlib.sha256(hit._indexed_body.encode("utf-8", errors="strict")).hexdigest()
            ),
            "current_revision": hit.current_revision,
            "factual": hit.factual,
            "execution_handle": hit._execution_handle,
            "index_path_current": hit.index_path_current,
            "index_revision_current": hit.index_revision_current,
            "index_state": hit.index_state.value,
            "lane": hit.lane.value,
            "lifecycle": hit.lifecycle.value,
            "match_kind": hit.match_kind.value,
            "metadata_coverage": hit.metadata_coverage.value,
            "path": hit.path,
            "principal_handle": hit._principal_handle.hex(),
            "rank": hit.rank,
            "request_handle": hit._request_handle.hex(),
            "snapshot_handle": hit._snapshot_handle.hex(),
            "title": hit.title,
            "tenant_handle": hit._tenant_handle.hex(),
            "vault_id": hit.vault_id,
        }
    )


def _hit_is_valid(value: object) -> bool:
    if type(value) is not ArchiveObsidianHit:
        return False
    hit = cast(ArchiveObsidianHit, value)
    try:
        return bool(
            type(hit._seal) is bytes
            and len(hit._seal) == 32
            and type(hit._execution_handle) is str
            and _SHA256.fullmatch(hit._execution_handle) is not None
            and all(
                type(handle) is bytes and len(handle) == 32
                for handle in (
                    hit._principal_handle,
                    hit._request_handle,
                    hit._snapshot_handle,
                    hit._tenant_handle,
                )
            )
            and hmac.compare_digest(
                hit._seal,
                _mac(b"friday/archive-obsidian-hit/v2", _hit_material(hit)),
            )
        )
    except Exception:
        return False


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ArchiveObsidianLanePage(_ProcessPrivate):
    lane: SearchLane
    hits: tuple[ArchiveObsidianHit, ...]
    eligible_authorized: int | None
    examined: int
    matched: int
    returned: int
    limit: int
    capped: bool
    stale: int
    backfill_pending: int
    matched_exact: bool
    unavailable_reason: ArchiveObsidianUnavailableReason | None
    _execution_handle: str
    _principal_handle: bytes
    _request_handle: bytes
    _seal: bytes
    _snapshot_handle: bytes
    _tenant_handle: bytes

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail("archive Obsidian pages require storage authority")

    def __repr__(self) -> str:
        return "<ArchiveObsidianLanePage sealed private>"

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    @property
    def unavailable(self) -> bool:
        return self.unavailable_reason is not None

    def to_coverage(
        self,
        *,
        execution_binding: SearchExecutionBinding,
        tenant_id: str,
        principal_id: str,
        request: ArchiveSearchRequest,
        snapshot_discriminator: str,
    ) -> SearchCoverage:
        try:
            if (
                not _page_is_valid(self)
                or not _scope_matches(
                    tenant_handle=self._tenant_handle,
                    principal_handle=self._principal_handle,
                    request_handle=self._request_handle,
                    snapshot_handle=self._snapshot_handle,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    request=request,
                    snapshot_discriminator=snapshot_discriminator,
                )
                or not _execution_binding_attests(
                    execution_binding,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    request=request,
                    snapshot_discriminator=snapshot_discriminator,
                    lane=self.lane,
                )
                or not hmac.compare_digest(
                    execution_binding.opaque_handle,
                    self._execution_handle,
                )
            ):
                raise _fail("archive Obsidian coverage binding is invalid")
            reasons: set[CoverageState] = set()
            if self.unavailable:
                reasons.add(CoverageState.UNAVAILABLE)
            if self.stale:
                reasons.add(CoverageState.STALE)
            if self.backfill_pending:
                reasons.add(CoverageState.BACKFILL_PENDING)
            if self.capped:
                reasons.add(CoverageState.CAPPED)
            states = (
                (CoverageState.COMPLETE,)
                if not reasons
                else tuple(sorted({CoverageState.PARTIAL, *reasons}, key=lambda item: item.value))
            )
            authority_rechecked = self.unavailable_reason not in {
                ArchiveObsidianUnavailableReason.PRINCIPAL_DENIED,
                ArchiveObsidianUnavailableReason.STORAGE_UNAVAILABLE,
            }
            return SearchCoverage.create(
                corpus=SearchCorpus.OBSIDIAN,
                lane=self.lane,
                execution_binding=execution_binding,
                states=states,
                eligible_authorized=self.eligible_authorized,
                examined=self.examined,
                matched_at_least=self.matched,
                returned=self.returned,
                authority_rechecked=authority_rechecked,
                snapshot_current=True,
                limit=self.limit if self.capped else None,
                next_cursor_available=False,
            )
        except ArchiveObsidianStorageError:
            raise
        except Exception:
            raise _fail("archive Obsidian coverage binding is invalid") from None


def _page_material(page: ArchiveObsidianLanePage) -> bytes:
    return _canonical_json(
        {
            "backfill_pending": page.backfill_pending,
            "capped": page.capped,
            "eligible_authorized": page.eligible_authorized,
            "execution_handle": page._execution_handle,
            "examined": page.examined,
            "hit_seals": [hit._seal.hex() for hit in page.hits],
            "lane": page.lane.value,
            "limit": page.limit,
            "matched": page.matched,
            "matched_exact": page.matched_exact,
            "principal_handle": page._principal_handle.hex(),
            "request_handle": page._request_handle.hex(),
            "returned": page.returned,
            "snapshot_handle": page._snapshot_handle.hex(),
            "stale": page.stale,
            "tenant_handle": page._tenant_handle.hex(),
            "unavailable_reason": (
                None if page.unavailable_reason is None else page.unavailable_reason.value
            ),
        }
    )


def _page_is_valid(value: object) -> bool:
    if type(value) is not ArchiveObsidianLanePage:
        return False
    page = cast(ArchiveObsidianLanePage, value)
    try:
        available = page.unavailable_reason is None
        counts = (page.examined, page.matched, page.returned, page.stale, page.backfill_pending)
        return bool(
            type(page.lane) is SearchLane
            and type(page.hits) is tuple
            and all(_hit_is_valid(hit) for hit in page.hits)
            and all(
                hmac.compare_digest(hit._principal_handle, page._principal_handle)
                and hmac.compare_digest(hit._request_handle, page._request_handle)
                and hmac.compare_digest(hit._snapshot_handle, page._snapshot_handle)
                and hit.lane is page.lane
                and hmac.compare_digest(hit._execution_handle, page._execution_handle)
                and hmac.compare_digest(hit._tenant_handle, page._tenant_handle)
                for hit in page.hits
            )
            and (page.eligible_authorized is None or type(page.eligible_authorized) is int)
            and (page.eligible_authorized is None or page.eligible_authorized >= 0)
            and all(type(item) is int and item >= 0 for item in counts)
            and page.matched <= page.examined
            and page.returned == len(page.hits)
            and page.returned <= page.matched
            and (page.eligible_authorized is None or page.examined <= page.eligible_authorized)
            and type(page.capped) is bool
            and type(page._execution_handle) is str
            and _SHA256.fullmatch(page._execution_handle) is not None
            and type(page.matched_exact) is bool
            and type(page.limit) is int
            and 1 <= page.limit <= MAX_ARCHIVE_OBSIDIAN_RESULTS
            and (
                page.unavailable_reason is None
                or type(page.unavailable_reason) is ArchiveObsidianUnavailableReason
            )
            and (available or not (page.examined or page.matched or page.returned or page.capped))
            and all(
                type(handle) is bytes and len(handle) == 32
                for handle in (
                    page._principal_handle,
                    page._request_handle,
                    page._snapshot_handle,
                    page._tenant_handle,
                )
            )
            and type(page._seal) is bytes
            and len(page._seal) == 32
            and hmac.compare_digest(
                page._seal,
                _mac(b"friday/archive-obsidian-page/v3", _page_material(page)),
            )
        )
    except Exception:
        return False


def _new_page(
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    execution_binding: SearchExecutionBinding,
    lane: SearchLane,
    hits: tuple[ArchiveObsidianHit, ...],
    eligible_authorized: int | None,
    examined: int,
    matched: int,
    limit: int,
    capped: bool,
    unavailable_reason: ArchiveObsidianUnavailableReason | None,
    stale: int,
    backfill_pending: int,
    matched_exact: bool,
) -> ArchiveObsidianLanePage:
    tenant_handle, principal_handle, request_handle, snapshot_handle = _scope_handles(
        tenant_id=tenant_id,
        principal_id=principal_id,
        request=request,
        snapshot_discriminator=snapshot_discriminator,
    )
    returned = len(hits)
    counts = (examined, matched, returned, stale, backfill_pending)
    if (
        type(lane) is not SearchLane
        or not _execution_binding_attests(
            execution_binding,
            tenant_id=tenant_id,
            principal_id=principal_id,
            request=request,
            snapshot_discriminator=snapshot_discriminator,
            lane=lane,
        )
        or type(hits) is not tuple
        or any(type(hit) is not ArchiveObsidianHit or not _hit_is_valid(hit) for hit in hits)
        or eligible_authorized is not None
        and (isinstance(eligible_authorized, bool) or eligible_authorized < 0)
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts)
        or matched > examined
        or returned > matched
        or eligible_authorized is not None
        and examined > eligible_authorized
        or any(type(item) is not bool for item in (capped, matched_exact))
        or unavailable_reason is not None
        and type(unavailable_reason) is not ArchiveObsidianUnavailableReason
        or unavailable_reason is not None
        and (examined or matched or returned or capped)
        or any(
            not hmac.compare_digest(handle, expected)
            for hit in hits
            for handle, expected in (
                (hit._principal_handle, principal_handle),
                (hit._request_handle, request_handle),
                (hit._snapshot_handle, snapshot_handle),
                (hit._tenant_handle, tenant_handle),
            )
        )
        or any(
            not hmac.compare_digest(hit._execution_handle, execution_binding.opaque_handle) for hit in hits
        )
        or not 1 <= limit <= MAX_ARCHIVE_OBSIDIAN_RESULTS
    ):
        raise _fail("archive Obsidian page is inconsistent")
    page = cast(ArchiveObsidianLanePage, object.__new__(ArchiveObsidianLanePage))
    for name, value in (
        ("lane", lane),
        ("hits", hits),
        ("eligible_authorized", eligible_authorized),
        ("examined", examined),
        ("matched", matched),
        ("returned", returned),
        ("limit", limit),
        ("capped", capped),
        ("stale", stale),
        ("backfill_pending", backfill_pending),
        ("matched_exact", matched_exact),
        ("unavailable_reason", unavailable_reason),
        ("_execution_handle", execution_binding.opaque_handle),
        ("_principal_handle", principal_handle),
        ("_request_handle", request_handle),
        ("_seal", b"0" * 32),
        ("_snapshot_handle", snapshot_handle),
        ("_tenant_handle", tenant_handle),
    ):
        object.__setattr__(page, name, value)
    object.__setattr__(
        page,
        "_seal",
        _mac(b"friday/archive-obsidian-page/v3", _page_material(page)),
    )
    if not _page_is_valid(page):
        raise _fail("archive Obsidian page is inconsistent")
    return page


def _unavailable_page(
    lane: SearchLane,
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    execution_binding: SearchExecutionBinding,
    reason: ArchiveObsidianUnavailableReason,
    limit: int,
    eligible_authorized: int | None = None,
    stale: int = 0,
    backfill_pending: int = 0,
) -> ArchiveObsidianLanePage:
    return _new_page(
        tenant_id=tenant_id,
        principal_id=principal_id,
        request=request,
        snapshot_discriminator=snapshot_discriminator,
        execution_binding=execution_binding,
        lane=lane,
        hits=(),
        eligible_authorized=eligible_authorized,
        examined=0,
        matched=0,
        limit=limit,
        capped=False,
        unavailable_reason=reason,
        stale=stale,
        backfill_pending=backfill_pending,
        matched_exact=False,
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        raise _fail("archive Obsidian storage is unavailable") from None


def _principal_is_active(conn: sqlite3.Connection, principal_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT status FROM users WHERE id=?",
            (principal_id,),
        ).fetchone()
    except sqlite3.Error:
        raise _fail("archive Obsidian principal authority is unavailable") from None
    return bool(row is not None and type(row[0]) is str and row[0] == "active")


def _principal_vault_is_ready(conn: sqlite3.Connection, principal_id: str) -> bool:
    rows = _select_rows(
        conn,
        "SELECT state FROM obsidian_vaults WHERE user_id=? ORDER BY id LIMIT 2",
        (principal_id,),
    )
    if len(rows) > 1:
        raise _fail("archive Obsidian vault authority is invalid")
    if not rows:
        return False
    state = rows[0].get("state")
    if type(state) is not str or state not in {
        "provisioning",
        "offering_folder",
        "awaiting_folder_acceptance",
        "initial_sync",
        "awaiting_vault_registration",
        "verifying",
        "ready",
        "disconnected",
        "failed",
    }:
        raise _fail("archive Obsidian vault authority is invalid")
    return state == "ready"


def _has_unsupported_lifecycle(
    request: ArchiveSearchRequest,
    *,
    lane: SearchLane,
) -> bool:
    requested: tuple[LifecycleState, ...] | None = None
    for constraint in request.lifecycle_constraints:
        if constraint.corpus is ArchiveSearchCorpus.OBSIDIAN:
            requested = constraint.states
            break
    if requested is None:
        return False
    supported = (
        {LifecycleState.ACTIVE}
        if lane is SearchLane.LEXICAL
        else {LifecycleState.ACTIVE, LifecycleState.TOMBSTONED}
    )
    return not set(requested) <= supported


def _lifecycle_clause(request: ArchiveSearchRequest, *, factual: bool) -> str:
    allowed = {LifecycleState.ACTIVE, LifecycleState.TOMBSTONED}
    for constraint in request.lifecycle_constraints:
        if constraint.corpus is ArchiveSearchCorpus.OBSIDIAN:
            allowed = set(constraint.states)
            break
    if factual:
        allowed &= {LifecycleState.ACTIVE}
    predicates: list[str] = []
    if LifecycleState.ACTIVE in allowed:
        predicates.append("b.deleted_at IS NULL")
    if LifecycleState.TOMBSTONED in allowed:
        predicates.append("b.deleted_at IS NOT NULL")
    return "0" if not predicates else "(" + " OR ".join(predicates) + ")"


def _aliases_expression() -> str:
    return """CASE
        WHEN idx.binding_id IS NULL OR idx.state<>'ready'
          OR idx.revision<>b.current_revision OR idx.path<>b.current_path
          OR idx.metadata_coverage<>'complete' OR NOT json_valid(idx.metadata_json)
        THEN '[]'
        WHEN json_type(idx.metadata_json,'$.aliases')='text'
        THEN json_array(json_extract(idx.metadata_json,'$.aliases'))
        WHEN json_type(idx.metadata_json,'$.aliases')='array'
        THEN json_extract(idx.metadata_json,'$.aliases')
        WHEN json_type(idx.metadata_json,'$.aliases') IS NULL THEN '[]'
        ELSE NULL END"""


def _owned_cte(*, index_available: bool, lifecycle_clause: str) -> str:
    if index_available:
        projection = f"""idx.binding_id AS index_binding_id,
               CASE WHEN idx.binding_id IS NULL THEN 'missing' ELSE idx.state END AS index_state,
               idx.revision AS index_revision, idx.path AS index_path,
               COALESCE(idx.title,'') AS index_title,
               COALESCE(idx.metadata_coverage,'none') AS metadata_coverage,
               COALESCE(idx.body_coverage,'none') AS body_coverage,
               COALESCE(idx.source_size_bytes,0) AS source_size_bytes,
               {_aliases_expression()} AS aliases_json"""
        join = """LEFT JOIN obsidian_note_index idx
                    ON idx.user_id=b.user_id
                   AND idx.binding_id=b.id
                   AND idx.vault_id=b.vault_id"""
    else:
        projection = """NULL AS index_binding_id, 'missing' AS index_state,
               NULL AS index_revision, NULL AS index_path, '' AS index_title,
               'none' AS metadata_coverage, 'none' AS body_coverage,
               0 AS source_size_bytes, '[]' AS aliases_json"""
        join = ""
    return f"""owned AS MATERIALIZED (
        SELECT b.id AS binding_id, b.user_id, b.vault_id, b.current_path, b.current_revision,
               b.deleted_at, {projection}
          FROM obsidian_note_bindings b
          {join}
         WHERE b.user_id=?
           AND EXISTS (SELECT 1 FROM users authority
                        WHERE authority.id=? AND authority.status='active')
           AND {lifecycle_clause}
    )"""


def _index_stale_sql(alias: str = "o") -> str:
    return f"""{alias}.index_binding_id IS NOT NULL AND (
        {alias}.index_state NOT IN ('ready','stale')
        OR {alias}.index_state='stale'
        OR {alias}.index_revision<>{alias}.current_revision
        OR {alias}.index_path<>{alias}.current_path
    )"""


def _index_current_sql(alias: str = "o") -> str:
    return f"""{alias}.index_binding_id IS NOT NULL
        AND {alias}.index_state='ready'
        AND {alias}.index_revision={alias}.current_revision
        AND {alias}.index_path={alias}.current_path"""


def _select_rows(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
) -> list[dict[str, Any]]:
    try:
        cursor = conn.execute(sql, parameters)
        names = tuple(str(item[0]) for item in (cursor.description or ()))
        return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]
    except sqlite3.Error:
        raise _fail("archive Obsidian storage read is unavailable") from None


def _summary(rows: list[dict[str, Any]]) -> tuple[int, int, int, list[dict[str, Any]]]:
    if not rows:
        raise _fail("archive Obsidian storage summary is unavailable")
    try:
        total = int(rows[0]["total"])
        stale = int(rows[0]["stale"])
        backfill = int(rows[0]["backfill"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise _fail("archive Obsidian storage summary is invalid") from None
    if min(total, stale, backfill) < 0 or stale > total or backfill > total:
        raise _fail("archive Obsidian storage summary is invalid")
    return total, stale, backfill, [row for row in rows if row.get("binding_id") is not None]


def _aliases(value: object) -> tuple[str, ...]:
    if type(value) is not str:
        raise _fail("archive Obsidian aliases are invalid")
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, UnicodeError):
        raise _fail("archive Obsidian aliases are invalid") from None
    if type(decoded) is not list or len(decoded) > MAX_ARCHIVE_OBSIDIAN_ALIASES:
        raise _fail("archive Obsidian aliases are invalid")
    aliases = tuple(_bounded_text(item, label="alias", maximum=512) for item in decoded)
    canonical = tuple(sorted(set(aliases), key=lambda item: (_fold(item), item)))
    if len(canonical) != len(aliases):
        raise _fail("archive Obsidian aliases are invalid")
    return canonical


def _row_state(
    row: dict[str, Any],
) -> tuple[
    str,
    str,
    str,
    LifecycleState,
    ArchiveObsidianIndexState,
    bool,
    bool,
    ArchiveObsidianCoverage,
    ArchiveObsidianCoverage,
    str,
    tuple[str, ...],
]:
    binding = _binding_id(row.get("binding_id"))
    vault = _vault_id(row.get("vault_id"))
    current_path = _path(row.get("current_path"))
    current_revision = _revision(row.get("current_revision"))
    lifecycle = LifecycleState.ACTIVE if row.get("deleted_at") is None else LifecycleState.TOMBSTONED
    try:
        index_state = ArchiveObsidianIndexState(str(row.get("index_state")))
        metadata_coverage = ArchiveObsidianCoverage(str(row.get("metadata_coverage")))
        body_coverage = ArchiveObsidianCoverage(str(row.get("body_coverage")))
    except ValueError:
        raise _fail("archive Obsidian index state is invalid") from None
    index_revision = row.get("index_revision")
    index_path = row.get("index_path")
    revision_current = index_state is not ArchiveObsidianIndexState.MISSING and (
        _revision(index_revision) == current_revision
    )
    path_current = index_state is not ArchiveObsidianIndexState.MISSING and (
        _path(index_path) == current_path
    )
    current_index = index_state is ArchiveObsidianIndexState.READY and revision_current and path_current
    raw_title = row.get("index_title") if current_index else ""
    title = _bounded_text(raw_title, label="title", maximum=512, empty=True)
    if not title:
        title = PurePosixPath(current_path).stem
    aliases = _aliases(row.get("aliases_json")) if current_index else ()
    return (
        binding,
        vault,
        current_path,
        lifecycle,
        index_state,
        revision_current,
        path_current,
        metadata_coverage,
        body_coverage,
        title,
        aliases,
    )


def _identity_values(path: str, title: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    pure = PurePosixPath(path)
    return tuple(
        dict.fromkeys(_fold(value) for value in (path, pure.name, pure.stem, title, *aliases) if value)
    )


def _needles(request: ArchiveSearchRequest) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            _fold(value) for value in (request.query, *request.filename_hints, *request.title_hints) if value
        )
    )


def _identity_match(
    values: tuple[str, ...],
    needles: tuple[str, ...],
) -> tuple[int, ArchiveObsidianMatchKind] | None:
    if any(needle == value for needle in needles for value in values):
        return 0, ArchiveObsidianMatchKind.EXACT
    if any(value.startswith(needle) for needle in needles for value in values):
        return 1, ArchiveObsidianMatchKind.PREFIX
    if any(needle in value for needle in needles for value in values):
        return 2, ArchiveObsidianMatchKind.SUBSTRING

    for needle in needles:
        if not 5 <= len(needle) <= 64:
            continue
        budget = 2 if len(needle) >= 8 else 1
        if any(
            4 <= len(value) <= 128
            and abs(len(value) - len(needle)) <= budget
            and _edit_distance(needle, value, budget) <= budget
            for value in values
        ):
            return 3, ArchiveObsidianMatchKind.TYPO
    for needle in needles:
        if not looks_mistyped(needle) or not 2 <= len(needle) <= 128:
            continue
        alternate = _fold(switched(needle))
        if alternate != needle and any(
            alternate == value or value.startswith(alternate) or alternate in value for value in values
        ):
            return 4, ArchiveObsidianMatchKind.KEYBOARD_LAYOUT
    return None


def _new_hit(
    row: dict[str, Any],
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    execution_binding: SearchExecutionBinding,
    lane: SearchLane,
    match_kind: ArchiveObsidianMatchKind,
    rank: int,
    indexed_body: str | None = None,
) -> ArchiveObsidianHit:
    tenant_handle, principal_handle, request_handle, snapshot_handle = _scope_handles(
        tenant_id=tenant_id,
        principal_id=principal_id,
        request=request,
        snapshot_discriminator=snapshot_discriminator,
    )
    (
        binding,
        vault,
        current_path,
        lifecycle,
        index_state,
        revision_current,
        path_current,
        metadata_coverage,
        body_coverage,
        title,
        aliases,
    ) = _row_state(row)
    factual = lane is SearchLane.LEXICAL
    if not _execution_binding_attests(
        execution_binding,
        tenant_id=tenant_id,
        principal_id=principal_id,
        request=request,
        snapshot_discriminator=snapshot_discriminator,
        lane=lane,
    ):
        raise _fail("archive Obsidian execution binding is invalid")
    if factual:
        if (
            lifecycle is not LifecycleState.ACTIVE
            or index_state is not ArchiveObsidianIndexState.READY
            or not revision_current
            or not path_current
            or body_coverage is not ArchiveObsidianCoverage.COMPLETE
            or type(indexed_body) is not str
        ):
            raise _fail("archive Obsidian factual hit is not current")
        try:
            body_bytes = indexed_body.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _fail("archive Obsidian factual projection is invalid") from None
        if len(body_bytes) > MAX_ARCHIVE_OBSIDIAN_BODY_BYTES or "\x00" in indexed_body:
            raise _fail("archive Obsidian factual projection is invalid")
    elif indexed_body is not None:
        raise _fail("archive Obsidian navigation hit cannot carry a body")
    hit = cast(ArchiveObsidianHit, object.__new__(ArchiveObsidianHit))
    for name, value in (
        ("binding_id", binding),
        ("vault_id", vault),
        ("path", current_path),
        ("title", title),
        ("aliases", aliases),
        ("current_revision", _revision(row.get("current_revision"))),
        ("lifecycle", lifecycle),
        ("index_state", index_state),
        ("index_revision_current", revision_current),
        ("index_path_current", path_current),
        ("metadata_coverage", metadata_coverage),
        ("body_coverage", body_coverage),
        ("lane", lane),
        ("match_kind", match_kind),
        ("rank", rank),
        ("factual", factual),
        ("_execution_handle", execution_binding.opaque_handle),
        ("_indexed_body", indexed_body),
        ("_principal_handle", principal_handle),
        ("_request_handle", request_handle),
        ("_seal", b"0" * 32),
        ("_snapshot_handle", snapshot_handle),
        ("_tenant_handle", tenant_handle),
    ):
        object.__setattr__(hit, name, value)
    object.__setattr__(
        hit,
        "_seal",
        _mac(b"friday/archive-obsidian-hit/v2", _hit_material(hit)),
    )
    return hit


def _identity_page(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    execution_binding: SearchExecutionBinding,
    lane: SearchLane,
    limit: int,
    index_available: bool,
) -> ArchiveObsidianLanePage:
    owned = _owned_cte(
        index_available=index_available,
        lifecycle_clause=_lifecycle_clause(request, factual=False),
    )
    stale = _index_stale_sql()
    current = _index_current_sql()
    sql = f"""WITH {owned},
        statistics AS MATERIALIZED (
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN {stale} THEN 1 ELSE 0 END),0) AS stale,
                   COALESCE(SUM(CASE
                       WHEN o.index_binding_id IS NULL THEN 1
                       WHEN {current} AND o.metadata_coverage<>'complete' THEN 1
                       ELSE 0 END),0) AS backfill
              FROM owned o
        ),
        bounded AS MATERIALIZED (
            SELECT * FROM owned
             ORDER BY jericho_casefold(current_path), current_path, binding_id
             LIMIT ?
        )
        SELECT statistics.total, statistics.stale, statistics.backfill, bounded.*
          FROM statistics LEFT JOIN bounded ON 1=1
         ORDER BY CASE WHEN bounded.binding_id IS NULL THEN 1 ELSE 0 END,
                  jericho_casefold(bounded.current_path), bounded.current_path,
                  bounded.binding_id"""
    rows = _select_rows(
        conn,
        sql,
        (principal_id, principal_id, MAX_ARCHIVE_OBSIDIAN_IDENTITY_SCAN + 1),
    )
    total, stale_count, backfill, candidates = _summary(rows)
    bounded = candidates[:MAX_ARCHIVE_OBSIDIAN_IDENTITY_SCAN]
    scan_complete = total <= MAX_ARCHIVE_OBSIDIAN_IDENTITY_SCAN
    needles = _needles(request)
    ranked: list[tuple[int, str, str, dict[str, Any], ArchiveObsidianMatchKind]] = []
    for row in bounded:
        state = _row_state(row)
        match = _identity_match(_identity_values(state[2], state[9], state[10]), needles)
        if match is None:
            continue
        score, kind = match
        if lane is SearchLane.EXACT_IDENTITY and kind is not ArchiveObsidianMatchKind.EXACT:
            continue
        if lane is SearchLane.APPROXIMATE_IDENTITY and kind not in {
            ArchiveObsidianMatchKind.TYPO,
            ArchiveObsidianMatchKind.KEYBOARD_LAYOUT,
        }:
            continue
        ranked.append((score, _fold(state[2]), state[0], row, kind))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    visible = ranked[:limit]
    hits = tuple(
        _new_hit(
            row,
            tenant_id=tenant_id,
            principal_id=principal_id,
            request=request,
            snapshot_discriminator=snapshot_discriminator,
            execution_binding=execution_binding,
            lane=lane,
            match_kind=kind,
            rank=rank,
        )
        for rank, (_score, _path_key, _binding, row, kind) in enumerate(visible, 1)
    )
    matched = len(ranked)
    return _new_page(
        tenant_id=tenant_id,
        principal_id=principal_id,
        request=request,
        snapshot_discriminator=snapshot_discriminator,
        execution_binding=execution_binding,
        lane=lane,
        hits=hits,
        eligible_authorized=total,
        examined=len(bounded),
        matched=matched,
        limit=limit,
        capped=not scan_complete or matched > limit,
        unavailable_reason=None,
        stale=stale_count,
        backfill_pending=backfill,
        matched_exact=scan_complete,
    )


def _lexical_page(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    execution_binding: SearchExecutionBinding,
    limit: int,
) -> ArchiveObsidianLanePage:
    owned = _owned_cte(
        index_available=True,
        lifecycle_clause=_lifecycle_clause(request, factual=True),
    )
    current = _index_current_sql()
    stale = _index_stale_sql()
    folded_query = _fold(request.query)
    terms = tuple(
        dict.fromkeys(
            _fold(term) for term in re.findall(r"[^\W_]+", request.query, flags=re.UNICODE) if len(term) >= 2
        )
    )
    terms_json = json.dumps(list(terms), ensure_ascii=False, separators=(",", ":"))
    sql = f"""WITH {owned},
        factual AS MATERIALIZED (
            SELECT o.*, idx.body_text
              FROM owned o
              JOIN obsidian_note_index idx
                ON idx.user_id=o.user_id
               AND idx.binding_id=o.binding_id
               AND idx.vault_id=o.vault_id
               AND idx.revision=o.current_revision
               AND idx.path=o.current_path
             WHERE {current}
               AND o.body_coverage='complete'
        ),
        needles AS MATERIALIZED (
            SELECT value AS term FROM json_each(?) WHERE type='text'
        ),
        matched_rows AS MATERIALIZED (
            SELECT f.*,
                   CASE
                     WHEN instr(replace(jericho_casefold(f.body_text),'ё','е'),?)>0 THEN 0
                     ELSE 1 END AS score
              FROM factual f
             WHERE instr(replace(jericho_casefold(f.body_text),'ё','е'),?)>0
                OR EXISTS (
                    SELECT 1 FROM needles n
                     WHERE instr(replace(jericho_casefold(f.body_text),'ё','е'),n.term)>0
                )
        ),
        ranked AS MATERIALIZED (
            SELECT m.*, ROW_NUMBER() OVER (
                       ORDER BY score, jericho_casefold(current_path), current_path, binding_id
                   ) AS lane_rank
              FROM matched_rows m
        ),
        page AS MATERIALIZED (
            SELECT * FROM ranked ORDER BY lane_rank LIMIT ?
        ),
        statistics AS MATERIALIZED (
            SELECT (SELECT COUNT(*) FROM owned) AS total,
                   (SELECT COUNT(*) FROM factual) AS examined,
                   (SELECT COUNT(*) FROM matched_rows) AS matched,
                   (SELECT COUNT(*) FROM owned o WHERE {stale}) AS stale,
                   (SELECT COUNT(*) FROM owned o
                     WHERE o.index_binding_id IS NULL
                        OR (NOT ({stale}) AND NOT ({current} AND o.body_coverage='complete'))
                   ) AS backfill
        )
        SELECT statistics.*, page.* FROM statistics LEFT JOIN page ON 1=1
         ORDER BY CASE WHEN page.binding_id IS NULL THEN 1 ELSE 0 END, page.lane_rank"""
    rows = _select_rows(
        conn,
        sql,
        (
            principal_id,
            principal_id,
            terms_json,
            folded_query,
            folded_query,
            limit + 1,
        ),
    )
    if not rows:
        raise _fail("archive Obsidian lexical summary is unavailable")
    try:
        total = int(rows[0]["total"])
        examined = int(rows[0]["examined"])
        matched = int(rows[0]["matched"])
        stale_count = int(rows[0]["stale"])
        backfill = int(rows[0]["backfill"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise _fail("archive Obsidian lexical summary is invalid") from None
    if min(total, examined, matched, stale_count, backfill) < 0 or not (matched <= examined <= total):
        raise _fail("archive Obsidian lexical summary is invalid")
    hits_rows = [row for row in rows if row.get("binding_id") is not None][:limit]
    hits = tuple(
        _new_hit(
            row,
            tenant_id=tenant_id,
            principal_id=principal_id,
            request=request,
            snapshot_discriminator=snapshot_discriminator,
            execution_binding=execution_binding,
            lane=SearchLane.LEXICAL,
            match_kind=(
                ArchiveObsidianMatchKind.LEXICAL_PHRASE
                if int(row["score"]) == 0
                else ArchiveObsidianMatchKind.LEXICAL_TERMS
            ),
            rank=rank,
            indexed_body=cast(str, row.get("body_text")),
        )
        for rank, row in enumerate(hits_rows, 1)
    )
    return _new_page(
        tenant_id=tenant_id,
        principal_id=principal_id,
        request=request,
        snapshot_discriminator=snapshot_discriminator,
        execution_binding=execution_binding,
        lane=SearchLane.LEXICAL,
        hits=hits,
        eligible_authorized=total,
        examined=examined,
        matched=matched,
        limit=limit,
        capped=matched > limit,
        unavailable_reason=None,
        stale=stale_count,
        backfill_pending=backfill,
        matched_exact=True,
    )


def _count_bindings(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    request: ArchiveSearchRequest,
    factual: bool,
) -> int:
    owned = _owned_cte(
        index_available=False,
        lifecycle_clause=_lifecycle_clause(request, factual=factual),
    )
    rows = _select_rows(
        conn,
        f"WITH {owned} SELECT COUNT(*) AS total FROM owned",
        (principal_id, principal_id),
    )
    try:
        total = int(rows[0]["total"])
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        raise _fail("archive Obsidian storage summary is invalid") from None
    if total < 0:
        raise _fail("archive Obsidian storage summary is invalid")
    return total


def _count_default_unsearchable_lexical_bindings(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
) -> int:
    owned = _owned_cte(
        index_available=False,
        lifecycle_clause="b.deleted_at IS NOT NULL",
    )
    rows = _select_rows(
        conn,
        f"WITH {owned} SELECT COUNT(*) AS total FROM owned",
        (principal_id, principal_id),
    )
    try:
        total = int(rows[0]["total"])
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        raise _fail("archive Obsidian lifecycle summary is invalid") from None
    if total < 0:
        raise _fail("archive Obsidian lifecycle summary is invalid")
    return total


def _uses_default_obsidian_lifecycle(request: ArchiveSearchRequest) -> bool:
    return not any(
        constraint.corpus is ArchiveSearchCorpus.OBSIDIAN for constraint in request.lifecycle_constraints
    )


def select_archive_obsidian_lane_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    execution_binding: SearchExecutionBinding,
    lane: SearchLane,
    limit: int | None = None,
) -> ArchiveObsidianLanePage:
    """Select one owner-scoped lane without opening, closing or writing a transaction."""

    try:
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            raise _fail("archive Obsidian search requires a caller-owned transaction")
        if type(request) is not ArchiveSearchRequest:
            raise _fail("archive Obsidian request is invalid")
        if ArchiveSearchCorpus.OBSIDIAN not in request.corpora or type(lane) is not SearchLane:
            raise _fail("archive Obsidian target is invalid")
        tenant = _tenant(tenant_id)
        principal = _principal(principal_id)
        _scope_handles(
            tenant_id=tenant,
            principal_id=principal,
            request=request,
            snapshot_discriminator=snapshot_discriminator,
        )
        page_limit = _limit(request.limit if limit is None else limit, request)
        if not _execution_binding_attests(
            execution_binding,
            tenant_id=tenant,
            principal_id=principal,
            request=request,
            snapshot_discriminator=snapshot_discriminator,
            lane=lane,
        ):
            raise _fail("archive Obsidian execution binding is invalid")
        if not _principal_is_active(conn, principal):
            return _unavailable_page(
                lane,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                reason=ArchiveObsidianUnavailableReason.PRINCIPAL_DENIED,
                limit=page_limit,
            )
        if not _table_exists(conn, "obsidian_vaults"):
            return _unavailable_page(
                lane,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                reason=ArchiveObsidianUnavailableReason.STORAGE_UNAVAILABLE,
                limit=page_limit,
            )
        if not _principal_vault_is_ready(conn, principal):
            return _unavailable_page(
                lane,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                reason=ArchiveObsidianUnavailableReason.VAULT_UNAVAILABLE,
                limit=page_limit,
            )
        if not _table_exists(conn, "obsidian_note_bindings"):
            return _unavailable_page(
                lane,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                reason=ArchiveObsidianUnavailableReason.STORAGE_UNAVAILABLE,
                limit=page_limit,
            )
        temporal_unsupported = any(
            constraint.corpus is ArchiveSearchCorpus.OBSIDIAN for constraint in request.temporal_constraints
        )
        if temporal_unsupported:
            return _unavailable_page(
                lane,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                reason=ArchiveObsidianUnavailableReason.TEMPORAL_UNSUPPORTED,
                limit=page_limit,
            )
        if lane in _SUPPORTED_LANES and _has_unsupported_lifecycle(request, lane=lane):
            return _unavailable_page(
                lane,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                reason=ArchiveObsidianUnavailableReason.LIFECYCLE_UNSUPPORTED,
                limit=page_limit,
            )
        if (
            lane is SearchLane.LEXICAL
            and _uses_default_obsidian_lifecycle(request)
            and _count_default_unsearchable_lexical_bindings(
                conn,
                principal_id=principal,
            )
            > 0
        ):
            return _unavailable_page(
                lane,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                reason=ArchiveObsidianUnavailableReason.LIFECYCLE_UNSUPPORTED,
                limit=page_limit,
            )
        if lane not in _SUPPORTED_LANES:
            return _unavailable_page(
                lane,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                reason=ArchiveObsidianUnavailableReason.LANE_UNSUPPORTED,
                limit=page_limit,
                eligible_authorized=_count_bindings(
                    conn,
                    principal_id=principal,
                    request=request,
                    factual=True,
                ),
            )
        index_available = _table_exists(conn, "obsidian_note_index")
        if lane is SearchLane.LEXICAL:
            if not index_available:
                total = _count_bindings(
                    conn,
                    principal_id=principal,
                    request=request,
                    factual=True,
                )
                return _unavailable_page(
                    lane,
                    tenant_id=tenant,
                    principal_id=principal,
                    request=request,
                    snapshot_discriminator=snapshot_discriminator,
                    execution_binding=execution_binding,
                    reason=ArchiveObsidianUnavailableReason.INDEX_UNAVAILABLE,
                    limit=page_limit,
                    eligible_authorized=total,
                    backfill_pending=total,
                )
            return _lexical_page(
                conn,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                execution_binding=execution_binding,
                limit=page_limit,
            )
        return _identity_page(
            conn,
            tenant_id=tenant,
            principal_id=principal,
            request=request,
            snapshot_discriminator=snapshot_discriminator,
            execution_binding=execution_binding,
            lane=lane,
            limit=page_limit,
            index_available=index_available,
        )
    except ArchiveObsidianStorageError:
        raise
    except Exception:
        raise _fail("archive Obsidian storage read failed") from None


_HIT_FIELDS: Final = (
    "binding_id",
    "vault_id",
    "path",
    "title",
    "aliases",
    "current_revision",
    "lifecycle",
    "index_state",
    "index_revision_current",
    "index_path_current",
    "metadata_coverage",
    "body_coverage",
    "lane",
    "match_kind",
    "rank",
    "factual",
    "_execution_handle",
    "_indexed_body",
    "_principal_handle",
    "_request_handle",
    "_seal",
    "_snapshot_handle",
    "_tenant_handle",
)


def _validated_hit_copy(value: object) -> ArchiveObsidianHit | None:
    if type(value) is not ArchiveObsidianHit:
        return None
    try:
        copied = cast(ArchiveObsidianHit, object.__new__(ArchiveObsidianHit))
        for name in _HIT_FIELDS:
            object.__setattr__(copied, name, getattr(value, name))
        return copied if _hit_is_valid(copied) else None
    except Exception:
        return None


@dataclass(frozen=True, slots=True, repr=False, init=False)
class VerifiedArchiveObsidianNavigation(_ProcessPrivate):
    """One-use navigation carrier consumed only through a final callback."""

    _execution_handle: str
    _hit_seal: bytes
    _nonce: bytes
    _phase: ArchiveObsidianReadPhase
    _principal_handle: bytes
    _request_handle: bytes
    _seal: bytes
    _snapshot_handle: bytes
    _tenant_handle: bytes

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail("verified archive Obsidian navigation requires storage authority")

    def __repr__(self) -> str:
        return "<VerifiedArchiveObsidianNavigation sealed private>"

    def consume_with(
        self,
        *,
        execution_binding: SearchExecutionBinding,
        tenant_id: str,
        principal_id: str,
        request: ArchiveSearchRequest,
        snapshot_discriminator: str,
        hit: ArchiveObsidianHit,
        phase: ArchiveObsidianReadPhase,
        consumer: ArchiveObsidianNavigationConsumer[_ResultT],
    ) -> _ResultT:
        """Atomically consume once, passing only copied immutable hit fields."""

        try:
            carrier = _validated_navigation_copy(self)
            safe_hit = _validated_hit_copy(hit)
            if (
                carrier is None
                or safe_hit is None
                or safe_hit.factual
                or type(phase) is not ArchiveObsidianReadPhase
                or phase is not carrier._phase
                or not callable(consumer)
                or not hmac.compare_digest(safe_hit._seal, carrier._hit_seal)
                or not _scope_matches(
                    tenant_handle=carrier._tenant_handle,
                    principal_handle=carrier._principal_handle,
                    request_handle=carrier._request_handle,
                    snapshot_handle=carrier._snapshot_handle,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    request=request,
                    snapshot_discriminator=snapshot_discriminator,
                )
                or not _execution_binding_attests(
                    execution_binding,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    request=request,
                    snapshot_discriminator=snapshot_discriminator,
                    lane=safe_hit.lane,
                )
                or not hmac.compare_digest(
                    execution_binding.opaque_handle,
                    carrier._execution_handle,
                )
                or not _claim_carrier(
                    nonce=carrier._nonce,
                    kind=_NAVIGATION_CARRIER_KIND,
                    seal=carrier._seal,
                )
            ):
                raise _fail("archive Obsidian navigation carrier is unavailable")
        except ArchiveObsidianStorageError:
            raise
        except Exception:
            raise _fail("archive Obsidian navigation carrier is unavailable") from None
        try:
            return consumer(
                binding_id=safe_hit.binding_id,
                vault_id=safe_hit.vault_id,
                path=safe_hit.path,
                title=safe_hit.title,
                aliases=tuple(safe_hit.aliases),
                current_revision=safe_hit.current_revision,
                lifecycle=safe_hit.lifecycle,
                index_state=safe_hit.index_state,
                index_revision_current=safe_hit.index_revision_current,
                index_path_current=safe_hit.index_path_current,
                metadata_coverage=safe_hit.metadata_coverage,
                body_coverage=safe_hit.body_coverage,
                lane=safe_hit.lane,
                match_kind=safe_hit.match_kind,
                rank=safe_hit.rank,
            )
        except Exception:
            raise _fail("archive Obsidian navigation consumption failed") from None


_NAVIGATION_FIELDS: Final = (
    "_execution_handle",
    "_hit_seal",
    "_nonce",
    "_phase",
    "_principal_handle",
    "_request_handle",
    "_seal",
    "_snapshot_handle",
    "_tenant_handle",
)


def _navigation_material(value: VerifiedArchiveObsidianNavigation) -> bytes:
    return _canonical_json(
        {
            "execution_handle": value._execution_handle,
            "hit_seal": value._hit_seal.hex(),
            "nonce": value._nonce.hex(),
            "phase": value._phase.value,
            "principal_handle": value._principal_handle.hex(),
            "request_handle": value._request_handle.hex(),
            "snapshot_handle": value._snapshot_handle.hex(),
            "tenant_handle": value._tenant_handle.hex(),
        }
    )


def _verified_navigation_is_valid(value: object) -> bool:
    if type(value) is not VerifiedArchiveObsidianNavigation:
        return False
    navigation = cast(VerifiedArchiveObsidianNavigation, value)
    try:
        return bool(
            type(navigation._execution_handle) is str
            and _SHA256.fullmatch(navigation._execution_handle) is not None
            and type(navigation._phase) is ArchiveObsidianReadPhase
            and all(
                type(item) is bytes and len(item) == 32
                for item in (
                    navigation._hit_seal,
                    navigation._nonce,
                    navigation._principal_handle,
                    navigation._request_handle,
                    navigation._snapshot_handle,
                    navigation._tenant_handle,
                    navigation._seal,
                )
            )
            and hmac.compare_digest(
                navigation._seal,
                _mac(
                    b"friday/archive-obsidian-navigation/v2",
                    _navigation_material(navigation),
                ),
            )
        )
    except Exception:
        return False


def _validated_navigation_copy(value: object) -> VerifiedArchiveObsidianNavigation | None:
    if type(value) is not VerifiedArchiveObsidianNavigation:
        return None
    try:
        copied = cast(
            VerifiedArchiveObsidianNavigation,
            object.__new__(VerifiedArchiveObsidianNavigation),
        )
        for name in _NAVIGATION_FIELDS:
            object.__setattr__(copied, name, getattr(value, name))
        return copied if _verified_navigation_is_valid(copied) else None
    except Exception:
        return None


def _new_verified_navigation(
    *,
    hit: ArchiveObsidianHit,
    phase: ArchiveObsidianReadPhase,
) -> VerifiedArchiveObsidianNavigation:
    if not _hit_is_valid(hit) or type(phase) is not ArchiveObsidianReadPhase:
        raise _fail("verified archive Obsidian navigation is invalid")
    value = cast(
        VerifiedArchiveObsidianNavigation,
        object.__new__(VerifiedArchiveObsidianNavigation),
    )
    for name, item in (
        ("_execution_handle", hit._execution_handle),
        ("_hit_seal", hit._seal),
        ("_nonce", secrets.token_bytes(32)),
        ("_phase", phase),
        ("_principal_handle", hit._principal_handle),
        ("_request_handle", hit._request_handle),
        ("_seal", b"0" * 32),
        ("_snapshot_handle", hit._snapshot_handle),
        ("_tenant_handle", hit._tenant_handle),
    ):
        object.__setattr__(value, name, item)
    object.__setattr__(
        value,
        "_seal",
        _mac(b"friday/archive-obsidian-navigation/v2", _navigation_material(value)),
    )
    if not _verified_navigation_is_valid(value):
        raise _fail("verified archive Obsidian navigation is invalid")
    _register_carrier(
        nonce=value._nonce,
        kind=_NAVIGATION_CARRIER_KIND,
        seal=value._seal,
    )
    return value


@dataclass(frozen=True, slots=True, repr=False, init=False)
class VerifiedArchiveObsidianBody(_ProcessPrivate):
    """One-use exact body consumed only through a final callback."""

    _execution_handle: str
    _hit_seal: bytes
    _nonce: bytes
    _phase: ArchiveObsidianReadPhase
    _principal_handle: bytes
    _request_handle: bytes
    _seal: bytes
    _snapshot_handle: bytes
    _tenant_handle: bytes
    _text: str
    _text_sha256: bytes

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail("verified archive Obsidian bodies require exact-file authority")

    def __repr__(self) -> str:
        return "<VerifiedArchiveObsidianBody sealed private>"

    def consume_with(
        self,
        *,
        execution_binding: SearchExecutionBinding,
        tenant_id: str,
        principal_id: str,
        request: ArchiveSearchRequest,
        snapshot_discriminator: str,
        hit: ArchiveObsidianHit,
        phase: ArchiveObsidianReadPhase,
        consumer: ArchiveObsidianBodyConsumer[_ResultT],
    ) -> _ResultT:
        """Atomically consume once and invoke the final callback with immutable text."""

        try:
            carrier = _validated_body_copy(self)
            safe_hit = _validated_hit_copy(hit)
            if (
                carrier is None
                or safe_hit is None
                or not safe_hit.factual
                or type(phase) is not ArchiveObsidianReadPhase
                or phase is not carrier._phase
                or not callable(consumer)
                or not hmac.compare_digest(safe_hit._seal, carrier._hit_seal)
                or not _scope_matches(
                    tenant_handle=carrier._tenant_handle,
                    principal_handle=carrier._principal_handle,
                    request_handle=carrier._request_handle,
                    snapshot_handle=carrier._snapshot_handle,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    request=request,
                    snapshot_discriminator=snapshot_discriminator,
                )
                or not _execution_binding_attests(
                    execution_binding,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    request=request,
                    snapshot_discriminator=snapshot_discriminator,
                    lane=safe_hit.lane,
                )
                or not hmac.compare_digest(
                    execution_binding.opaque_handle,
                    carrier._execution_handle,
                )
                or not _claim_carrier(
                    nonce=carrier._nonce,
                    kind=_BODY_CARRIER_KIND,
                    seal=carrier._seal,
                )
            ):
                raise _fail("archive Obsidian body carrier is unavailable")
        except ArchiveObsidianStorageError:
            raise
        except Exception:
            raise _fail("archive Obsidian body carrier is unavailable") from None
        try:
            return consumer(carrier._text)
        except Exception:
            raise _fail("archive Obsidian body consumption failed") from None


_BODY_FIELDS: Final = (
    "_execution_handle",
    "_hit_seal",
    "_nonce",
    "_phase",
    "_principal_handle",
    "_request_handle",
    "_seal",
    "_snapshot_handle",
    "_tenant_handle",
    "_text",
    "_text_sha256",
)


def _body_material(value: VerifiedArchiveObsidianBody) -> bytes:
    return _canonical_json(
        {
            "execution_handle": value._execution_handle,
            "hit_seal": value._hit_seal.hex(),
            "nonce": value._nonce.hex(),
            "phase": value._phase.value,
            "principal_handle": value._principal_handle.hex(),
            "request_handle": value._request_handle.hex(),
            "snapshot_handle": value._snapshot_handle.hex(),
            "tenant_handle": value._tenant_handle.hex(),
            "text_sha256": value._text_sha256.hex(),
            "text_size": len(value._text.encode("utf-8", errors="strict")),
        }
    )


def _verified_body_is_valid(value: object) -> bool:
    if type(value) is not VerifiedArchiveObsidianBody:
        return False
    body = cast(VerifiedArchiveObsidianBody, value)
    try:
        encoded = body._text.encode("utf-8", errors="strict")
        return bool(
            type(body._text) is str
            and "\x00" not in body._text
            and len(encoded) <= MAX_ARCHIVE_OBSIDIAN_BODY_BYTES
            and type(body._execution_handle) is str
            and _SHA256.fullmatch(body._execution_handle) is not None
            and type(body._phase) is ArchiveObsidianReadPhase
            and all(
                type(item) is bytes and len(item) == 32
                for item in (
                    body._hit_seal,
                    body._nonce,
                    body._principal_handle,
                    body._request_handle,
                    body._snapshot_handle,
                    body._tenant_handle,
                    body._text_sha256,
                    body._seal,
                )
            )
            and hmac.compare_digest(hashlib.sha256(encoded).digest(), body._text_sha256)
            and hmac.compare_digest(
                body._seal,
                _mac(b"friday/archive-obsidian-body/v3", _body_material(body)),
            )
        )
    except Exception:
        return False


def _validated_body_copy(value: object) -> VerifiedArchiveObsidianBody | None:
    if type(value) is not VerifiedArchiveObsidianBody:
        return None
    try:
        copied = cast(VerifiedArchiveObsidianBody, object.__new__(VerifiedArchiveObsidianBody))
        for name in _BODY_FIELDS:
            object.__setattr__(copied, name, getattr(value, name))
        return copied if _verified_body_is_valid(copied) else None
    except Exception:
        return None


def _new_verified_body(
    *,
    hit: ArchiveObsidianHit,
    phase: ArchiveObsidianReadPhase,
    text: str,
) -> VerifiedArchiveObsidianBody:
    if not _hit_is_valid(hit) or type(phase) is not ArchiveObsidianReadPhase or type(text) is not str:
        raise _fail("verified archive Obsidian body is invalid")
    value = cast(VerifiedArchiveObsidianBody, object.__new__(VerifiedArchiveObsidianBody))
    for name, item in (
        ("_execution_handle", hit._execution_handle),
        ("_hit_seal", hit._seal),
        ("_nonce", secrets.token_bytes(32)),
        ("_phase", phase),
        ("_principal_handle", hit._principal_handle),
        ("_request_handle", hit._request_handle),
        ("_seal", b"0" * 32),
        ("_snapshot_handle", hit._snapshot_handle),
        ("_tenant_handle", hit._tenant_handle),
        ("_text", text),
        ("_text_sha256", hashlib.sha256(text.encode("utf-8", errors="strict")).digest()),
    ):
        object.__setattr__(value, name, item)
    object.__setattr__(
        value,
        "_seal",
        _mac(b"friday/archive-obsidian-body/v3", _body_material(value)),
    )
    if not _verified_body_is_valid(value):
        raise _fail("verified archive Obsidian body is invalid")
    _register_carrier(nonce=value._nonce, kind=_BODY_CARRIER_KIND, seal=value._seal)
    return value


def _current_factual_row(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    binding_id: str,
) -> dict[str, Any] | None:
    rows = _select_rows(
        conn,
        """WITH owned AS MATERIALIZED (
               SELECT id AS binding_id, vault_id, current_path, current_revision
                 FROM obsidian_note_bindings
                WHERE user_id=? AND id=? AND deleted_at IS NULL
                  AND EXISTS (SELECT 1 FROM users authority
                               WHERE authority.id=? AND authority.status='active')
           ), current_index AS MATERIALIZED (
               SELECT owned.*, idx.body_text, idx.source_size_bytes
                 FROM owned
                 JOIN obsidian_note_index idx
                   ON idx.user_id=?
                  AND idx.binding_id=owned.binding_id
                  AND idx.vault_id=owned.vault_id
                  AND idx.revision=owned.current_revision
                  AND idx.path=owned.current_path
                WHERE idx.state='ready' AND idx.body_coverage='complete'
           ) SELECT * FROM current_index""",
        (principal_id, binding_id, principal_id, principal_id),
    )
    if len(rows) > 1:
        raise _fail("archive Obsidian exact-file authority is invalid")
    return rows[0] if rows else None


def _current_navigation_row(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    request: ArchiveSearchRequest,
    binding_id: str,
) -> dict[str, Any] | None:
    if not _table_exists(conn, "obsidian_note_bindings"):
        return None
    owned = _owned_cte(
        index_available=_table_exists(conn, "obsidian_note_index"),
        lifecycle_clause=_lifecycle_clause(request, factual=False),
    )
    rows = _select_rows(
        conn,
        f"WITH {owned} SELECT * FROM owned WHERE binding_id=?",
        (principal_id, principal_id, binding_id),
    )
    if len(rows) > 1:
        raise _fail("archive Obsidian navigation authority is invalid")
    return rows[0] if rows else None


def _navigation_row_attests_hit(
    row: dict[str, Any],
    *,
    request: ArchiveSearchRequest,
    hit: ArchiveObsidianHit,
) -> bool:
    try:
        state = _row_state(row)
        if (
            state[0],
            state[1],
            state[2],
            _revision(row.get("current_revision")),
            state[3],
            state[4],
            state[5],
            state[6],
            state[7],
            state[8],
            state[9],
            state[10],
        ) != (
            hit.binding_id,
            hit.vault_id,
            hit.path,
            hit.current_revision,
            hit.lifecycle,
            hit.index_state,
            hit.index_revision_current,
            hit.index_path_current,
            hit.metadata_coverage,
            hit.body_coverage,
            hit.title,
            hit.aliases,
        ):
            return False
        match = _identity_match(_identity_values(state[2], state[9], state[10]), _needles(request))
        if match is None or match[1] is not hit.match_kind:
            return False
        if hit.lane is SearchLane.EXACT_IDENTITY:
            return hit.match_kind is ArchiveObsidianMatchKind.EXACT
        if hit.lane is SearchLane.APPROXIMATE_IDENTITY:
            return hit.match_kind in {
                ArchiveObsidianMatchKind.TYPO,
                ArchiveObsidianMatchKind.KEYBOARD_LAYOUT,
            }
        return hit.lane is SearchLane.CATALOG
    except Exception:
        return False


def verify_archive_obsidian_navigation_hit_in_transaction(
    conn: sqlite3.Connection,
    *,
    execution_binding: SearchExecutionBinding,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    hit: ArchiveObsidianHit,
    phase: ArchiveObsidianReadPhase,
) -> VerifiedArchiveObsidianNavigation:
    """Reauthorize one exact navigation hit at a model/publication boundary."""

    try:
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            raise _fail("archive Obsidian verification requires a caller-owned transaction")
        tenant = _tenant(tenant_id)
        principal = _principal(principal_id)
        safe_hit = _validated_hit_copy(hit)
        if (
            type(request) is not ArchiveSearchRequest
            or safe_hit is None
            or safe_hit.factual
            or safe_hit.lane
            not in {
                SearchLane.CATALOG,
                SearchLane.EXACT_IDENTITY,
                SearchLane.APPROXIMATE_IDENTITY,
            }
            or type(phase) is not ArchiveObsidianReadPhase
            or not _scope_matches(
                tenant_handle=safe_hit._tenant_handle,
                principal_handle=safe_hit._principal_handle,
                request_handle=safe_hit._request_handle,
                snapshot_handle=safe_hit._snapshot_handle,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
            )
            or not _execution_binding_attests(
                execution_binding,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                lane=safe_hit.lane,
            )
            or not hmac.compare_digest(
                execution_binding.opaque_handle,
                safe_hit._execution_handle,
            )
        ):
            raise _fail("archive Obsidian navigation authority is invalid")
        if (
            not _principal_is_active(conn, principal)
            or not _table_exists(conn, "obsidian_vaults")
            or not _principal_vault_is_ready(conn, principal)
        ):
            raise _fail("archive Obsidian navigation authority changed")
        row = _current_navigation_row(
            conn,
            principal_id=principal,
            request=request,
            binding_id=safe_hit.binding_id,
        )
        if row is None or not _navigation_row_attests_hit(row, request=request, hit=safe_hit):
            raise _fail("archive Obsidian navigation authority changed")
        return _new_verified_navigation(hit=safe_hit, phase=phase)
    except ArchiveObsidianStorageError:
        raise
    except Exception:
        raise _fail("archive Obsidian navigation verification failed") from None


def verify_archive_obsidian_factual_hit_in_transaction(
    conn: sqlite3.Connection,
    *,
    execution_binding: SearchExecutionBinding,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    hit: ArchiveObsidianHit,
    phase: ArchiveObsidianReadPhase,
    exact_file_reader: ArchiveObsidianExactFileReader,
) -> VerifiedArchiveObsidianBody:
    """Reauthorize one hit and SHA-read vault bytes at either publication boundary."""

    try:
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            raise _fail("archive Obsidian verification requires a caller-owned transaction")
        tenant = _tenant(tenant_id)
        principal = _principal(principal_id)
        safe_hit = _validated_hit_copy(hit)
        if (
            type(request) is not ArchiveSearchRequest
            or safe_hit is None
            or not safe_hit.factual
            or safe_hit.lane is not SearchLane.LEXICAL
            or type(phase) is not ArchiveObsidianReadPhase
            or not callable(exact_file_reader)
            or not _scope_matches(
                tenant_handle=safe_hit._tenant_handle,
                principal_handle=safe_hit._principal_handle,
                request_handle=safe_hit._request_handle,
                snapshot_handle=safe_hit._snapshot_handle,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
            )
            or not _execution_binding_attests(
                execution_binding,
                tenant_id=tenant,
                principal_id=principal,
                request=request,
                snapshot_discriminator=snapshot_discriminator,
                lane=safe_hit.lane,
            )
            or not hmac.compare_digest(
                execution_binding.opaque_handle,
                safe_hit._execution_handle,
            )
        ):
            raise _fail("archive Obsidian factual authority is invalid")
        if (
            not _principal_is_active(conn, principal)
            or not _table_exists(conn, "obsidian_vaults")
            or not _principal_vault_is_ready(conn, principal)
        ):
            raise _fail("archive Obsidian factual authority changed")
        first = _current_factual_row(
            conn,
            principal_id=principal,
            binding_id=safe_hit.binding_id,
        )
        if first is None:
            raise _fail("archive Obsidian factual authority changed")
        identity = (
            _binding_id(first.get("binding_id")),
            _vault_id(first.get("vault_id")),
            _path(first.get("current_path")),
            _revision(first.get("current_revision")),
        )
        if identity != (
            safe_hit.binding_id,
            safe_hit.vault_id,
            safe_hit.path,
            safe_hit.current_revision,
        ):
            raise _fail("archive Obsidian factual authority changed")
        indexed_body = first.get("body_text")
        if type(indexed_body) is not str or not hmac.compare_digest(
            hashlib.sha256(indexed_body.encode("utf-8", errors="strict")).digest(),
            hashlib.sha256(cast(str, safe_hit._indexed_body).encode("utf-8", errors="strict")).digest(),
        ):
            raise _fail("archive Obsidian factual projection changed")
        try:
            body = exact_file_reader(safe_hit.vault_id, safe_hit.path, safe_hit.current_revision)
        except Exception:
            raise _fail("archive Obsidian exact file read failed") from None
        if type(body) is not bytes or len(body) > MAX_ARCHIVE_OBSIDIAN_BODY_BYTES:
            raise _fail("archive Obsidian exact file read is invalid")
        if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), safe_hit.current_revision):
            raise _fail("archive Obsidian exact file revision changed")
        try:
            text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _fail("archive Obsidian exact file is not UTF-8 text") from None
        if "\x00" in text or text != indexed_body or len(body) != int(first["source_size_bytes"]):
            raise _fail("archive Obsidian exact file projection changed")
        second = _current_factual_row(
            conn,
            principal_id=principal,
            binding_id=safe_hit.binding_id,
        )
        if second != first:
            raise _fail("archive Obsidian factual authority changed")
        return _new_verified_body(hit=safe_hit, phase=phase, text=text)
    except ArchiveObsidianStorageError:
        raise
    except Exception:
        raise _fail("archive Obsidian exact-file verification failed") from None


__all__ = [
    "ArchiveObsidianBodyConsumer",
    "ArchiveObsidianCoverage",
    "ArchiveObsidianExactFileReader",
    "ArchiveObsidianHit",
    "ArchiveObsidianIndexState",
    "ArchiveObsidianLanePage",
    "ArchiveObsidianMatchKind",
    "ArchiveObsidianNavigationConsumer",
    "ArchiveObsidianReadPhase",
    "ArchiveObsidianStorageError",
    "ArchiveObsidianUnavailableReason",
    "MAX_ARCHIVE_OBSIDIAN_ALIASES",
    "MAX_ARCHIVE_OBSIDIAN_BODY_BYTES",
    "MAX_ARCHIVE_OBSIDIAN_IDENTITY_SCAN",
    "MAX_ARCHIVE_OBSIDIAN_LIVE_CARRIERS",
    "MAX_ARCHIVE_OBSIDIAN_RESULTS",
    "VerifiedArchiveObsidianBody",
    "VerifiedArchiveObsidianNavigation",
    "select_archive_obsidian_lane_in_transaction",
    "verify_archive_obsidian_factual_hit_in_transaction",
    "verify_archive_obsidian_navigation_hit_in_transaction",
]
