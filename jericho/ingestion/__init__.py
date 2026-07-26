"""Review-gated ingestion: capture, assess, and promote only what a human approved.

The pipeline is assembled from one mixin per concern (see ``_capture``, ``_files``,
``_review`` and siblings). Each was lifted verbatim out of what used to be a single
3564-line module: names, signatures and bodies are unchanged, so ``IngestionPipeline``
is exactly the surface every caller already uses. ``tests/test_ingestion_surface.py``
pins that surface, including the shadowing a mixin split makes possible.
"""

from __future__ import annotations

from jericho.ingestion._advice import AdviceMixin
from jericho.ingestion._base import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    KnowledgeEnrichment,
    PromotionAssessment,
    _extract_entities,
)
from jericho.ingestion._candidates import CandidatesMixin
from jericho.ingestion._capture import CaptureMixin
from jericho.ingestion._classifier import ContentClassifier
from jericho.ingestion._core import CoreMixin
from jericho.ingestion._files import FilesMixin
from jericho.ingestion._legacy import LegacyMixin
from jericho.ingestion._review import ReviewMixin

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
