"""Process-private query plan for the archive document dense lane.

The embedding request is asynchronous and therefore happens before the caller
opens the archive snapshot.  This module carries only the bounded result of that
work into the synchronous, authority-owning archive facade.  The plan grants no
source authority: storage must reselect every candidate from its principal-
scoped source CTE and re-score the exact current vector before publication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
from dataclasses import dataclass
from typing import NoReturn, SupportsIndex, cast

_KEY = secrets.token_bytes(32)
_MAX_ID_BYTES = 200
_MAX_DIMENSIONS = 16_384
_MAX_CANDIDATES = 256


class ArchiveDensePlanError(RuntimeError):
    """Body-free rejection at the private dense-plan seam."""


def _fail() -> ArchiveDensePlanError:
    return ArchiveDensePlanError("archive dense query plan is unavailable")


def _text(value: object, *, maximum: int = _MAX_ID_BYTES) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail() from None
    if len(encoded) > maximum or any(ord(character) < 32 for character in value):
        raise _fail()
    return value


def _query_sha256(query: str) -> str:
    try:
        return hashlib.sha256(query.encode("utf-8", errors="strict")).hexdigest()
    except UnicodeEncodeError:
        raise _fail() from None


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveDenseCandidate:
    knowledge_object_id: str
    chunk_index: int

    def __post_init__(self) -> None:
        _text(self.knowledge_object_id)
        if type(self.chunk_index) is not int or not 0 <= self.chunk_index <= 1_000_000:
            raise _fail()

    def __repr__(self) -> str:
        return "ArchiveDenseCandidate(private=True)"

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive dense candidate cannot be serialized")


class ArchiveDenseQueryPlan:
    """Immutable non-serializable plan bound to one principal and query."""

    __slots__ = (
        "_candidates",
        "_dimensions",
        "_minimum_score",
        "_model_id",
        "_principal_id",
        "_query_sha256",
        "_scheme",
        "_seal",
        "_vector",
    )

    _candidates: tuple[ArchiveDenseCandidate, ...]
    _dimensions: int
    _minimum_score: float
    _model_id: str
    _principal_id: str
    _query_sha256: str
    _scheme: str
    _seal: bytes
    _vector: tuple[float, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail()

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive dense query plan is immutable")

    def __repr__(self) -> str:
        return "ArchiveDenseQueryPlan(private=True)"

    def __copy__(self) -> NoReturn:
        raise TypeError("archive dense query plan cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive dense query plan cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive dense query plan cannot be serialized")


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveDenseQueryProjection:
    """Validated values consumed only by the code-owned storage lane."""

    model_id: str
    dimensions: int
    chunk_scheme: str
    query_vector: tuple[float, ...]
    minimum_score: float
    candidates: tuple[ArchiveDenseCandidate, ...]
    identity_sha256: str

    def __repr__(self) -> str:
        return "ArchiveDenseQueryProjection(private=True)"

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive dense query projection cannot be serialized")


def _material(plan: ArchiveDenseQueryPlan) -> bytes:
    value = {
        "candidates": [[item.knowledge_object_id, item.chunk_index] for item in plan._candidates],
        "dimensions": plan._dimensions,
        "minimum_score": plan._minimum_score,
        "model_id": plan._model_id,
        "principal_id": plan._principal_id,
        "query_sha256": plan._query_sha256,
        "scheme": plan._scheme,
        "vector": plan._vector,
    }
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise _fail() from None


def _valid(plan: object) -> bool:
    try:
        if type(plan) is not ArchiveDenseQueryPlan:
            return False
        value = cast(ArchiveDenseQueryPlan, plan)
        return bool(
            type(value._principal_id) is str
            and type(value._query_sha256) is str
            and len(value._query_sha256) == 64
            and type(value._model_id) is str
            and type(value._scheme) is str
            and type(value._dimensions) is int
            and 1 <= value._dimensions <= _MAX_DIMENSIONS
            and type(value._vector) is tuple
            and len(value._vector) == value._dimensions
            and all(type(item) is float and math.isfinite(item) for item in value._vector)
            and math.isfinite(sum(item * item for item in value._vector))
            and sum(item * item for item in value._vector) > 0.0
            and type(value._minimum_score) is float
            and math.isfinite(value._minimum_score)
            and -1.0 <= value._minimum_score <= 1.0
            and type(value._candidates) is tuple
            and len(value._candidates) <= _MAX_CANDIDATES
            and all(type(item) is ArchiveDenseCandidate for item in value._candidates)
            and len(value._candidates) == len(set(value._candidates))
            and type(value._seal) is bytes
            and len(value._seal) == hashlib.sha256().digest_size
            and hmac.compare_digest(
                value._seal,
                hmac.new(
                    _KEY,
                    b"friday/archive-dense-plan/v1\0" + _material(value),
                    hashlib.sha256,
                ).digest(),
            )
        )
    except Exception:
        return False


def issue_archive_dense_query_plan(
    *,
    principal_id: str,
    query: str,
    model_id: str,
    chunk_scheme: str,
    query_vector: list[float],
    minimum_score: float,
    candidates: tuple[tuple[str, int], ...],
) -> ArchiveDenseQueryPlan:
    """Seal one bounded sidecar-backed recall result for the archive facade."""

    principal = _text(principal_id)
    model = _text(model_id)
    scheme = _text(chunk_scheme)
    if type(query) is not str or not query:
        raise _fail()
    if type(query_vector) is not list or not 1 <= len(query_vector) <= _MAX_DIMENSIONS:
        raise _fail()
    if type(candidates) is not tuple or any(
        type(item) is not tuple or len(item) != 2 for item in candidates
    ):
        raise _fail()
    try:
        vector = tuple(float(item) for item in query_vector)
        floor = float(minimum_score)
        selected = tuple(ArchiveDenseCandidate(item[0], item[1]) for item in candidates)
    except (IndexError, OverflowError, TypeError, ValueError):
        raise _fail() from None
    norm_squared = sum(item * item for item in vector)
    if (
        any(not math.isfinite(item) for item in vector)
        or not math.isfinite(norm_squared)
        or norm_squared <= 0.0
        or not math.isfinite(floor)
        or not -1.0 <= floor <= 1.0
        or len(selected) > _MAX_CANDIDATES
        or len(selected) != len(set(selected))
    ):
        raise _fail()
    value = object.__new__(ArchiveDenseQueryPlan)
    for name, item in (
        ("_principal_id", principal),
        ("_query_sha256", _query_sha256(query)),
        ("_model_id", model),
        ("_scheme", scheme),
        ("_dimensions", len(vector)),
        ("_vector", vector),
        ("_minimum_score", floor),
        ("_candidates", selected),
        ("_seal", b""),
    ):
        object.__setattr__(value, name, item)
    object.__setattr__(
        value,
        "_seal",
        hmac.new(
            _KEY,
            b"friday/archive-dense-plan/v1\0" + _material(value),
            hashlib.sha256,
        ).digest(),
    )
    if not _valid(value):
        raise _fail()
    return value


def project_archive_dense_query_plan(
    plan: object,
    *,
    principal_id: str,
    query: str,
) -> ArchiveDenseQueryProjection | None:
    """Return exact plan values only for the bound principal/query pair."""

    if not _valid(plan):
        return None
    value = cast(ArchiveDenseQueryPlan, plan)
    try:
        expected_principal = _text(principal_id).encode("utf-8", errors="strict")
        if not hmac.compare_digest(
            value._principal_id.encode("utf-8", errors="strict"),
            expected_principal,
        ) or not hmac.compare_digest(value._query_sha256, _query_sha256(query)):
            return None
        identity = hashlib.sha256(_material(value)).hexdigest()
        return ArchiveDenseQueryProjection(
            value._model_id,
            value._dimensions,
            value._scheme,
            value._vector,
            value._minimum_score,
            value._candidates,
            identity,
        )
    except Exception:
        return None


__all__ = [
    "ArchiveDensePlanError",
    "ArchiveDenseQueryPlan",
    "ArchiveDenseQueryProjection",
    "issue_archive_dense_query_plan",
    "project_archive_dense_query_plan",
]
