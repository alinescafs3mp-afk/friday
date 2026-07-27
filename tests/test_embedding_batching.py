"""An embeddings request is bounded by how much TEXT it carries, not how many strings.

Batches were sized by input count alone. That number says nothing about the work: 256
strings of eighteen characters and 65 passages of two thousand differ by two orders of
magnitude, and only the second is what a real document looks like.

Measured on the live service at roughly 2800 characters per second. One promoted
document produced 65 inputs totalling 149000 characters — about 53 seconds against a 60
second request timeout. It lost that race on every cycle, and because an object's inputs
travel together, it ended up with no vector at all and was unfindable. The log said
"backend returned no usable vectors", which reads as a broken endpoint rather than a
request that was simply too large.

The same measurement is why my own earlier benchmark was wrong: 256 toy strings embedded
in 2.45s looked like 105 vectors/second, and that number is meaningless for documents.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from jericho.workers import _DOC_VECTOR_MAX_CHARS, _EMBED_REQUEST_MAX_CHARS, WorkersManager


class _Backend:
    """Records the shape of each request; optionally fails one of them."""

    def __init__(self, *, fail_on: int | None = None, dimensions: int = 8):
        self.requests: list[list[int]] = []
        self._fail_on = fail_on
        self._dimensions = dimensions

    async def embed(self, texts: list[str]):
        self.requests.append([len(text) for text in texts])
        if self._fail_on is not None and len(self.requests) == self._fail_on:
            return None
        return [[0.1] * self._dimensions for _ in texts]


def _manager(backend) -> WorkersManager:
    manager = WorkersManager.__new__(WorkersManager)
    manager.embeddings = backend
    return manager


def _run(manager, texts):
    return asyncio.run(manager._embed_in_volume_slices(texts))


def test_a_large_object_is_split_across_requests():
    """The measured case: 65 passages that used to go in one oversized request."""
    backend = _Backend()
    texts = ["x" * 2300 for _ in range(65)]

    vectors = _run(_manager(backend), texts)

    assert vectors is not None and len(vectors) == 65
    assert len(backend.requests) > 1, "149000 characters still went in a single request"
    assert all(sum(request) <= _EMBED_REQUEST_MAX_CHARS for request in backend.requests)


def test_order_is_preserved_so_the_offset_ledger_still_lines_up():
    """Vectors are attributed by position; a reordering would file them under the wrong
    Knowledge Objects, which no log line would ever reveal."""
    backend = _Backend()
    texts = [f"{index}" + "y" * 15_000 for index in range(6)]

    _run(_manager(backend), texts)

    flattened = [length for request in backend.requests for length in request]
    assert flattened == [len(text) for text in texts]


def test_a_small_batch_still_goes_in_one_request():
    backend = _Backend()
    vectors = _run(_manager(backend), ["короткий текст"] * 20)

    assert len(backend.requests) == 1
    assert vectors is not None and len(vectors) == 20


def test_one_failed_slice_fails_the_whole_object():
    """All-or-nothing on purpose.

    A partially embedded object keeps some passages and silently loses others, and
    nothing downstream can tell that apart from an object that simply has fewer.
    """
    backend = _Backend(fail_on=2)
    texts = ["z" * 2300 for _ in range(65)]

    assert _run(_manager(backend), texts) is None


def test_a_single_oversized_text_is_still_sent():
    """Refusing it would drop content. _DOC_VECTOR_MAX_CHARS already keeps the largest
    input well under what a service accepts."""
    backend = _Backend()
    texts = ["w" * (_EMBED_REQUEST_MAX_CHARS + 5_000)]

    vectors = _run(_manager(backend), texts)

    assert vectors is not None and len(vectors) == 1
    assert len(backend.requests) == 1


def test_nothing_in_means_nothing_asked():
    backend = _Backend()
    assert _run(_manager(backend), []) == []
    assert backend.requests == []


@pytest.mark.parametrize("size", [1, 2, 17, 64, 65, 200])
def test_every_input_comes_back_exactly_once(size):
    backend = _Backend()
    texts = [f"текст {index} " + "q" * 1200 for index in range(size)]

    vectors = _run(_manager(backend), texts)

    assert vectors is not None and len(vectors) == size
    assert sum(len(request) for request in backend.requests) == size


def test_the_document_vector_cap_fits_inside_a_request():
    """The two ceilings must not contradict each other: a whole-document input has to be
    embeddable on its own without exceeding the request budget."""
    assert _DOC_VECTOR_MAX_CHARS <= _EMBED_REQUEST_MAX_CHARS


def test_the_splitting_is_actually_wired_into_the_indexer():
    """Testing the helper is not testing that anything calls it.

    Mutation check: reverting ``_embed_group`` to a single ``embeddings.embed`` call
    left every test above passing, because they exercised the helper directly. This one
    goes through the indexer's own path, which is where the oversized request was made.
    """
    backend = _Backend()
    manager = _manager(backend)

    # One object shaped like the promoted document: a bounded doc text plus 64 passages.
    plan = {
        "row": {"id": "ko_probe", "user_id": "alice"},
        "texts": ["d" * _DOC_VECTOR_MAX_CHARS] + ["p" * 2100 for _ in range(64)],
        "hashes": [f"h{index}" for index in range(65)],
        "cached": {},
        "missing": list(range(65)),
    }
    # Storage is only reached after a successful embed; failing there is fine, the
    # request shape is what this asserts.
    with contextlib.suppress(Exception):
        asyncio.run(manager._embed_group([plan], "test-model", "scheme-v1"))

    assert backend.requests, "the indexer never called the backend"
    assert len(backend.requests) > 1, "the indexer still sends one oversized request"
    assert all(sum(request) <= _EMBED_REQUEST_MAX_CHARS for request in backend.requests)
