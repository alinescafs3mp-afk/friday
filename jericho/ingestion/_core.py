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
            # Тот же срок, что у веб-пути. Без него `ingest_file` держал поток пула
            # столько, сколько попросит файл: замерено 35 с на PDF в 41 КБ, и это
            # не потолок — стоимость растёт с числом операторов, а не с размером.
            parse_budget_sec=settings.pdf_parse_budget_sec,
        )

    def bind_knowledge_graph(self, knowledge_graph: KnowledgeGraph) -> None:
        self.knowledge_graph = knowledge_graph

    def bind_llm(self, llm: LLMRouter) -> None:
        self.llm = llm

    def assess_text(self, content: str, *, force_knowledge: bool = False) -> PromotionAssessment:
        return _classifier.assess(content, force_knowledge=force_knowledge)

    def review_required(self, *, force_review: bool, explicit_intent: bool) -> bool:
        """Does this arrival wait for a person before becoming canonical knowledge?

        ONE predicate for both paths. They used to decide separately, and the two
        answers were not the same rule: the text path consulted the strict-review
        setting, while `ingest_file` knew only about `force_review`, missing text
        and vision. That is how 342 of 344 documents on the stand walked into
        knowledge unseen while a pasted paragraph — the smaller commitment by far —
        was the one being judged.

        `force_review` is a floor, never a ceiling: a caller that asks for review
        gets it under every policy. So bulk import, `/api/ingest/url` and the
        importer organ are unaffected by whatever the owner chooses here.

        `explicit_intent` is passed IN rather than read off the classifier's
        signals, because `ingest_file` hands the classifier `force_knowledge=True`
        to get a useful assessment of the extracted text — which stamps
        `manual_promotion` on the result. Reading intent from that would let every
        file declare its own exemption from the policy, from inside.
        """
        if force_review:
            return True
        policy = self.settings.ingestion_review_policy
        if policy == "always":
            return True
        if policy == "unless_explicit":
            return not explicit_intent
        return False
