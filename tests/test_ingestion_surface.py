"""`IngestionPipeline` is the write path into knowledge; splitting it must not move it.

Everything a Knowledge Object can become — captured, staged, classified, promoted,
enriched — goes through this class. It grew to 39 methods inside a 3564-line module,
and it is the last god-module. As with storage, the split is by mixin, so the harness
is the class surface plus the guard a name list cannot give: two mixins defining the
same method shadow each other by MRO while the surface still looks intact.

Update EXPECTED only when deliberately adding or removing a method.
"""

from __future__ import annotations

import inspect

from jericho.ingestion import IngestionPipeline


def _surface() -> dict[str, str]:
    surface: dict[str, str] = {}
    for name, member in inspect.getmembers(IngestionPipeline):
        if name.startswith("__"):
            continue
        if isinstance(member, property):
            surface[name] = "property"
            continue
        if not (inspect.isfunction(member) or inspect.ismethod(member)):
            continue
        surface[name] = str(inspect.signature(member))
    return surface


def test_pipeline_exposes_the_same_surface() -> None:
    surface = _surface()
    assert len(surface) == EXPECTED_MEMBER_COUNT, (
        f"IngestionPipeline exposes {len(surface)} members, expected {EXPECTED_MEMBER_COUNT}."
    )
    missing = sorted(set(EXPECTED_SIGNATURES) - set(surface))
    assert not missing, f"members disappeared: {missing}"
    changed = sorted(
        name
        for name, signature in EXPECTED_SIGNATURES.items()
        if name in surface and surface[name] != signature
    )
    assert not changed, f"signatures changed: {changed}"


# Which members are coroutines is part of the contract and `inspect.signature`
# does not show it: a method that quietly becomes `async` returns a coroutine to
# every existing caller, and a caller that forgets `await` gets a truthy object
# instead of a result — no exception, no test failure, just a silently skipped
# step. Pinned separately for that reason.
EXPECTED_ASYNC = frozenset(
    {
        "_extract_visual_document",
        "_transcribe_audio",
        "advise_inbox_item",
        "ingest_file",
        "ingest_text",
        "inspect_file_transient",
        "queue_agent_candidate",
        "queue_knowledge_work_candidate",
        "queue_research_candidate",
    }
)


def test_async_members_are_exactly_the_pinned_ones() -> None:
    actual = {
        name
        for name, member in inspect.getmembers(IngestionPipeline)
        if not name.startswith("__") and inspect.iscoroutinefunction(member)
    }
    assert actual == EXPECTED_ASYNC, (
        f"became async: {sorted(actual - EXPECTED_ASYNC)}; "
        f"stopped being async: {sorted(EXPECTED_ASYNC - actual)}"
    )


def test_no_pipeline_method_is_defined_twice() -> None:
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for base in IngestionPipeline.__mro__:
        if base is object:
            continue
        for name, member in vars(base).items():
            if name.startswith("__") or not callable(member):
                continue
            if name in seen and seen[name] != base.__name__:
                duplicates.append(f"{name}: {seen[name]} and {base.__name__}")
            seen.setdefault(name, base.__name__)
    assert not duplicates, f"method defined in more than one base: {duplicates}"


def test_public_names_stay_importable() -> None:
    """The three names the rest of the tree imports from this package."""
    from jericho.ingestion import (  # noqa: F401
        IdempotencyConflictError,
        _extract_entities,
    )


