"""Ingestion: construction and late binding of collaborators.

Moved verbatim out of the single 3564-line module: same names, signatures and
bodies. Mixed back into ``IngestionPipeline``, so every collaborator resolves
exactly as before and no call site moved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jericho.ingestion._base import (
    DocumentExtractor,
    JerichoSettings,
    JerichoStorage,
    PipelineShared,
    PromotionAssessment,
    threading,
)
from jericho.ingestion._classifier import _classifier

if TYPE_CHECKING:
    from jericho.agent_runtime.llm import LLMRouter
    from jericho.knowledge_graph import KnowledgeGraph


class CoreMixin(PipelineShared):
    def __init__(
        self,
        settings: JerichoSettings,
        storage: JerichoStorage,
        knowledge_graph: KnowledgeGraph | None = None,
        llm: LLMRouter | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.knowledge_graph = knowledge_graph
        self.llm = llm
        # Serialises inbox promotion within this (single) backend process so
        # concurrent approvals cannot race on the shared storage connection.
        self._promotion_lock = threading.Lock()
        self._doc_extractor = DocumentExtractor(
            max_archive_entries=settings.max_archive_entries,
            max_archive_uncompressed_bytes=settings.max_archive_uncompressed_bytes,
            max_text_chars=settings.max_extracted_text_chars,
            max_input_bytes=settings.max_upload_bytes,
        )

    def bind_knowledge_graph(self, knowledge_graph: KnowledgeGraph) -> None:
        self.knowledge_graph = knowledge_graph

    def bind_llm(self, llm: LLMRouter) -> None:
        self.llm = llm

    def assess_text(self, content: str, *, force_knowledge: bool = False) -> PromotionAssessment:
        return _classifier.assess(content, force_knowledge=force_knowledge)
