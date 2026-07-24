"""Hybrid personal retrieval with lexical, FTS, optional embeddings, graph, and feedback signals."""

from __future__ import annotations

import array
import heapq
import json
import math
import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import httpx

from jericho.config import JerichoSettings

if TYPE_CHECKING:
    from jericho.storage import JerichoStorage

_TOKEN_RE = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ][0-9a-zA-Zа-яёА-ЯЁ._+#-]*", re.UNICODE)
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
    tokens = [token.casefold() for token in _TOKEN_RE.findall(text or "")]
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


def pack_vector(vector: list[float]) -> bytes:
    """Pack an embedding as little-agnostic float32 bytes for BLOB storage."""
    return array.array("f", (float(value) for value in vector)).tobytes()


def unpack_vector(blob: bytes) -> array.array:
    """Inverse of :func:`pack_vector` — returns a float32 array of the stored vector."""
    values = array.array("f")
    values.frombytes(blob)
    return values


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
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.settings.embeddings_base_url}/embeddings",
                    json={"model": self.settings.embeddings_model, "input": texts},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception:
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
    ) -> None:
        self.storage = storage
        self.embeddings = embeddings
        self._graph_max_depth = max(1, int(graph_max_depth))

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
        recent_pool = self.storage.list_knowledge_objects(
            user_id,
            limit=min(400, max(limit * 10, 100)),
        )
        candidate_map = {item["id"]: item for item in [*fts_candidates, *recent_pool]}
        candidates = list(candidate_map.values())

        fts_ranking = [item["id"] for item in fts_candidates]
        lexical_ranking, lexical_scores = self._lexical_rank(candidates, clean_query)
        rankings = [fts_ranking, lexical_ranking]
        embedding_scores: dict[str, float] = {}
        if self.embeddings and self.embeddings.remote_enabled:
            embedding_scores = await self._dense_recall(user_id, clean_query, candidate_map)
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
        lexical_ranking, lexical_scores = self._lexical_rank(candidates, clean_query)
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
        for document_id, item in candidate_map.items():
            names = [str(entity.get("name") or "") for entity in entity_links.get(document_id, [])]
            fields = self._field_scores(item, clean_query, names)
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
                + user_feedback * 0.05
                + usage_signal * 0.028
                + kind_alignment * 0.035
                + (0.018 if document_id in fts_ranking else 0.0)
                - noise_penalty
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
                and emb < 0.16
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
            item["_graph_score"] = round(graph_score, 6)
            item["_feedback_score"] = round(feedback.get(document_id, 0.0), 6)
            item["_score_components"] = components[document_id]
            if graph_evidence.get(document_id):
                item["_graph_evidence"] = graph_evidence[document_id]
            entities = entity_links.get(document_id, [])
            if entities:
                item["_entities"] = entities
            results.append(item)

        # Ranking remains available if a read-only/locked deployment cannot
        # persist this optional, best-effort usage signal.
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
        response: dict[str, Any] = {
            "query": clean_query,
            "results": results,
            "count": len(results),
            "entity_matches": entity_matches,
            "strategy": {
                "fts": True,
                "lexical": True,
                "embeddings": bool(embedding_scores),
                "feedback": True,
                "graph": bool(kg),
            },
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
        for token in _TOKEN_RE.findall(query or ""):
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
            token.casefold()
            for token in _TOKEN_RE.findall(self._search_text(item) + " " + " ".join(entity_names))
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
    ) -> tuple[list[str], dict[str, float]]:
        query_vector = lexical_vector(query)
        scored = [
            (item["id"], sparse_cosine(query_vector, lexical_vector(self._search_text(item))))
            for item in candidates
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [document_id for document_id, _ in scored], dict(scored)

    async def _dense_recall(
        self,
        user_id: str,
        query: str,
        candidate_map: dict[str, dict[str, Any]],
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
        model = self.embeddings.settings.embeddings_model
        stored = self.storage.get_user_embeddings(user_id, model, query_dim)
        if not stored:
            return await self._dense_recall_pool(query_vector, candidate_map)

        query_norm = math.sqrt(sum(value * value for value in query_vector))
        if query_norm == 0.0:
            return {}
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
        if not scored:
            return {}

        top_k = max(1, int(self.embeddings.settings.embeddings_recall_candidates))
        for _, document_id in heapq.nlargest(top_k, scored):
            if document_id in candidate_map:
                continue
            item = self.storage.get_knowledge_object(document_id, user_id)
            if item and not item.get("deleted_at"):
                candidate_map[document_id] = item
        return {document_id: score for score, document_id in scored if document_id in candidate_map}

    async def _dense_recall_pool(
        self,
        query_vector: list[float],
        candidate_map: dict[str, dict[str, Any]],
    ) -> dict[str, float]:
        """Fallback: embed and score the current pool when no vectors are indexed."""
        assert self.embeddings is not None
        candidates = list(candidate_map.values())
        if not candidates:
            return {}
        vectors = await self.embeddings.embed([self._search_text(item) for item in candidates])
        if not vectors or len(vectors) != len(candidates):
            return {}
        return {
            item["id"]: dense_cosine(query_vector, vectors[index]) for index, item in enumerate(candidates)
        }

    def _field_scores(
        self,
        item: dict[str, Any],
        query: str,
        entity_names: list[str],
    ) -> dict[str, float]:
        vector = lexical_vector(query)
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