EXPECTED_MEMBER_COUNT = 40
EXPECTED_SIGNATURES: dict[str, str] = {
    "_apply_feedback_calibration": "(self, user_id: 'str', assessment: 'PromotionAssessment') -> 'PromotionAssessment'",
    "_commit_staged_file": "(self, target: 'Path', staged: 'Path | None', digest: 'str') -> 'Path'",
    "_create_promoted_ko": "(self, inbox_id: 'str', user_id: 'str', item: 'dict[str, Any]', reviewer: 'str', *, title: 'str | None', summary: 'str | None', knowledge_kind: 'str | None', importance: 'float | None', metadata: 'dict[str, Any] | None', tags: 'list[str] | None') -> 'str | None'",
    "_enrich": "(self, content: 'str', assessment: 'PromotionAssessment', *, user_id: 'str') -> 'KnowledgeEnrichment'",
    "_entity_suggestions": "(self, user_id: 'str', content: 'str') -> 'list[dict[str, Any]]'",
    "_extract_visual_document": "(self, file_content: 'bytes', *, filename: 'str', mime_type: 'str') -> 'dict[str, Any] | None'",
    "_file_sha256": "(path: 'Path') -> 'str'",
    "_file_target": "(self, user_id: 'str', digest: 'str', filename: 'str') -> 'Path'",
    "_link_entities": "(self, user_id: 'str', ko_id: 'str', raw_id: 'str', entity_candidates: 'list[dict[str, Any]]') -> 'tuple[list[dict[str, Any]], list[dict[str, Any]]]'",
    "_promote_raw": "(self, *, raw: 'RawObject', content: 'str', assessment: 'PromotionAssessment', enrichment: 'KnowledgeEnrichment', force_pending: 'bool' = False) -> 'dict[str, Any]'",
    "_record_event_times": "(self, user_id: 'str', content: 'str', graph_links: 'list[dict[str, Any]]') -> 'None'",
    "_replay_file_source": "(self, user_id: 'str', existing_raw: 'dict[str, Any]') -> 'dict[str, Any]'",
    "_replay_text_source": "(self, user_id: 'str', existing_raw: 'dict[str, Any]') -> 'dict[str, Any]'",
    "_safe_component": "(value: 'str') -> 'str'",
    "_sanitize_filename": "(filename: 'str') -> 'str'",
    "_stage_file": "(self, user_id: 'str', content: 'bytes', digest: 'str', filename: 'str') -> 'tuple[Path, Path | None]'",
    "_store_file": "(self, user_id: 'str', content: 'bytes', digest: 'str', filename: 'str') -> 'Path'",
    "_store_review_inbox": "(self, raw: 'RawObject', assessment: 'PromotionAssessment', enrichment: 'KnowledgeEnrichment') -> 'InboxItem'",
    "_transcribe_audio": "(self, content: 'bytes', *, filename: 'str', mime_type: 'str', metadata: 'dict[str, Any] | None') -> 'dict[str, Any] | None'",
    "_validate_existing_file_source": "(existing: 'dict[str, Any]', digest: 'str') -> 'None'",
    "advise_inbox_item": "(self, user_id: 'str', inbox_id: 'str', *, llm: 'LLMRouter', requested_by: 'str' = '', force: 'bool' = False) -> 'dict[str, Any]'",
    "apply_legacy_cleanup": "(self, user_id: 'str', knowledge_object_id: 'str', *, action: 'str', reviewed_by: 'str', reason: 'str' = 'legacy quality cleanup') -> 'dict[str, Any]'",
    "assess_existing_knowledge": "(self, user_id: 'str', knowledge: 'dict[str, Any] | str', *, threshold: 'float' = 0.55, include_suggestion: 'bool' = False) -> 'dict[str, Any]'",
    "assess_text": "(self, content: 'str', *, force_knowledge: 'bool' = False) -> 'PromotionAssessment'",
    "bind_knowledge_graph": "(self, knowledge_graph: 'KnowledgeGraph') -> 'None'",
    "bind_llm": "(self, llm: 'LLMRouter') -> 'None'",
    "classify_inbox_item": "(self, user_id: 'str', inbox_id: 'str', status: 'InboxStatus', *, entity_id: 'str | None' = None, tags: 'list[str] | None' = None, notes: 'str' = '', reviewed_by: 'str | None' = None, promote: 'bool | None' = None, title: 'str | None' = None, summary: 'str | None' = None, knowledge_kind: 'str | None' = None, importance: 'float | None' = None, metadata: 'dict[str, Any] | None' = None) -> 'dict[str, Any] | None'",
    "ingest_file": "(self, user_id: 'str', file_path: 'Path | None', file_content: 'bytes', *, filename: 'str' = '', mime_type: 'str' = '', media_kind: 'str' = '', metadata: 'dict[str, Any] | None' = None, source_ref: 'str' = '', force_review: 'bool' = False) -> 'dict[str, Any]'",
    "ingest_text": "(self, user_id: 'str', content: 'str', *, source: 'str' = 'telegram', source_ref: 'str' = '', force_knowledge: 'bool' = False, force_review: 'bool' = False, metadata: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "inspect_file_transient": "(self, file_content: 'bytes', *, filename: 'str' = '', mime_type: 'str' = '', preview_chars: 'int' = 24000) -> 'dict[str, Any]'",
    "list_inbox": "(self, user_id: 'str', status: 'InboxStatus | None' = None) -> 'list[dict[str, Any]]'",
    "queue_agent_candidate": "(self, user_id: 'str', content: 'str', *, source_ref: 'str', candidate_type: 'str', metadata: 'dict[str, Any] | None' = None, suggestion_overrides: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "queue_knowledge_work_candidate": "(self, user_id: 'str', content: 'str', *, source_ref: 'str', metadata: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "queue_research_candidate": "(self, user_id: 'str', content: 'str', *, source_ref: 'str', metadata: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "reenrich_knowledge": "(self, user_id: 'str', knowledge_object_id: 'str', *, apply: 'bool' = False, reviewed_by: 'str | None' = None) -> 'dict[str, Any]'",
    "return_knowledge_to_inbox": "(self, user_id: 'str', knowledge_object_id: 'str', *, reviewed_by: 'str', reason: 'str' = 'legacy quality review') -> 'dict[str, Any]'",
    "scan_legacy_low_quality": "(self, user_id: 'str', *, limit: 'int' = 250, threshold: 'float' = 0.48) -> 'list[dict[str, Any]]'",
    "scan_legacy_quality_page": "(self, user_id: 'str', *, limit: 'int' = 250, offset: 'int' = 0, threshold: 'float' = 0.55, include_archived: 'bool' = False) -> 'tuple[list[dict[str, Any]], int]'",
    "scan_legacy_quality": "(self, user_id: 'str', *, limit: 'int' = 250, threshold: 'float' = 0.55, include_archived: 'bool' = False) -> 'list[dict[str, Any]]'",
}
