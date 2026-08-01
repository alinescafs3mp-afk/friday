"""G10 remeasure: person-query answer_mode on today's code (graph_expansion=False).

Criterion announced BEFORE any classifier edit:
- person-archive forms with matching corpus → personal_knowledge or mixed, hits > 0
- true chitchat without archive hits → general_conversation
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path


async def main() -> None:
    td = tempfile.mkdtemp(prefix="jericho-g10-")
    home = Path(td) / "jericho-home"
    os.environ["FRIDAY_HOME"] = str(home)
    os.environ["FRIDAY_API_TOKEN"] = "A" * 48
    os.environ["FRIDAY_TELEGRAM_BRIDGE_SECRET"] = "B" * 48
    os.environ["FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS"] = "42"
    os.environ["FRIDAY_LLM_ENABLED"] = "0"
    os.environ["FRIDAY_EMBEDDINGS_ENABLED"] = "0"
    os.environ["FRIDAY_WORKERS_ENABLED"] = "0"
    os.environ["FRIDAY_CODE_EXECUTION_ENABLED"] = "0"
    os.environ["FRIDAY_EMBEDDINGS_INDEX_REST_RATIO"] = "0"
    os.environ.pop("FRIDAY_ENV_FILE", None)

    from friday.agent_runtime import AgentRuntime
    from friday.config import ensure_runtime_dirs, load_settings
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.retrieval import HybridSearcher
    from friday.storage import init_storage

    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = init_storage(settings)
    storage.ensure_user("alice")
    graph = KnowledgeGraph(storage)
    pipe = IngestionPipeline(settings, storage, graph)
    await pipe.ingest_text(
        "alice",
        (
            "Личное дело. Макаров Кирилл Евгеньевич, дата рождения 1999, "
            "должность инженер, личное дело СА-396195. "
            "Брат — Макаров Андрей Евгеньевич."
        ),
        force_knowledge=True,
        source_ref="ld-makarov",
    )
    await pipe.ingest_text(
        "alice",
        "Штатное расписание. Нестеренко Ольга Петровна, отдел кадров, телефон 100-200.",
        force_knowledge=True,
        source_ref="staff-nesterenko",
    )
    for i in range(5):
        await pipe.ingest_text(
            "alice",
            (
                f"Общий приказ N{i}: перечислить присутствующих. "
                "Макаров Кирилл Евгеньевич, Иванов Иван Иванович, "
                "Петров Пётр Петрович, Сидоров С.С., Козлов А.А."
            ),
            force_knowledge=True,
            source_ref=f"order-{i}",
        )

    searcher = HybridSearcher(storage)
    runtime = AgentRuntime(settings, storage)
    conv = storage.create_conversation("alice", title="g10")
    storage.store_message(
        conv["id"],
        "alice",
        "user",
        "что ты знаешь про Макарова Кирилла Евгеньевича",
    )
    history = storage.get_conversation_messages(conv["id"], user_id="alice")

    # Forms taken from correspondence / existing regression fixtures (not invented).
    person_qs = [
        "давай про Макарова Кирилла инфу",
        "что ты знаешь про Макарова Кирилла Евгеньевича",
        "найди мне человека с фамилией Нестеренко",
        "что известно о Нестеренко Ольге",
        "про Макарова Кирилла",
        "Макаров Кирилл Евгеньевич",
        "расскажи про Нестеренко",
        "а Макарова Кирилла?",
        "про Макарова Кирила",  # typo form
    ]
    followups = ["а его брат?", "а его должность?", "её телефон?"]
    chitchat = [
        "привет",
        "как дела?",
        "что думаешь о погоде?",
        "расскажи анекдот",
        "кто такой Наполеон?",
        "сколько будет 2+2",
        "спасибо",
        "ок",
    ]

    print("=== PERSON QUERIES ===")
    person_ok = 0
    for q in person_qs:
        ctx = await runtime._prepare_context(
            "alice",
            q,
            conv["id"],
            prior_history=[],
            kg=graph,
            searcher=searcher,
        )
        ok = ctx.answer_mode in {"personal_knowledge", "mixed"} and bool(ctx.knowledge_hits)
        person_ok += int(ok)
        top = ""
        if ctx.knowledge_hits:
            h = ctx.knowledge_hits[0]
            top = (
                f" top={h.get('title')!r} score={h.get('_score')} "
                f"lex={h.get('_lexical_score')}"
            )
        print(
            f"{'OK' if ok else 'FAIL'} {q!r} -> mode={ctx.answer_mode} "
            f"hits={len(ctx.knowledge_hits)} conf={ctx.retrieval_confidence}{top}"
        )

    print("=== FOLLOWUPS ===")
    fu_ok = 0
    for q in followups:
        ctx = await runtime._prepare_context(
            "alice",
            q,
            conv["id"],
            prior_history=history,
            kg=graph,
            searcher=searcher,
        )
        ok = ctx.answer_mode in {"personal_knowledge", "mixed"} and bool(ctx.knowledge_hits)
        fu_ok += int(ok)
        print(
            f"{'OK' if ok else 'FAIL'} {q!r} -> mode={ctx.answer_mode} "
            f"hits={len(ctx.knowledge_hits)} conf={ctx.retrieval_confidence} "
            f"q={ctx.search_query!r}"
        )

    print("=== CHITCHAT ===")
    chat_ok = 0
    for q in chitchat:
        ctx = await runtime._prepare_context(
            "alice",
            q,
            conv["id"],
            prior_history=[],
            kg=graph,
            searcher=searcher,
        )
        if not ctx.knowledge_hits:
            ok = ctx.answer_mode == "general_conversation"
        else:
            # Accidental hit on short tokens is ok as mixed, not forced personal cue.
            ok = ctx.answer_mode in {"mixed", "personal_knowledge", "general_conversation"}
            if ctx.answer_mode == "personal_knowledge_missing":
                ok = False
        chat_ok += int(ok)
        print(
            f"{'OK' if ok else 'FAIL'} {q!r} -> mode={ctx.answer_mode} "
            f"hits={len(ctx.knowledge_hits)} conf={ctx.retrieval_confidence}"
        )

    n_person = len(person_qs) + len(followups)
    person_total_ok = person_ok + fu_ok
    print()
    print(
        f"SUMMARY person {person_total_ok}/{n_person} "
        f"(standalone {person_ok}/{len(person_qs)}, followup {fu_ok}/{len(followups)}); "
        f"chitchat preserved {chat_ok}/{len(chitchat)}"
    )
    storage.close()


if __name__ == "__main__":
    asyncio.run(main())
