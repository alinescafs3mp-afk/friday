#!/usr/bin/env python
"""Measure retrieval quality on a synthetic personal archive, with and without the model.

Jericho's live database is nearly empty, so retrieval cannot be measured on real data.
This builds a corpus that has the properties personal knowledge actually has and asks
the real searcher real questions.

The corpus is deterministic: same seed, same documents, same gold set. That is what
makes two runs comparable, which is the entire point — a recall number on its own says
little, and a recall number compared against a differently-generated corpus says
nothing at all.

The gold set is deliberately weighted toward the queries that are hard for lexical
search, because those are the ones that reveal what dense retrieval would buy:

* CROSS-SCRIPT — a Cyrillic question about a Latin-named thing ("как чинить кластер"
  against a note that only ever writes "Kubernetes"). No amount of FTS tokenisation
  bridges this; an embedding does.
* PARAPHRASE — the question shares no content word with the document.
* SYNONYM — the document uses one word for a thing, the question another.
* LITERAL — a rare exact term. Lexical search should ace these; if it does not, the
  problem is the index, not the ranking.

Usage:
    python tools/retrieval_bench.py --home /tmp/bench-a            # heuristic only
    JERICHO_LLM_ENABLED=1 python tools/retrieval_bench.py --home /tmp/bench-b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- the corpus -----------------------------------------------------------

# (document id, title line, body, category). Written as real Russian notes rather than
# lorem ipsum: the classifier and the model both key off how text actually reads.
DOCUMENTS: list[tuple[str, str, str, str]] = [
    (
        "k8s-restore",
        "Восстановление узлов Kubernetes",
        "После аварии в стойке B нужно вернуть узлы в строй. Порядок: сначала etcd, потом "
        "control plane, только затем воркеры. Команда kubectl uncordon снимает пометку. "
        "Если под не поднимается — смотреть kubelet на самом узле, а не в апи-сервере.",
        "Работа",
    ),
    (
        "pg-vacuum",
        "PostgreSQL и раздувание таблиц",
        "Таблица events разрослась до 40 ГБ при 6 ГБ полезных данных. Autovacuum не успевал. "
        "Помог VACUUM FULL в окно обслуживания и подъём autovacuum_vacuum_scale_factor. "
        "На будущее: партиционировать по месяцам.",
        "Работа",
    ),
    (
        "redis-evict",
        "Redis выселяет ключи раньше срока",
        "maxmemory-policy стояла allkeys-lru при maxmemory 2gb. Кэш сессий вымывался под "
        "нагрузкой отчётов. Разделили на два инстанса: сессии и аналитика.",
        "Работа",
    ),
    (
        "vpn-split",
        "Раздельное туннелирование в офисной сети",
        "Весь трафик уходил в туннель, включая локальные адреса. Настроили исключение "
        "приватных подсетей, иначе принтер в соседней комнате недоступен.",
        "Работа",
    ),
    (
        "dentist",
        "Запись к стоматологу",
        "Приём у Ирины Валентиновны 14 марта в 9:30, клиника на Лесной. Взять снимок с "
        "прошлого раза. Пломба на шестёрке снизу слева.",
        "Личное",
    ),
    (
        "kazan-trip",
        "Поездка в Казань",
        "Поезд 23 апреля в 21:40 с Казанского вокзала, вагон 7. Гостиница у кремля забронирована "
        "на три ночи. Обязательно попробовать эчпочмак и дойти до Свияжска.",
        "Личное",
    ),
    (
        "mother-birthday",
        "День рождения мамы",
        "12 июня. В прошлом году дарили плед, в позапрошлом — чайник. Хочет садовые ножницы "
        "и что-нибудь для рассады.",
        "Личное",
    ),
    (
        "car-service",
        "Обслуживание машины",
        "Замена масла каждые 10 тысяч, следующая на 87 400. Летнюю резину поменять до конца "
        "апреля. Сервис на Кузнецкой, спрашивать Дмитрия.",
        "Личное",
    ),
    (
        "mortgage",
        "Ипотека: досрочное погашение",
        "Выгоднее сокращать срок, а не платёж, пока ставка выше инфляции. Заявление подавать "
        "до 20 числа, иначе пересчёт уйдёт на следующий месяц.",
        "Финансы",
    ),
    (
        "tax-deduction",
        "Налоговый вычет за обучение",
        "Подать декларацию можно за три предыдущих года. Нужны договор с учебным заведением, "
        "лицензия и чеки. Лимит 120 тысяч в год на себя.",
        "Финансы",
    ),
    (
        "sourdough",
        "Закваска для хлеба",
        "Кормить раз в сутки в пропорции 1:5:5. Если пахнет ацетоном — голодает. Готовое тесто "
        "должно увеличиться вдвое за четыре часа при 24 градусах.",
        "Дом",
    ),
    (
        "plants",
        "Полив растений в отпуске",
        "Фикус переносит две недели без воды, монстера — нет. Соседке оставить ключи и "
        "написать список: кого поливать раз в неделю, кого раз в три дня.",
        "Дом",
    ),
    (
        "thesis-idea",
        "Идея для статьи о провенансе",
        "Главная мысль: система знаний должна помнить не только факт, но и откуда он взялся "
        "и кто его подтвердил. Без этого нельзя ни проверить, ни отозвать.",
        "Проекты",
    ),
    (
        "review-gate",
        "Почему нужен барьер проверки",
        "Автоматическое сохранение всего подряд превращает базу в свалку. Материал должен "
        "ждать подтверждения человека, прежде чем стать каноническим знанием.",
        "Проекты",
    ),
    (
        "backup-rule",
        "Правило резервных копий",
        "Копия на том же диске копией не является. Нужен внешний носитель и регулярная "
        "проверка восстановлением, а не только контрольная сумма.",
        "Проекты",
    ),
    (
        "naming",
        "Именование в коде",
        "Имя должно говорить, зачем сущность нужна, а не из чего состоит. Если в названии "
        "есть Manager или Helper — скорее всего, ответственность не выделена.",
        "Проекты",
    ),
    (
        "grafana-latency",
        "Grafana: checkout latency",
        "Dashboard checkout p99 latency поднялся до 2.4s после релиза. Scrape interval 15s, "
        "retention 30d. Alert fires above 1.5s for five minutes.",
        "Работа",
    ),
    (
        "ci-flaky",
        "Flaky tests in CI",
        "Pipeline fails intermittently on the integration stage. Retry budget exhausted. "
        "Root cause: shared fixture teardown race between parallel workers.",
        "Работа",
    ),
    (
        "s3-lifecycle",
        "S3 lifecycle policy",
        "Objects transition to Glacier after 90 days and expire after 365. Versioning enabled, "
        "noncurrent versions expire after 30 days.",
        "Работа",
    ),
    (
        "oauth-refresh",
        "OAuth refresh token rotation",
        "Access token TTL 15 minutes, refresh rotates on every use. Reuse detection revokes "
        "the whole token family and forces re-authentication.",
        "Работа",
    ),
]

# (query, expected document id, kind). Kind labels what the query is testing.
GOLD: list[tuple[str, str, str]] = [
    # LITERAL — the exact rare term appears in the document.
    ("Свияжск", "kazan-trip", "literal"),
    ("эчпочмак", "kazan-trip", "literal"),
    ("autovacuum_vacuum_scale_factor", "pg-vacuum", "literal"),
    ("maxmemory-policy allkeys-lru", "redis-evict", "literal"),
    ("Ирина Валентиновна стоматолог", "dentist", "literal"),
    # CROSS-SCRIPT — the concept exists in the document ONLY as a Latin term, and the
    # Russian question shares no token with it. Lexical search cannot bridge this by
    # construction; an embedding can. These are the cases that measure what dense
    # retrieval is worth, so they must not be satisfiable by word overlap.
    ("почему оформление заказа стало медленным", "grafana-latency", "cross-script"),
    ("тесты падают через раз без причины", "ci-flaky", "cross-script"),
    ("как настроить удаление старых файлов в облаке", "s3-lifecycle", "cross-script"),
    ("как устроено продление входа в систему", "oauth-refresh", "cross-script"),
    # LEXICAL-OVERLAP — honest labelling of what these actually test: the question
    # repeats words the document uses. Kept because a search engine must not fail them.
    ("как чинить кластер после аварии", "k8s-restore", "lexical"),
    ("база данных раздулась что делать", "pg-vacuum", "lexical"),
    ("кэш теряет сессии под нагрузкой", "redis-evict", "lexical"),
    # PARAPHRASE — no shared content word with the document.
    ("когда менять летние покрышки", "car-service", "paraphrase"),
    ("что подарить маме летом", "mother-birthday", "paraphrase"),
    ("вернуть часть денег за курсы", "tax-deduction", "paraphrase"),
    ("уменьшить переплату по кредиту на жильё", "mortgage", "paraphrase"),
    ("кто присмотрит за цветами пока меня нет", "plants", "paraphrase"),
    # SYNONYM — the concept is named differently.
    ("зачем нужна ручная модерация входящего", "review-gate", "synonym"),
    ("можно ли доверять записи без источника", "thesis-idea", "synonym"),
    ("почему копия рядом с оригиналом бесполезна", "backup-rule", "synonym"),
    ("как называть классы правильно", "naming", "synonym"),
    ("почему опара не растёт и даёт химический душок", "sourdough", "synonym"),
    ("локальная сеть недоступна через туннель", "vpn-split", "lexical"),
    ("сколько ехать поездом до Татарстана", "kazan-trip", "paraphrase"),
]

# Filler keeps the corpus from being trivially small: with 16 documents almost any
# ranking scores well, and the measurement would flatter itself.
FILLER_TOPICS = [
    "покупки на неделю",
    "список фильмов",
    "пароль от роутера сменить",
    "расписание тренировок",
    "книги к прочтению",
    "рецепт борща",
    "телефон мастера по окнам",
    "показания счётчиков",
    "маршрут пробежки",
]


# Labels that promise the query gives lexical search nothing to latch onto. If one of
# these shares content words with its document, the case is easy, the label lies, and
# the score is inflated in exactly the category that is supposed to justify embeddings.
SEMANTIC_KINDS = frozenset({"cross-script", "paraphrase", "synonym"})
# One shared word can be incidental ("нет", "как"); two means the query is quoting.
MAX_SHARED_TOKENS = 1


def audit_gold_set() -> list[str]:
    """Return a complaint per gold case whose label overstates its difficulty."""
    from jericho.retrieval import _STOPWORDS, tokens_of

    text_of = {doc_id: f"{title} {body}" for doc_id, title, body, _ in DOCUMENTS}
    complaints: list[str] = []
    for query, expected, kind in GOLD:
        if kind not in SEMANTIC_KINDS:
            continue
        query_tokens = {
            token.casefold()
            for token in tokens_of(query)
            if len(token) > 2 and token.casefold() not in _STOPWORDS
        }
        shared = query_tokens & {token.casefold() for token in tokens_of(text_of[expected])}
        if len(shared) > MAX_SHARED_TOKENS:
            complaints.append(
                f"{kind}: {query!r} shares {sorted(shared)} with {expected!r} — "
                "lexical search can answer it, so it does not test what the label claims"
            )
    return complaints


@dataclass
class Case:
    query: str
    expected: str
    kind: str


def build_corpus(root: Path, *, filler: int, seed: int) -> None:
    """Write the deterministic corpus to disk."""
    rng = random.Random(seed)
    if root.exists():
        shutil.rmtree(root)
    for doc_id, title, body, category in DOCUMENTS:
        folder = root / category
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{doc_id}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    misc = root / "Разное"
    misc.mkdir(parents=True, exist_ok=True)
    for index in range(filler):
        topic = FILLER_TOPICS[index % len(FILLER_TOPICS)]
        words = rng.sample(
            [
                "встреча",
                "отчёт",
                "заметка",
                "план",
                "черновик",
                "письмо",
                "счёт",
                "договор",
                "выписка",
                "инструкция",
                "памятка",
                "список",
            ],
            6,
        )
        (misc / f"note-{index:03d}.md").write_text(
            f"# {topic} {index}\n\n{' '.join(words)}. Запись номер {index}.\n", encoding="utf-8"
        )


async def ingest(root: Path, settings: Any, storage: Any, user_id: str) -> dict[str, Any]:
    from jericho.bulk_import import plan_import, run_import, summarise
    from jericho.ingestion import IngestionPipeline
    from jericho.knowledge_graph import KnowledgeGraph

    llm = None
    if settings.llm_enabled:
        from jericho.agent_runtime.llm import LLMRouter

        llm = LLMRouter(settings)
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), llm)
    plan = plan_import(root, max_bytes=settings.max_upload_bytes)
    started = time.monotonic()
    outcomes = await run_import(pipeline, user_id, plan)
    return {
        "files": len(plan.candidates),
        "seconds": round(time.monotonic() - started, 1),
        **summarise(outcomes),
    }


def promote_everything(storage: Any, pipeline: Any, user_id: str) -> int:
    """Confirm every pending item so the corpus is searchable.

    A bench measures RETRIEVAL, and pending material is deliberately unsearchable. This
    stands in for the human review that would normally happen — which is exactly why it
    lives in a bench script and not behind an API.
    """
    from jericho.storage.models import InboxStatus

    promoted = 0
    for item in storage.list_inbox(user_id, InboxStatus.PENDING, limit=5000):
        if pipeline.classify_inbox_item(
            user_id, item["id"], InboxStatus.CLASSIFIED, promote=True, reviewed_by="bench"
        ):
            promoted += 1
    return promoted


async def index_embeddings(settings: Any, storage: Any, kg: Any, embeddings: Any) -> dict[str, Any]:
    """Vectorise the corpus using the production indexer, not a reimplementation.

    Reusing ``WorkersManager._embeddings_index_all`` matters: it is the code that
    decides chunking, batch sizes and what counts as already-indexed. A bench that
    embedded documents its own way would be measuring a pipeline that does not exist.
    """
    from jericho.ingestion import IngestionPipeline
    from jericho.workers import WorkersManager

    manager = WorkersManager(
        settings,
        storage,
        IngestionPipeline(settings, storage, kg, None),
        kg,
        embeddings=embeddings,
    )
    started = time.monotonic()
    rounds = 0
    # The worker indexes one bounded batch per call; loop until the corpus is covered.
    while rounds < 200:
        remaining = storage.list_knowledge_missing_embedding(
            settings.embeddings_model,
            limit=1,
            chunk_scheme=None,
            chunk_threshold=settings.embeddings_chunk_chars,
        )
        if not remaining:
            break
        await manager._embeddings_index_all()
        rounds += 1
    vectors = storage.execute("SELECT COUNT(*) AS n FROM knowledge_embeddings").fetchone()["n"]
    chunks = storage.execute("SELECT COUNT(*) AS n FROM knowledge_chunk_embeddings").fetchone()["n"]
    return {
        "rounds": rounds,
        "seconds": round(time.monotonic() - started, 1),
        "object_vectors": int(vectors),
        "chunk_vectors": int(chunks),
    }


async def measure(searcher: Any, kg: Any, user_id: str, k: int) -> dict[str, Any]:
    """Recall@k overall and per query kind, plus the queries that found nothing."""
    by_kind: dict[str, list[bool]] = {}
    misses: list[dict[str, str]] = []
    for query, expected, kind in GOLD:
        hits = await searcher.search(user_id, query, limit=k, kg=kg)
        results = hits.get("results", [])
        found = False
        for hit in results[:k]:
            source = str(hit.get("source_ref") or "")
            title = str(hit.get("title") or "")
            body = str(hit.get("content") or hit.get("summary") or "")
            if expected in source or expected in title or expected in body:
                found = True
                break
            # Documents are named after their id; the title carries it once promoted.
            doc = next((d for d in DOCUMENTS if d[0] == expected), None)
            if doc and (doc[1] in title or doc[1] in body):
                found = True
                break
        by_kind.setdefault(kind, []).append(found)
        if not found:
            misses.append({"query": query, "expected": expected, "kind": kind})
    flat = [ok for values in by_kind.values() for ok in values]
    return {
        "k": k,
        "cases": len(flat),
        "recall": round(sum(flat) / len(flat), 4) if flat else None,
        "by_kind": {
            kind: {"recall": round(sum(v) / len(v), 4), "cases": len(v)}
            for kind, v in sorted(by_kind.items())
        },
        "misses": misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, help="Throwaway JERICHO_HOME for this run")
    parser.add_argument("--filler", type=int, default=120, help="Filler documents")
    parser.add_argument("--seed", type=int, default=20260726)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--keep", action="store_true", help="Keep the home directory afterwards")
    args = parser.parse_args()

    home = Path(args.home).expanduser().resolve()
    corpus = home / "corpus"
    os.environ["JERICHO_HOME"] = str(home)
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    build_corpus(corpus, filler=args.filler, seed=args.seed)

    from jericho.config import ensure_runtime_dirs, load_settings
    from jericho.retrieval import HybridSearcher
    from jericho.storage import init_storage

    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = init_storage(settings)
    report: dict[str, Any] = {
        "llm_enabled": settings.llm_enabled,
        "llm_base_url": settings.llm_base_url if settings.llm_enabled else None,
        "embeddings_enabled": settings.embeddings_enabled,
        "documents": len(DOCUMENTS),
        "filler": args.filler,
        "gold_cases": len(GOLD),
    }
    complaints = audit_gold_set()
    report["gold_set_audit"] = complaints or "ok"
    for complaint in complaints:
        print(f"GOLD SET WARNING: {complaint}", file=sys.stderr)
    try:
        report["ingest"] = asyncio.run(ingest(corpus, settings, storage, "bench"))

        from jericho.ingestion import IngestionPipeline
        from jericho.knowledge_graph import KnowledgeGraph

        pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
        report["promoted"] = promote_everything(storage, pipeline, "bench")
        # record_usage off: a harness must not write the counter that feeds the very
        # blend it is measuring.
        kg = KnowledgeGraph(storage)
        embeddings = None
        if settings.embeddings_enabled:
            from jericho.retrieval import EmbeddingBackend

            embeddings = EmbeddingBackend(settings)
            report["index"] = asyncio.run(index_embeddings(settings, storage, kg, embeddings))
        # The backend has to be HANDED to the searcher. Leaving it out is how the first
        # run with embeddings enabled reported a byte-identical score: the setting said
        # yes, the searcher was never told, and nothing measured anything new.
        searcher = HybridSearcher(
            storage,
            embeddings,
            graph_max_depth=settings.graph_max_depth,
            record_usage=False,
        )
        report["result"] = asyncio.run(measure(searcher, kg, "bench", args.k))
    finally:
        storage.close()
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
