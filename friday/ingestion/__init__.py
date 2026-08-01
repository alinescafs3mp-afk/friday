"""Review-gated ingestion: capture, assess, and promote only what a human approved.

The pipeline is assembled from one mixin per concern (see ``_capture``, ``_files``,
``_review`` and siblings). Each was lifted verbatim out of what used to be a single
3564-line module: names, signatures and bodies are unchanged, so ``IngestionPipeline``
is exactly the surface every caller already uses. ``tests/test_ingestion_surface.py``
pins that surface, including the shadowing a mixin split makes possible.
"""

from __future__ import annotations

from friday.ingestion._advice import AdviceMixin
from friday.ingestion._base import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    KnowledgeEnrichment,
    PromotionAssessment,
    _extract_entities,
)
from friday.ingestion._candidates import CandidatesMixin
from friday.ingestion._capture import CaptureMixin
from friday.ingestion._classifier import ContentClassifier
from friday.ingestion._core import CoreMixin
from friday.ingestion._files import FilesMixin
from friday.ingestion._legacy import LegacyMixin
from friday.ingestion._review import ReviewMixin

__all__ = [
    "ContentClassifier",
    "IdempotencyConflictError",
    "IdempotencyInProgressError",
    "IngestionPipeline",
    "KnowledgeEnrichment",
    "PromotionAssessment",
    "_extract_entities",
]


class IngestionPipeline(
    AdviceMixin, CandidatesMixin, CaptureMixin, CoreMixin, FilesMixin, LegacyMixin, ReviewMixin
):
    """Capture, assess and promote knowledge behind an explicit human review gate."""
