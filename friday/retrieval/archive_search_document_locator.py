"""Version identities for archive document text-span locators.

The legacy identity addresses a bounded slice of the exact Raw/Knowledge body
and historically required ``chunk_index == 0``.  The stored-passage identity is
minted only from an authenticated current ``document_passages`` child.  Keeping
the two identities distinct lets durable legacy selections replay without
pretending that they came from the rebuildable passage sidecar.
"""

from __future__ import annotations

from typing import Final

LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION: Final = "archive-storage-char-v1"
DOCUMENT_STORED_PASSAGE_INDEX_VERSION: Final = "archive-storage-char-v2:document-chunk-spans-v3"

__all__ = [
    "DOCUMENT_STORED_PASSAGE_INDEX_VERSION",
    "LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION",
]
