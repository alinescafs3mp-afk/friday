"""Hybrid personal retrieval with lexical, FTS, optional embeddings, graph, and feedback signals."""

from __future__ import annotations

import array
import heapq
import json
import logging
import math
import re
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import httpx

from jericho.config import JerichoSettings

try:  # optional acceleration (jericho[vectors]); pure-Python fallback below
    import numpy as _np
except ImportError:  # pragma: no cover - exercised only when numpy is absent
    _np = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from jericho.storage import JerichoStorage

LOGGER = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ][0-9a-zA-Zа-яёА-ЯЁ._+#-]*", re.UNICODE)
# Separators that carry meaning INSIDE a token but are punctuation at the end of one.
# `+` and `#` are excluded on purpose: C++ and C# end in them legitimately.
_TRAILING_PUNCTUATION = ".-"


def tokens_of(text: str) -> list[str]:
    """Tokenize the way every part of retrieval must agree to tokenize.

    ``_TOKEN_RE`` deliberately lets ``. _ + # -`` continue a token so that ``file.txt``,
    ``BRK.A`` and ``scale_factor`` survive as single units. The cost is that a token
    ending a sentence swallows the full stop, and then the same identifier written in a
    query and in a document is two different strings.

    That is not cosmetic. ``_identifier_coverage`` drops any candidate whose identifiers
    do not all appear in its text, so a document mentioning
    ``autovacuum_vacuum_scale_factor.`` was unreachable by a query for
    ``autovacuum_vacuum_scale_factor`` — FTS returned the hit and the blend discarded it
    as ``identifier_mismatch``.
    """

    return [token.rstrip(_TRAILING_PUNCTUATION) for token in _TOKEN_RE.findall(text or "")]


_RELATIONAL_QUERY_RE = re.compile(
    r"\b(?:связан\w*|завис\w*|участву\w*|работа\w*\s+над|относ\w*\s+к|"
    r"част\w*\s+(?:проекта|системы)|через\s+что|между\s+\w+\s+и\s+\w+|"
    r"related\s+to|depends?\s+on|works?\s+on|part\s+of|connected\s+to|between)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "with",
    "а",
    "без",
    "бы",
    "в",
    "во",
    "где",
    "да",
    "для",
    "до",
    "же",
    "за",
    "и",
    "из",
    "или",
    "как",
    "к",
    "ко",
    "ли",
    "мне",
    "мой",
    "моя",
    "мы",
    "на",
    "над",
    "не",
    "но",
    "о",
    "об",
    "он",
    "она",
    "они",
    "от",
    "по",
    "под",
    "про",
    "с",
    "со",
    "так",
    "там",
    "то",
    "у",
    "что",
    "это",
    "я",
}


def lexical_vector(text: str) -> dict[str, float]:
    """L2-normalized word and character-trigram vector with identifier preservation."""
    tokens = [token.casefold() for token in tokens_of(text)]
    tokens = [token for token in tokens if token not in _STOPWORDS]
    weights: dict[str, float] = {}
    for token in tokens:
        weights[f"w:{token}"] = weights.get(f"w:{token}", 0.0) + 1.5
        padded = f"#{token}#"
        for index in range(max(0, len(padded) - 2)):
            key = f"t:{padded[index : index + 3]}"
            weights[key] = weights.get(key, 0.0) + 0.35
    norm = math.sqrt(sum(value * value for value in weights.values()))
    return {key: value / norm for key, value in weights.items()} if norm else {}


def sparse_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def best_snippet(query: str, text: str, *, max_chars: int = 520) -> str:
    """Return the passage of ``text`` most relevant to ``query`` (query-aware
    excerpting) so a reader — the LLM or the answer verifier — sees the region
    that actually matched, not the document head. Pure lexical scoring over the
    shared tokenizer (no model, no embedding); falls back to the head when the
    text is short or nothing matches.
    """
    body = (text or "").strip()
    if len(body) <= max_chars:
        return body
    query_tokens = {
        token.casefold()
        for token in tokens_of(query)
        if len(token) > 1 and token.casefold() not in _STOPWORDS
    }
    if not query_tokens:
        return body[:max_chars].rstrip() + "…"
    lowered = body.casefold()
    occurrences: list[tuple[int, str]] = []
    for token in query_tokens:
        start = 0
        while True:
            found = lowered.find(token, start)
            if found < 0:
                break
            occurrences.append((found, token))
            start = found + len(token)
    if not occurrences:
        return body[:max_chars].rstrip() + "…"
    occurrences.sort()
    # Pick the max_chars window that covers the most DISTINCT query tokens.
    #
    # Two pointers over the already-sorted occurrences, with a multiset of the
    # tokens currently inside the window. The obvious version — rescanning
    # `occurrences` for every candidate start — is quadratic in the number of
    # matches, which is a function of DOCUMENT SIZE, not of query length: measured
    # on this machine at 0.31 s for 40 KB, 4.5 s for 162 KB and **115.7 s for
    # 812 KB**, all of it synchronous on the event loop, so one large document made
    # the whole backend unresponsive for two minutes. Same window chosen, linear.
    counts: Counter[str] = Counter()
    right = 0
    best_pos, best_distinct = occurrences[0][0], -1
    for left, (pos, _) in enumerate(occurrences):
        if left:
            # The window held [left-1, right); drop the element leaving it.
            leaving = occurrences[left - 1][1]
            counts[leaving] -= 1
            if not counts[leaving]:
                del counts[leaving]
        while right < len(occurrences) and occurrences[right][0] < pos + max_chars:
            counts[occurrences[right][1]] += 1
            right += 1
        if len(counts) > best_distinct:
            best_distinct = len(counts)
            best_pos = pos
    start = max(0, best_pos - 64)
    snippet = body[start : start + max_chars].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + max_chars < len(body) else ""
    return f"{prefix}{snippet}{suffix}"


def dense_cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


def reciprocal_rank_fusion(rankings: list[list[str]], *, k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, document_id in enumerate(ranking):
            scores[document_id] = scores.get(document_id, 0.0) + 1.0 / (k + position + 1)
    return scores


def knowledge_search_text(item: dict[str, Any]) -> str:
    """Canonical text used both for indexing and query-time embedding of an object."""
    return " ".join(
        str(item.get(field, "")) for field in ("title", "summary", "content", "tags_json", "knowledge_kind")
    )


# A boundary is only worth honouring past this fraction of the window; nearer the
# start it would trade a whole chunk for a stub.
# Mirrors the indexer's own ceilings (`_DOC_VECTOR_MAX_CHARS` / `_EMBED_REQUEST_MAX_CHARS`
# in jericho/workers): one candidate never exceeds what an embeddings service accepts,
# and one request stays inside the measured ~2800 chars/s throughput.
_POOL_TEXT_MAX_CHARS = 20_000
_POOL_REQUEST_MAX_CHARS = 40_000
# A raw cosine below this is noise, not evidence — the floor for the
# `insufficient_evidence` gate. MEASURED against the production model
# (qwen3-embedding-0.6b, dim 1024) at the operating point that matters, a short
# query against a document body: 56 query x unrelated-document pairs scored
# min 0.1032 / p50 0.2361 / p90 0.3255 / max 0.3878, while 8 query x own-document
# pairs scored min 0.4188 / p50 0.5197 / max 0.6196.
#
# The previous constant, 0.16, sat *below the median of the noise*: it admitted
# 48 of those 56 unrelated documents — 85.7% — as dense evidence. 0.35 clears
# noise p90 while leaving 0.07 of headroom under the weakest genuine match, which
# matters because this gate REMOVES results: erring low costs precision, erring
# high costs recall, and only one of those is recoverable by reading further down
# the list. Configurable because the number belongs to the model, not to Jericho.
_DENSE_EVIDENCE_MIN_DEFAULT = 0.35
_CHUNK_BOUNDARY_FLOOR = 0.5
_SENTENCE_END_RE = re.compile(r"[.!?…][»\"')\]]?\s")


def _chunk_boundary(text: str, start: int, end: int, max_chars: int) -> int:
    """The last natural boundary inside ``[start, end)``, else ``end``.

    Searched backwards from the window edge in descending order of how much meaning
    the break preserves: paragraph, sentence, line, word. A hard cut is the last
    resort — a base64 blob with no whitespace at all must still terminate.
    """
    floor = start + int(max_chars * _CHUNK_BOUNDARY_FLOOR)
    window = text[start:end]
    paragraph = window.rfind("\n\n")
    if paragraph >= 0 and start + paragraph + 2 > floor:
        return start + paragraph + 2
    sentence = None
    for candidate in _SENTENCE_END_RE.finditer(window):
        if start + candidate.end() > floor:
            sentence = candidate
    if sentence is not None:
        return start + sentence.end()
    for separator in ("\n", " "):
        found = window.rfind(separator)
        if found >= 0 and start + found + 1 > floor:
            return start + found + 1
    # Below the floor a word break still beats splitting mid-word.
    found = window.rfind(" ")
    return start + found + 1 if found > 0 else end


def _advance(text: str, position: int, limit: int) -> int:
    """Nudge an overlap start forward off the middle of a word."""
    if position <= 0 or position >= len(text) or text[position - 1].isspace():
        return position
    found = text.find(" ", position)
    return found + 1 if 0 <= found < limit else position


def chunk_spans(text: str, *, max_chars: int, overlap_chars: int, max_chunks: int) -> list[tuple[int, int]]:
    """Split ``text`` into overlapping ``[start, end)`` spans on natural boundaries.

    Spans are character offsets into ``text`` itself (not into a normalised copy), so
    the winning passage can be quoted back verbatim. Overlap means a fact shorter than
    ``overlap_chars`` always lands whole inside at least one span.
    """
    body = text or ""
    if max_chars <= 0 or not body:
        return []
    if len(body) <= max_chars:
        return [(0, len(body))]
    bounded_overlap = max(0, min(int(overlap_chars), max_chars // 2))
    limit = max(1, int(max_chunks))

    def _cut(window: int) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        start = 0
        while start < len(body):
            end = min(start + window, len(body))
            if end < len(body):
                end = _chunk_boundary(body, start, end, window)
            spans.append((start, end))
            if end >= len(body):
                break
            # ``start + 1`` guarantees forward progress whatever the overlap is.
            start = _advance(body, max(start + 1, end - bounded_overlap), end)
        return spans

    spans = _cut(max_chars)
    window = max_chars
    while len(spans) > limit and window < len(body):
        # Widen until it fits, instead of one pass followed by ``[:limit]``.
        #
        # The single pass was capped at ``max_chars * 4`` and sized by
        # ``ceil(len/limit)``, which ignores that ``_chunk_boundary`` snaps a span
        # back by up to half a window — so both bounds under-shoot, and whatever did
        # not fit was cut away in silence. Measured with the shipped defaults
        # (1200 / 200 / 63): a 490 KB document indexed **59% of itself**, and the
        # missing 41% was reachable only through the whole-object vector, which is
        # itself capped. Nothing downstream could tell a truncated document from a
        # short one.
        #
        # Doubling makes the window grow with the document — a 490 KB body settles
        # at ~9.6 K per passage, which is still well inside an embedding model's
        # context. Very large documents do end up with coarse passages, and coarse
        # passages beat absent ones: the whole-object vector remains the floor, so
        # chunking can still only add recall.
        estimate = math.ceil(len(body) / limit) + bounded_overlap
        # The estimate first (it is right whenever boundary snapping is mild, and
        # keeps passages as fine as they used to be), then gentle geometric growth
        # for the cases where snapping makes it optimistic.
        window = min(len(body), estimate if estimate > window else math.ceil(window * 1.5))
        spans = _cut(window)
    if len(spans) > limit:
        spans = spans[:limit]
        # Unreachable while the loop above can widen, but if it ever is reached the
        # tail is content, not slack: carry the last span to the end of the body.
        spans[-1] = (spans[-1][0], len(body))
    # A trailing stub scores erratically high on a short match; fold it backwards.
    stub = max(120, max_chars // 6)
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] < stub:
        spans[-2] = (spans[-2][0], spans[-1][1])
        spans.pop()
    return spans


def knowledge_chunk_units(
    item: dict[str, Any], *, max_chars: int, overlap_chars: int, max_chunks: int
) -> list[tuple[int, int, str]]:
    """``(start, end, text_to_embed)`` per passage, or ``[]`` when no chunking applies.

    Returning ``[]`` for a short object is the load-bearing case: it keeps such an
    object represented by exactly one whole-object vector computed from byte-identical
    text, so enabling chunking changes nothing for the bulk of a personal corpus.
    Each passage is prefixed with the object's header — a paragraph stripped of what
    document it belongs to loses most of its topical signal.
    """
    if max_chars <= 0 or len(knowledge_search_text(item)) <= max_chars:
        return []
    content = str(item.get("content") or "")
    spans = chunk_spans(content, max_chars=max_chars, overlap_chars=overlap_chars, max_chunks=max_chunks)
    if len(spans) <= 1:
        # One passage is what the whole-object vector already represents.
        return []
    header = " ".join(
        part
        for part in (
            str(item.get("title") or ""),
            str(item.get("summary") or ""),
            str(item.get("knowledge_kind") or ""),
        )
        if part.strip()
    )[: max(0, max_chars // 4)]
    return [(start, end, f"{header}\n\n{content[start:end]}") for start, end in spans]


def chunk_scheme(settings: JerichoSettings) -> str:
    """Fingerprint of the chunking configuration a stored row was built with.

    ``''`` means "not chunked" — exactly what every pre-0.41 row already stores, so
    turning chunking off re-indexes nothing that was never chunked.
    """
    if settings.embeddings_chunk_chars <= 0:
        return ""
    return (
        f"v1:{settings.embeddings_chunk_chars}"
        f":{settings.embeddings_chunk_overlap_chars}"
        f":{settings.embeddings_chunk_max_per_object}"
    )


def pack_vector(vector: list[float]) -> bytes:
    """Pack an embedding as little-agnostic float32 bytes for BLOB storage."""
    return array.array("f", (float(value) for value in vector)).tobytes()


def unpack_vector(blob: bytes) -> array.array:
    """Inverse of :func:`pack_vector` — returns a float32 array of the stored vector."""
    values = array.array("f")
    values.frombytes(blob)
    return values


def _dense_scores_python(
    query_vector: list[float], stored: list[tuple[str, bytes]], query_dim: int
) -> list[tuple[float, str]]:
    """Cosine-score every stored vector against the query in pure Python (fallback)."""
    query_norm = math.sqrt(sum(value * value for value in query_vector))
    if query_norm == 0.0:
        return []
    scored: list[tuple[float, str]] = []
    for document_id, blob in stored:
        vector = unpack_vector(blob)
        if len(vector) != query_dim:
            continue
        dot = 0.0
        norm = 0.0
        for query_value, value in zip(query_vector, vector, strict=False):
            dot += query_value * value
            norm += value * value
        if norm <= 0.0:
            continue
        scored.append((dot / (query_norm * math.sqrt(norm)), document_id))
    return scored


def _dense_scores_numpy(
    query_vector: list[float], stored: list[tuple[str, bytes]], query_dim: int
) -> list[tuple[float, str]]:
    """Vectorised cosine over all stored vectors: the per-element Python loop
    collapses into one matmul (BLAS), turning ~N*dim scalar ops into a matrix op."""
    expected_bytes = query_dim * 4  # pack_vector stores float32 (4 bytes/value)
    ids: list[str] = []
    buffers: list[bytes] = []
    for document_id, blob in stored:
        if len(blob) == expected_bytes:  # skip dimension-mismatched vectors, as the loop did
            ids.append(document_id)
            buffers.append(blob)
    if not ids:
        return []
    # float32 (the stored precision) keeps this zero-copy and ~15x over the loop;
    # cosine values differ from the float64 loop only ~1e-7, well below any ranking
    # threshold. einsum computes per-row squared norms without an N*dim temporary.
    matrix = _np.frombuffer(b"".join(buffers), dtype=_np.float32).reshape(len(ids), query_dim)
    query = _np.asarray(query_vector, dtype=_np.float32)
    query_norm = math.sqrt(float(query @ query))
    if query_norm == 0.0:
        return []
    norms = _np.sqrt(_np.einsum("ij,ij->i", matrix, matrix))
    dots = matrix @ query
    valid = norms > 0.0  # drop zero-norm vectors, matching the Python path
    scores = dots[valid] / (norms[valid] * query_norm)
    valid_ids = [ids[index] for index in range(len(ids)) if valid[index]]
    return list(zip((float(score) for score in scores), valid_ids, strict=True))


_CHUNK_CORROBORATION_K = 3

# Signals whose weight can be zeroed WITHOUT changing anything else, so switching one
# off and re-measuring the gold set answers "does this weight earn its place?".
ABLATABLE_SIGNALS: tuple[str, ...] = (
    "feedback",
    "usage",
    "kind_alignment",
    "fts_bonus",
    "noise_penalty",
)
# Signals that also decide candidate ADMISSION, feed the RRF fusion, or act as an
# exclusion gate. Zeroing their weight is NOT the same as removing them, so an
# ablation number for these would be confidently wrong — they are reported as
# not measured, with the reason, instead.
ENTANGLED_SIGNALS: dict[str, str] = {
    "lexical": "feeds RRF and the insufficient_evidence gate",
    "field": "feeds the insufficient_evidence gate",
    "embedding": "decides candidate admission, feeds RRF and the evidence gate",
    "graph": "expands the candidate pool and feeds the evidence gate",
    "rrf": "a fusion of the other rankings, not a weight of its own",
    "identifier_coverage": "drives the identifier_mismatch exclusion, not just a penalty",
    "importance": "also a multiplicative lifecycle/quality input",
    "quality": "also a multiplicative quality_factor input",
    "promotion": "also a multiplicative quality_factor input",
}


def _trim_to_whole_objects(rows: list[tuple[str, bytes]], budget: int) -> list[tuple[str, bytes]]:
    """Cut a chunk-row scan on an OBJECT boundary at or below ``budget``.

    Rows arrive grouped by object (the query orders by object then chunk index). An
    object is either scanned completely or not at all, so ``scanned == indexed`` for
    every object that contributes a score — otherwise a half-scanned document both
    hides its answering passage and escapes the corroboration discount.
    """
    if budget <= 0 or len(rows) <= budget:
        return rows
    kept = 0
    current = rows[0][0].rpartition("#")[0]
    for index, (key, _) in enumerate((*rows, ("\x00#0", b""))):
        document_id = key.rpartition("#")[0]
        if document_id == current:
            continue
        # ``current`` ended at ``index``. Take it whole if it fits — or if it is the
        # first one, since dropping every row would be worse than a single overrun.
        if index <= budget or kept == 0:
            kept = index
        else:
            break
        current = document_id
    return rows[:kept]


def aggregate_chunk_scores(
    scored: Sequence[tuple[float, str]], *, blend: float
) -> tuple[dict[str, float], dict[str, tuple[int, int]]]:
    """Collapse per-chunk cosines into one score per Knowledge Object.

    ``(1 - blend) * best + blend * mean(top-3)``. Max-over-passages is what makes a
    single relevant paragraph of a long import recallable at all; the top-k mean is a
    corroboration term, so one lucky fragment does not outrank a document that is
    genuinely about the query — the expected maximum of N samples grows with N, and
    against a fixed evidence gate that bias would convert directly into false rescues.
    The result is a convex combination of cosines, so it stays on exactly the scale
    the 0.17 blend weight and the 0.16 evidence gate were calibrated for.

    Returns ``(score_by_object, {object: (best_chunk_index, chunks_scored)})``.
    """
    weight = max(0.0, min(1.0, float(blend)))
    by_object: dict[str, list[tuple[float, int]]] = {}
    for score, key in scored:
        document_id, _, raw_index = key.rpartition("#")
        if not document_id:
            # Not a chunk key; ignore rather than mis-attribute it to an object.
            continue
        try:
            index = int(raw_index)
        except ValueError:
            continue
        by_object.setdefault(document_id, []).append((score, index))
    aggregated: dict[str, float] = {}
    provenance: dict[str, tuple[int, int]] = {}
    for document_id, entries in by_object.items():
        entries.sort(key=lambda pair: pair[0], reverse=True)
        best, best_index = entries[0]
        top = [score for score, _ in entries[:_CHUNK_CORROBORATION_K]]
        aggregated[document_id] = (1.0 - weight) * best + weight * (sum(top) / len(top))
        provenance[document_id] = (best_index, len(entries))
    return aggregated, provenance


def dense_scores(
    query_vector: list[float], stored: list[tuple[str, bytes]], query_dim: int
) -> list[tuple[float, str]]:
    """Cosine-score persisted vectors against the query, using numpy when available."""
    if _np is not None:
        return _dense_scores_numpy(query_vector, stored, query_dim)
    return _dense_scores_python(query_vector, stored, query_dim)


class EmbeddingBackend:
    def __init__(self, settings: JerichoSettings) -> None:
        self.settings = settings

    @property
    def remote_enabled(self) -> bool:
        return bool(
            self.settings.embeddings_enabled
            and self.settings.embeddings_base_url
            and self.settings.embeddings_model
        )

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.remote_enabled or not texts:
            return None
        try:
            timeout = httpx.Timeout(min(self.settings.llm_timeout_sec, 60.0), connect=10.0)
            key = self.settings.embeddings_api_key
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, headers=headers) as client:
                response = await client.post(
                    f"{self.settings.embeddings_base_url}/embeddings",
                    json={"model": self.settings.embeddings_model, "input": texts},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception:
            # Chunking multiplies how often this path runs (more inputs, bigger
            # requests), so the failure must stop being completely silent.
            LOGGER.warning("embeddings backend request failed", exc_info=True)
            return None
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            return None
        output: list[list[float]] = []
        for item in items:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list):
                return None
            output.append([float(value) for value in vector])
        return output


async def semantic_similarity_order(
    backend: EmbeddingBackend,
    query: str,
    documents: list[str],
) -> list[int]:
    if not documents:
        return []
    vectors = await backend.embed([query, *documents])
    if vectors and len(vectors) == len(documents) + 1:
        scores = [dense_cosine(vectors[0], vector) for vector in vectors[1:]]
    else:
        query_vector = lexical_vector(query)
        scores = [sparse_cosine(query_vector, lexical_vector(document)) for document in documents]
    return sorted(range(len(documents)), key=lambda index: scores[index], reverse=True)


class HybridSearcher:
    """Rank personal knowledge using text, graph, quality, lifecycle, and feedback."""

    def __init__(
        self,
        storage: JerichoStorage,
        embeddings: EmbeddingBackend | None = None,
        *,
        graph_max_depth: int = 2,
        chunk_recall: bool = True,
        record_usage: bool = True,
        ablate: Sequence[str] | None = None,
        pool_max: int = 400,
        dense_evidence_min: float = _DENSE_EVIDENCE_MIN_DEFAULT,
    ) -> None:
        self.storage = storage
        self.embeddings = embeddings
        self._graph_max_depth = max(1, int(graph_max_depth))
        # Ceiling on the fuzzy recall pool. Above it the lexical channel sees only
        # the most important/recent slice of the corpus, and the answer has to say so.
        self._pool_max = max(1, int(pool_max))
        # Cosine below which a dense score is not evidence. Model-dependent, so it is
        # configurable and its default is measured rather than chosen — see
        # `JERICHO_RETRIEVAL_DENSE_EVIDENCE_MIN` in docs/ARCHITECTURE.md §7.
        self._dense_evidence_min = float(dense_evidence_min)
        # Off only for the A/B harness, which must measure the same corpus twice.
        self._chunk_recall = bool(chunk_recall)
        # Off for advisory harnesses: they must not write the counter that
        # ``usage_signal`` reads back into the very blend they are measuring.
        self._record_usage = bool(record_usage)
        # Names from ABLATABLE_SIGNALS whose weight is forced to zero for this
        # instance. Measurement only: a name can silence a weight, never raise it.
        self._ablate = frozenset(ablate or ())

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        include_entities: bool = True,
        kg: Any = None,
        explain: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        clean_query = " ".join((query or "").split()).strip()
        if not clean_query:
            return {"query": query, "results": [], "count": 0, "entity_matches": []}

        fts_candidates = self.storage.search_knowledge(user_id, clean_query, limit=limit * 5)
        # Fuzzy matching needs a bounded recall pool even when FTS finds no exact token.
        pool_limit = min(self._pool_max, max(limit * 10, 100))
        recent_pool = self.storage.list_knowledge_objects(user_id, limit=pool_limit)
        candidate_map = {item["id"]: item for item in [*fts_candidates, *recent_pool]}
        candidates = list(candidate_map.values())

        fts_ranking = [item["id"] for item in fts_candidates]
        # Shared by both `_lexical_rank` passes and by the snippet pass below, so a
        # candidate's body is tokenized once per request rather than once per use.
        lexical_cache: dict[str, dict[str, float]] = {}
        lexical_ranking, lexical_scores = self._lexical_rank(candidates, clean_query, cache=lexical_cache)
        rankings = [fts_ranking, lexical_ranking]
        embedding_scores: dict[str, float] = {}
        dense_meta: dict[str, Any] = {}
        chunk_provenance: dict[str, tuple[int, int]] = {}
        if self.embeddings and self.embeddings.remote_enabled:
            embedding_scores = await self._dense_recall(user_id, clean_query, candidate_map, meta=dense_meta)
            chunk_provenance = dict(dense_meta.get("chunk_provenance") or {})
            if embedding_scores:
                candidates = list(candidate_map.values())
                rankings.append(
                    [
                        document_id
                        for document_id, _ in sorted(
                            embedding_scores.items(), key=lambda pair: pair[1], reverse=True
                        )
                    ]
                )

        # The graph is a retrieval signal, not a decorative side channel.  Seed it with the best
        # lexical/FTS matches and bring linked knowledge into the pool.  Explicitly relational
        # questions may use two conservative hops; ordinary lookup stays at one to limit noise.
        graph_context: dict[str, Any] = {
            "nodes": [],
            "relations": [],
            "knowledge_candidates": [],
        }
        graph_scores: dict[str, float] = {}
        graph_evidence: dict[str, list[dict[str, Any]]] = {}
        graph_depth = self._graph_max_depth if _RELATIONAL_QUERY_RE.search(clean_query) else 1
        graph_evidence_threshold = 0.12 if graph_depth >= 2 else 0.20
        if kg:
            seed_ids = list(dict.fromkeys([*fts_ranking[:8], *lexical_ranking[:8]]))
            try:
                graph_context = kg.context_for_query(
                    user_id,
                    clean_query,
                    seed_knowledge_ids=seed_ids,
                    entity_limit=8,
                    depth=graph_depth,
                    knowledge_limit=max(limit * 6, 50),
                )
                for candidate in graph_context.get("knowledge_candidates", []):
                    document_id = str(candidate.get("knowledge_object_id") or "")
                    if not document_id:
                        continue
                    item = candidate_map.get(document_id) or self.storage.get_knowledge_object(
                        document_id,
                        user_id,
                    )
                    if not item or item.get("deleted_at"):
                        continue
                    candidate_map[document_id] = item
                    graph_scores[document_id] = max(
                        graph_scores.get(document_id, 0.0),
                        float(candidate.get("score", 0.0)),
                    )
                    graph_evidence[document_id] = list(candidate.get("evidence") or [])
            except Exception:
                # Search must remain available even if graph enrichment encounters bad legacy data.
                graph_context = {"nodes": [], "relations": [], "knowledge_candidates": []}

        # Re-rank after graph expansion so newly discovered records receive lexical evidence too.
        candidates = list(candidate_map.values())
        lexical_ranking, lexical_scores = self._lexical_rank(candidates, clean_query, cache=lexical_cache)
        rankings[1] = lexical_ranking
        if graph_scores:
            rankings.append(
                [
                    document_id
                    for document_id, _ in sorted(graph_scores.items(), key=lambda item: item[1], reverse=True)
                ]
            )

        # A match in a concise, curated field is more trustworthy than the same token buried in a
        # long body. Entity names are loaded in one batch so this remains cheap on a local SQLite KB.
        entity_links = self._entity_links_by_document(user_id, list(candidate_map))
        field_components: dict[str, dict[str, float]] = {}
        field_scores: dict[str, float] = {}
        identifier_coverage: dict[str, float] = {}
        query_identifiers = self._query_identifiers(clean_query)
        query_lexical_vector = lexical_vector(clean_query)
        for document_id, item in candidate_map.items():
            names = [str(entity.get("name") or "") for entity in entity_links.get(document_id, [])]
            fields = self._field_scores(item, clean_query, names, query_vector=query_lexical_vector)
            field_components[document_id] = fields
            field_scores[document_id] = min(
                1.0,
                fields["title"] * 0.32
                + fields["summary"] * 0.14
                + fields["tags"] * 0.20
                + fields["entities"] * 0.24
                + fields["exact_phrase"] * 0.18,
            )
            identifier_coverage[document_id] = self._identifier_coverage(item, query_identifiers, names)
        field_ranking = [
            document_id
            for document_id, score in sorted(field_scores.items(), key=lambda pair: pair[1], reverse=True)
            if score >= 0.035
        ]
        if field_ranking:
            rankings.append(field_ranking)

        rrf = reciprocal_rank_fusion([ranking for ranking in rankings if ranking], k=45)
        feedback = self._feedback_scores(user_id, list(candidate_map))
        usage = self.storage.get_knowledge_usage(user_id, list(candidate_map))
        # Ablation seam: an advisory harness zeroes ONE weight and re-measures the gold
        # set, which is what turns a hand-tuned constant into a measured one. Lifted
        # out of the loop so the blend expression below stays a single readable line
        # per signal, and so the default path is the literal constant it always was.
        off = self._ablate
        w_feedback = 0.0 if "feedback" in off else 0.05
        w_usage = 0.0 if "usage" in off else 0.028
        w_kind = 0.0 if "kind_alignment" in off else 0.035
        w_fts_bonus = 0.0 if "fts_bonus" in off else 0.018
        w_noise = 0.0 if "noise_penalty" in off else 1.0
        final_scores: dict[str, float] = {}
        components: dict[str, dict[str, float]] = {}
        for document_id, item in candidate_map.items():
            lifecycle_factor = {
                "active": 1.0,
                "archived": 0.72,
                "deprecated": 0.36,
            }.get(str(item.get("lifecycle_stage", "active")), 0.25)
            importance = self._bounded(item.get("importance"), 0.5)
            quality = self._bounded(item.get("quality_score"), 0.5)
            promotion = self._bounded(item.get("promotion_score"), 0.5)
            lexical = max(0.0, lexical_scores.get(document_id, 0.0))
            embedding = max(0.0, embedding_scores.get(document_id, 0.0))
            graph = max(0.0, graph_scores.get(document_id, 0.0))
            field = max(0.0, field_scores.get(document_id, 0.0))
            identifiers = identifier_coverage.get(document_id, 1.0)
            user_feedback = max(-1.0, min(1.0, feedback.get(document_id, 0.0)))
            usage_row = usage.get(document_id, {})
            retrieval_count = max(0, int(usage_row.get("retrieval_count") or 0))
            answer_count = max(0, int(usage_row.get("answer_count") or 0))
            positive_count = max(0, int(usage_row.get("positive_feedback_count") or 0))
            negative_count = max(0, int(usage_row.get("negative_feedback_count") or 0))
            # Usage is a weak tie-breaker, not popularity lock-in.  Logarithmic
            # saturation and a low cap prevent old frequently seen notes from
            # drowning out a precise new fact.
            usage_signal = min(
                1.0,
                math.log1p(retrieval_count) * 0.08
                + math.log1p(answer_count) * 0.18
                + max(-0.4, min(0.4, (positive_count - negative_count) * 0.08)),
            )
            kind_alignment = self._kind_alignment(clean_query, str(item.get("knowledge_kind", "note")))
            noise_penalty = self._noise_penalty(item)
            identifier_penalty = 0.22 if query_identifiers and identifiers < 1.0 else 0.0
            base = (
                rrf.get(document_id, 0.0)
                + lexical * 0.19
                + field * 0.17
                + embedding * 0.17
                + graph * 0.16
                + importance * 0.035
                + quality * 0.045
                + promotion * 0.03
                + user_feedback * w_feedback
                + usage_signal * w_usage
                + kind_alignment * w_kind
                + (w_fts_bonus if document_id in fts_ranking else 0.0)
                - noise_penalty * w_noise
                - identifier_penalty
            )
            quality_factor = 0.42 + quality * 0.38 + promotion * 0.20
            final_scores[document_id] = max(0.0, base * lifecycle_factor * quality_factor)
            components[document_id] = {
                "rrf": round(rrf.get(document_id, 0.0), 6),
                "lexical": round(lexical, 6),
                "field": round(field, 6),
                "title_match": round(field_components[document_id]["title"], 6),
                "summary_match": round(field_components[document_id]["summary"], 6),
                "tag_match": round(field_components[document_id]["tags"], 6),
                "entity_match": round(field_components[document_id]["entities"], 6),
                "exact_phrase": round(field_components[document_id]["exact_phrase"], 6),
                "identifier_coverage": round(identifiers, 6),
                "embedding": round(embedding, 6),
                # -1 = the whole-object vector carried it, not a passage.
                "embedding_chunk": chunk_provenance.get(document_id, (-1, 0))[0],
                "embedding_chunks": chunk_provenance.get(document_id, (-1, 0))[1],
                "graph": round(graph, 6),
                "importance": round(importance, 6),
                "quality": round(quality, 6),
                "promotion": round(promotion, 6),
                "feedback": round(user_feedback, 6),
                "usage": round(usage_signal, 6),
                "kind_alignment": round(kind_alignment, 6),
                "noise_penalty": round(noise_penalty, 6),
                "lifecycle_factor": round(lifecycle_factor, 6),
            }

        def _exclusion_reason(
            document_id: str,
            item: dict[str, Any],
            lex: float,
            fld: float,
            emb: float,
            grp: float,
        ) -> str | None:
            """Why a scored candidate does not make the result set — centralised so
            the explain-trace reports the exact same reasons the ranker applies."""
            # Exact identifiers are discrete evidence, not fuzzy language: a query
            # mentioning BRK.A must not be satisfied by a nearby BRK.B record.
            if query_identifiers and identifier_coverage.get(document_id, 0.0) < 1.0:
                return "identifier_mismatch"
            # Recent-pool records need real evidence; graph expansion or a strong
            # curated-field match can satisfy it without repeating body terms.
            if (
                document_id not in fts_ranking
                and lex < 0.075
                and fld < 0.12
                and emb < self._dense_evidence_min
                and grp < graph_evidence_threshold
            ):
                return "insufficient_evidence"
            if (
                item.get("lifecycle_stage") == "deprecated"
                and document_id not in fts_ranking
                and lex < 0.25
                and grp < 0.45
            ):
                return "deprecated_weak"
            return None

        ordered = sorted(candidate_map, key=lambda document_id: final_scores[document_id], reverse=True)
        results: list[dict[str, Any]] = []
        for document_id in ordered:
            if len(results) >= limit:
                break
            item = self.storage.get_knowledge_object(document_id, user_id)
            if not item or item.get("deleted_at"):
                continue
            lexical_score = lexical_scores.get(document_id, 0.0)
            graph_score = graph_scores.get(document_id, 0.0)
            embedding_score = embedding_scores.get(document_id, 0.0)
            field_score = field_scores.get(document_id, 0.0)
            if _exclusion_reason(document_id, item, lexical_score, field_score, embedding_score, graph_score):
                continue
            item["_score"] = round(final_scores[document_id], 6)
            item["_lexical_score"] = round(lexical_score, 6)
            item["_field_score"] = round(field_score, 6)
            item["_field_matches"] = field_components[document_id]
            item["_embedding_score"] = round(embedding_score, 6)
            if document_id in chunk_provenance:
                item["_embedding_chunk"] = chunk_provenance[document_id][0]
            item["_graph_score"] = round(graph_score, 6)
            item["_feedback_score"] = round(feedback.get(document_id, 0.0), 6)
            item["_score_components"] = components[document_id]
            if graph_evidence.get(document_id):
                item["_graph_evidence"] = graph_evidence[document_id]
            entities = entity_links.get(document_id, [])
            if entities:
                item["_entities"] = entities
            results.append(item)

        # Resolve the winning passage's offsets so the answer can quote what actually
        # matched. Without this, a document recalled semantically at section 7 would
        # still be shown to the LLM through a LEXICALLY chosen window near its head —
        # precisely the case passage-level recall creates more of.
        if chunk_provenance and self.embeddings is not None:
            wanted = [
                (str(item["id"]), int(item["_embedding_chunk"]))
                for item in results
                if item.get("_embedding_chunk") is not None
                and int(item["_embedding_chunk"]) >= 0
                # Only when dense recall is the REASON the object is here. A hit that
                # also matched lexically must keep excerpting over the whole body, or
                # the excerpt could drop the very phrase the user searched for.
                and item["id"] not in fts_ranking
            ]
            if wanted:
                with suppress(Exception):
                    spans = self.storage.get_chunk_spans(
                        user_id, self.embeddings.settings.embeddings_model, wanted
                    )
                    for item in results:
                        key = (str(item["id"]), int(item.get("_embedding_chunk", -1)))
                        if key in spans:
                            item["_embedding_chunk_span"] = list(spans[key])

        # Ranking remains available if a read-only/locked deployment cannot
        # persist this optional, best-effort usage signal. An advisory harness turns
        # the write off entirely: ``usage_signal`` reads this counter back into the
        # blend, so measuring the corpus would otherwise change it.
        if self._record_usage:
            with suppress(Exception):
                top_score = float(results[0].get("_score", 0.0) or 0.0) if results else 0.0
                usage_floor = max(0.015, top_score * 0.32)
                retrieved_ids = [
                    str(item["id"])
                    for item in results[:5]
                    if item.get("id") and float(item.get("_score", 0.0) or 0.0) >= usage_floor
                ]
                self.storage.record_knowledge_usage(
                    user_id,
                    retrieved_ids,
                    retrieved=True,
                )

        if include_entities and kg:
            entity_matches = list(graph_context.get("nodes", []))[:5]
            if not entity_matches:
                entity_matches = kg.search_entities(user_id, clean_query, limit=5)
        else:
            entity_matches = []
        strategy: dict[str, Any] = {
            "fts": True,
            "lexical": True,
            "embeddings": bool(embedding_scores),
            "feedback": True,
            "graph": bool(kg),
        }
        if chunk_provenance:
            # At least one object was carried by a passage rather than its whole-object
            # vector — the visible signature of passage-level recall doing work.
            strategy["embeddings_chunked"] = True
        if dense_meta.get("dense_chunks_capped"):
            strategy["embeddings_chunks_capped"] = True
        if dense_meta.get("dense_capped"):
            # Dense recall scored only the newest N vectors — latency degrades
            # visibly (in the explain-trace) rather than silently on a big corpus.
            strategy["embeddings_capped"] = True
        if len(recent_pool) >= pool_limit:
            # The pool came back full, so the corpus may be larger than what the
            # lexical channel actually looked at. The count is only paid here, on a
            # saturated pool: an empty result over 8000 objects and an empty result
            # over 40 mean opposite things, and until now they printed the same.
            corpus_size = self.storage.count_knowledge_objects(user_id)
            if corpus_size > pool_limit:
                strategy["lexical_pool_capped"] = True
                strategy["lexical_pool_scanned"] = pool_limit
                strategy["corpus_size"] = corpus_size
        response: dict[str, Any] = {
            "query": clean_query,
            "results": results,
            "count": len(results),
            "entity_matches": entity_matches,
            "strategy": strategy,
        }
        if explain:
            # Every candidate was already scored; expose the ranked set (returned,
            # below-limit, and discarded-with-reason) plus each one's signal
            # breakdown so an admin can see WHY the ranking came out this way.
            returned_ids = {item["id"] for item in results}
            trace: list[dict[str, Any]] = []
            rank = 0
            trace_cap = max(limit * 3, 30)
            for document_id in ordered:
                item_meta = candidate_map.get(document_id) or {}
                reason = (
                    "deleted"
                    if item_meta.get("deleted_at")
                    else _exclusion_reason(
                        document_id,
                        item_meta,
                        lexical_scores.get(document_id, 0.0),
                        field_scores.get(document_id, 0.0),
                        embedding_scores.get(document_id, 0.0),
                        graph_scores.get(document_id, 0.0),
                    )
                )
                if reason:
                    status: str = "discarded"
                    entry_rank: int | None = None
                elif document_id in returned_ids:
                    status, entry_rank = "returned", rank
                    rank += 1
                else:
                    status, entry_rank = "below_limit", None
                    rank += 1
                trace.append(
                    {
                        "id": document_id,
                        "title": str(item_meta.get("title") or "Без названия")[:160],
                        "knowledge_kind": item_meta.get("knowledge_kind"),
                        "lifecycle_stage": item_meta.get("lifecycle_stage"),
                        "score": round(final_scores.get(document_id, 0.0), 6),
                        "status": status,
                        "rank": entry_rank,
                        "reason": reason,
                        "components": components.get(document_id, {}),
                    }
                )
                if len(trace) >= trace_cap:
                    break
            response["trace"] = trace
        return response

    @staticmethod
    def _bounded(value: Any, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return [value] if value.strip() else []
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        return []

    @staticmethod
    def _query_identifiers(query: str) -> set[str]:
        identifiers: set[str] = set()
        for token in tokens_of(query):
            has_discrete_separator = any(character in token for character in "._/#")
            has_hyphenated_code = "-" in token and any(character.isdigit() for character in token)
            has_alphanumeric_code = (
                any(character.isdigit() for character in token)
                and any(character.isalpha() for character in token)
                and token.upper() == token
            )
            if has_discrete_separator or has_hyphenated_code or has_alphanumeric_code:
                identifiers.add(token.casefold())
        return identifiers

    def _identifier_coverage(
        self,
        item: dict[str, Any],
        identifiers: set[str],
        entity_names: list[str],
    ) -> float:
        if not identifiers:
            return 1.0
        tokens = {
            token.casefold() for token in tokens_of(self._search_text(item) + " " + " ".join(entity_names))
        }
        return len(identifiers & tokens) / len(identifiers)

    def _entity_links_by_document(
        self, user_id: str, document_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {}
        unique_ids = list(dict.fromkeys(document_ids))
        for start in range(0, len(unique_ids), 350):
            chunk = unique_ids[start : start + 350]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            # The only interpolated fragment is a bounded sequence of ``?`` placeholders.
            query = f"""SELECT l.knowledge_object_id, l.entity_id, l.confidence,
                           e.name, e.entity_type
                    FROM knowledge_entity_links l
                    JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                    WHERE l.user_id=? AND l.status='accepted' AND e.deleted_at IS NULL
                      AND l.knowledge_object_id IN ({placeholders})
                    ORDER BY l.confidence DESC, e.name COLLATE NOCASE"""  # nosec B608
            rows = self.storage.execute(query, (user_id, *chunk)).fetchall()
            for row in rows:
                output.setdefault(str(row["knowledge_object_id"]), []).append(
                    {
                        "id": row["entity_id"],
                        "name": row["name"],
                        "type": row["entity_type"],
                        "confidence": float(row["confidence"] or 0.0),
                    }
                )
        return output

    def _noise_penalty(self, item: dict[str, Any]) -> float:
        metadata = self._json_dict(item.get("metadata_json"))
        assessment = metadata.get("promotion_assessment")
        if not isinstance(assessment, dict):
            assessment = {}
        category = str(assessment.get("category", ""))
        action = str(assessment.get("action", ""))
        penalty = 0.0
        if category in {"question", "greeting", "command"}:
            penalty += 0.12
        if action == "transient":
            penalty += 0.12
        if self._bounded(item.get("quality_score"), 0.5) < 0.28:
            penalty += 0.08
        if self._bounded(item.get("promotion_score"), 0.5) < 0.28:
            penalty += 0.07
        content = str(item.get("content") or "").strip()
        if len(content.split()) < 5 and str(item.get("content_type")) != "file":
            penalty += 0.045
        return min(0.28, penalty)

    @staticmethod
    def _kind_alignment(query: str, knowledge_kind: str) -> float:
        patterns = {
            "task": r"\b(?:задач|сделать|дедлайн|todo|task|deadline)\w*",
            "decision": r"\b(?:решени|решили|decision|decided)\w*",
            "preference": r"\b(?:предпочит|нрав|любим|preference|prefer|favou?r)\w*",
            "event": r"\b(?:встреч|событи|конференц|meeting|event)\w*",
            "project": r"\b(?:проект|репозитор|project|repository)\w*",
            "procedure": r"\b(?:инструкц|процедур|настро|runbook|procedure)\w*",
            "contact": r"\b(?:контакт|телефон|почт|contact|email)\w*",
            "document": r"\b(?:файл|документ|отч[её]т|file|document|report)\w*",
        }
        pattern = patterns.get(knowledge_kind)
        return 1.0 if pattern and re.search(pattern, query, re.I) else 0.0

    @staticmethod
    def _search_text(item: dict[str, Any]) -> str:
        return knowledge_search_text(item)

    def _lexical_rank(
        self,
        candidates: list[dict[str, Any]],
        query: str,
        *,
        cache: dict[str, dict[str, float]] | None = None,
    ) -> tuple[list[str], dict[str, float]]:
        query_vector = lexical_vector(query)
        # One vector per candidate per request. `_lexical_rank` runs twice — once
        # before dense recall widens the pool and once after — and each run rebuilt
        # the token and trigram vector of every candidate's FULL body from scratch.
        # Profiling one search over a 400-candidate pool counted 2708 calls to
        # `lexical_vector`, about half the request's total time.
        vectors = cache if cache is not None else {}
        scored: list[tuple[str, float]] = []
        for item in candidates:
            document_id = str(item["id"])
            vector = vectors.get(document_id)
            if vector is None:
                vector = lexical_vector(self._search_text(item))
                vectors[document_id] = vector
            scored.append((document_id, sparse_cosine(query_vector, vector)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [document_id for document_id, _ in scored], dict(scored)

    async def _dense_recall(
        self,
        user_id: str,
        query: str,
        candidate_map: dict[str, dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        """Corpus-wide dense recall over persisted vectors.

        The query is embedded once, cosine-scored against every stored vector for
        the active model, and the strongest matches are UNIONED into the candidate
        pool — so a semantically relevant object that shares no lexical tokens and is
        not recent still becomes retrievable. When no vectors are indexed yet (the
        indexer has not run since embeddings were enabled), this degrades to
        re-ranking the existing pool by embedding the candidate texts directly.
        """
        assert self.embeddings is not None
        query_vectors = await self.embeddings.embed([query])
        if not query_vectors:
            return {}
        query_vector = query_vectors[0]
        query_dim = len(query_vector)
        if query_dim == 0:
            return {}
        settings = self.embeddings.settings
        model = settings.embeddings_model
        max_objects = int(settings.embeddings_dense_max_objects)
        stored = self.storage.get_user_embeddings(user_id, model, query_dim, limit=(max_objects or None))
        if meta is not None:
            # Deliberately still counted in OBJECTS: the cap, its explain-trace flag
            # and the operator-facing wording all keep the meaning they had.
            meta["dense_scanned"] = len(stored)
            meta["dense_capped"] = bool(max_objects) and len(stored) >= max_objects
        doc_scores = {
            document_id: score for score, document_id in dense_scores(query_vector, stored, query_dim)
        }

        chunk_scores: dict[str, float] = {}
        provenance: dict[str, tuple[int, int]] = {}
        chunk_rows: list[tuple[str, bytes]] = []
        if settings.embeddings_chunk_chars > 0 and self._chunk_recall:
            # Floored at one object's worth of chunks: the fuse exists to bound a
            # heavily split corpus, never to scan a single document only halfway.
            row_cap = max(
                max_objects * max(1, int(settings.embeddings_chunk_scan_multiplier)),
                int(settings.embeddings_chunk_max_per_object) if max_objects else 0,
            )
            # Over-fetch by one object's worth, then drop the trailing PARTIAL object:
            # a plain LIMIT cuts at an arbitrary chunk index, which both hides the
            # passage that answers the query and — because the corroboration term
            # averages over however many chunks were scanned — hands the truncated
            # document an undiscounted score it did not earn.
            over_fetch = int(settings.embeddings_chunk_max_per_object)
            fetched = self.storage.get_user_chunk_embeddings(
                user_id,
                model,
                query_dim,
                object_limit=(max_objects or None),
                row_limit=(row_cap + over_fetch if row_cap else None),
            )
            chunk_rows = _trim_to_whole_objects(fetched, row_cap) if row_cap else fetched
            if meta is not None:
                meta["dense_chunks_scanned"] = len(chunk_rows)
                meta["dense_chunks_capped"] = bool(row_cap) and len(chunk_rows) < len(fetched)
            chunk_scores, provenance = aggregate_chunk_scores(
                dense_scores(query_vector, chunk_rows, query_dim),
                blend=float(settings.embeddings_chunk_blend),
            )

        if not stored and not chunk_rows:
            # Nothing indexed yet: degrade to re-ranking the pool, exactly as before.
            return await self._dense_recall_pool(query_vector, candidate_map)

        # The whole-object vector is the FLOOR: passage scores can only raise an
        # object, never lower it, so chunking cannot regress any existing result.
        # Built in a DETERMINISTIC order (doc rows first, in DB order) — a set union
        # would iterate in per-process string-hash order and leak PYTHONHASHSEED into
        # tie-breaking, which must stay identical to pre-0.41 when chunking is off.
        combined: dict[str, float] = {}
        for document_id in (*doc_scores, *chunk_scores):
            if document_id not in combined:
                combined[document_id] = max(
                    doc_scores.get(document_id, -1.0), chunk_scores.get(document_id, -1.0)
                )
        if not combined:
            return {}

        # Promote from BOTH rankings. Selecting on the combined score alone would let
        # chunk-boosted long documents evict objects the whole-object vector had
        # already earned (max-over-passages is systematically higher than the document
        # average) — and an object outside candidate_map loses its score entirely, so
        # the floor would protect the value while the selection silently dropped it.
        # Ties break on (score, id), matching the pre-0.41 nlargest over (score, id).
        top_k = max(1, int(settings.embeddings_recall_candidates))

        def _ranked(scores: dict[str, float]) -> list[str]:
            return [
                document_id
                for document_id, _ in heapq.nlargest(
                    top_k, scores.items(), key=lambda pair: (pair[1], pair[0])
                )
            ]

        promoted = list(dict.fromkeys(_ranked(doc_scores) + _ranked(combined)))
        for document_id in promoted:
            if document_id in candidate_map:
                continue
            item = self.storage.get_knowledge_object(document_id, user_id)
            if item and not item.get("deleted_at"):
                candidate_map[document_id] = item
        if meta is not None:
            # Per-call, never on the instance: HybridSearcher is shared and search()
            # is async, so instance state would race between concurrent queries.
            meta["chunk_provenance"] = {
                document_id: provenance[document_id]
                for document_id in combined
                if document_id in provenance
                and chunk_scores.get(document_id, -1.0) >= doc_scores.get(document_id, -1.0)
            }
        return {document_id: score for document_id, score in combined.items() if document_id in candidate_map}

    async def _dense_recall_pool(
        self,
        query_vector: list[float],
        candidate_map: dict[str, dict[str, Any]],
    ) -> dict[str, float]:
        """Fallback: embed and score the current pool when no vectors are indexed.

        Bounded twice, because neither bound the indexer applies reached here. Every
        candidate's FULL body went into a single request — title, summary, content,
        tags, kind, untruncated, for up to `pool_max` objects — so the fallback that
        exists to keep search working before the index is built was the one request
        most likely to time out or be rejected outright. A 400-object pool of
        ordinary articles is several megabytes in one POST.
        """
        assert self.embeddings is not None
        candidates = list(candidate_map.values())
        if not candidates:
            return {}
        texts = [self._search_text(item)[:_POOL_TEXT_MAX_CHARS] for item in candidates]
        vectors: list[list[float]] = []
        start = 0
        while start < len(texts):
            end, volume = start, 0
            while end < len(texts) and (end == start or volume + len(texts[end]) <= _POOL_REQUEST_MAX_CHARS):
                volume += len(texts[end])
                end += 1
            returned = await self.embeddings.embed(texts[start:end])
            if not returned or len(returned) != end - start:
                # All-or-nothing, like the indexer: a partially embedded pool would
                # score some candidates densely and others not at all, which reads as
                # a ranking decision rather than a failed request.
                return {}
            vectors.extend(returned)
            start = end
        if len(vectors) != len(candidates):
            return {}
        return {
            item["id"]: dense_cosine(query_vector, vectors[index]) for index, item in enumerate(candidates)
        }

    def _field_scores(
        self,
        item: dict[str, Any],
        query: str,
        entity_names: list[str],
        *,
        query_vector: dict[str, float] | None = None,
    ) -> dict[str, float]:
        # The QUERY vector does not depend on the candidate, and this runs once per
        # candidate: on a 400-object pool that was 400 rebuilds of the same thing.
        vector = lexical_vector(query) if query_vector is None else query_vector
        query_folded = query.casefold()
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "")
        tags = " ".join(self._json_list(item.get("tags_json")))
        entities = " ".join(entity_names)
        exact_phrase = (
            1.0 if len(query_folded) >= 3 and query_folded in self._search_text(item).casefold() else 0.0
        )
        return {
            "title": sparse_cosine(vector, lexical_vector(title)),
            "summary": sparse_cosine(vector, lexical_vector(summary)),
            "tags": sparse_cosine(vector, lexical_vector(tags)),
            "entities": sparse_cosine(vector, lexical_vector(entities)),
            "exact_phrase": exact_phrase,
        }

    def _feedback_scores(self, user_id: str, document_ids: list[str]) -> dict[str, float]:
        if not document_ids:
            return {}
        output: dict[str, float] = {}
        for start in range(0, len(document_ids), 400):
            chunk = document_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            # The only interpolated fragment is a bounded sequence of ``?`` placeholders.
            query = f"""SELECT target_id, AVG(score) AS score FROM feedback_state
                    WHERE user_id=? AND target_id IN ({placeholders})
                      AND feedback_type IN ('search_quality', 'answer_usefulness')
                    GROUP BY target_id"""  # nosec B608
            rows = self.storage.execute(query, (user_id, *chunk)).fetchall()
            output.update({row["target_id"]: float(row["score"] or 0.0) for row in rows})
        return output
