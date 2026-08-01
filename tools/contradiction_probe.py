#!/usr/bin/env python
"""Test whether cosine distance can find contradictions, before building on the idea.

The proposal was appealing: two Knowledge Objects that CONTRADICT each other should
sit in a middle cosine band — close enough to be about the same thing, far enough not
to be a paraphrase — so that band becomes a cheap prefilter and an LLM judges only
what falls inside it.

That is a claim about a specific embedding model, and it had never been measured.
This measures it, through the production embedding path (`EmbeddingBackend`, the
configured model, the same key inheritance), on Russian text of the shape this
installation actually stores, across four relations:

* CONTRADICTION — same subject, incompatible claims. The target.
* PARAPHRASE    — same claim, different words. Must not be flagged.
* UPDATE        — same subject, later state. Must not be flagged: supersession is
                  what Friday is *for*, not an error to report.
* UNRELATED     — different subject. Must not be flagged.

The question is not "is the band 0.78-0.92 correct". It is whether ANY threshold
separates contradiction from the other three well enough to be worth an LLM call.

    python tools/contradiction_probe.py

Run it again whenever the embedding model changes; the answer is a property of the
model, not of the idea.
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday.config import load_settings  # noqa: E402
from friday.retrieval import EmbeddingBackend  # noqa: E402

# Each pair is (relation, text_a, text_b). Subjects repeat across relations on
# purpose: the same claim appears as a contradiction, a paraphrase and an update,
# so any separation found is between the RELATIONS and not between the topics.
PAIRS: list[tuple[str, str, str]] = [
    ("contradiction", "Встреча с Игорем во вторник в 15:00.", "Встреча с Игорем в среду в 11:00."),
    ("contradiction", "Аренда квартиры стоит 45 тысяч в месяц.", "Аренда квартиры стоит 60 тысяч в месяц."),
    ("contradiction", "Пароль от роутера — admin1234.", "Пароль от роутера — qwerty777."),
    ("contradiction", "Проект сдаём в сентябре.", "Проект сдаём в декабре."),
    ("contradiction", "Ваня работает в Яндексе.", "Ваня работает в Сбере."),
    ("contradiction", "Кофеварка на кухне сломана.", "Кофеварка на кухне работает нормально."),
    ("contradiction", "Резервные копии делаются каждый день.", "Резервные копии не делаются вообще."),
    ("contradiction", "Врач сказал принимать таблетки утром.", "Врач сказал принимать таблетки вечером."),
    ("paraphrase", "Встреча с Игорем во вторник в 15:00.", "Во вторник в три часа дня встречаюсь с Игорем."),
    ("paraphrase", "Аренда квартиры стоит 45 тысяч в месяц.", "За квартиру платим 45 тысяч ежемесячно."),
    ("paraphrase", "Проект сдаём в сентябре.", "Сдача проекта запланирована на сентябрь."),
    ("paraphrase", "Ваня работает в Яндексе.", "Ваня — сотрудник Яндекса."),
    ("paraphrase", "Кофеварка на кухне сломана.", "Кухонная кофеварка вышла из строя."),
    ("paraphrase", "Резервные копии делаются каждый день.", "Бэкап снимается ежесуточно."),
    ("update", "Аренда квартиры стоит 45 тысяч в месяц.", "С января аренда выросла до 60 тысяч в месяц."),
    ("update", "Проект сдаём в сентябре.", "Срок сдачи проекта перенесли на декабрь."),
    ("update", "Ваня работает в Яндексе.", "Ваня перешёл из Яндекса в Сбер."),
    ("update", "Кофеварка на кухне сломана.", "Кофеварку починили, снова работает."),
    ("update", "Встреча с Игорем во вторник в 15:00.", "Игорь перенёс встречу на среду."),
    ("unrelated", "Встреча с Игорем во вторник в 15:00.", "Рецепт борща: свёкла, капуста, томатная паста."),
    ("unrelated", "Аренда квартиры стоит 45 тысяч в месяц.", "Настройка WireGuard на домашнем сервере."),
    ("unrelated", "Проект сдаём в сентябре.", "Список книг на лето: Стругацкие, Лем."),
    ("unrelated", "Пароль от роутера — admin1234.", "Ребёнку нужен новый рюкзак к школе."),
    ("unrelated", "Врач сказал принимать таблетки утром.", "Договор с подрядчиком по ремонту балкона."),
    ("unrelated", "Резервные копии делаются каждый день.", "Отпуск планируем в Грузию."),
]

RELATIONS = ("contradiction", "paraphrase", "update", "unrelated")
PROPOSED_BAND = (0.78, 0.92)


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


async def main() -> int:
    settings = load_settings()
    backend = EmbeddingBackend(settings)
    if not backend.remote_enabled:
        print("embeddings disabled — set FRIDAY_EMBEDDINGS_* and retry", file=sys.stderr)
        return 2

    texts = [text for _, left, right in PAIRS for text in (left, right)]
    vectors = await backend.embed(texts)
    if vectors is None:
        print("embeddings backend returned nothing — see the warning in the log", file=sys.stderr)
        return 1

    scored = [
        (relation, cosine(vectors[index * 2], vectors[index * 2 + 1]), left)
        for index, (relation, left, _) in enumerate(PAIRS)
    ]
    by_relation: dict[str, list[float]] = {}
    for relation, score, _ in scored:
        by_relation.setdefault(relation, []).append(score)

    print(f"model: {settings.embeddings_model}\n")
    print(f"{'relation':<14} {'n':>3} {'min':>7} {'median':>7} {'max':>7}")
    for relation in RELATIONS:
        values = sorted(by_relation.get(relation, []))
        if values:
            print(
                f"{relation:<14} {len(values):>3} {values[0]:>7.3f} "
                f"{values[len(values) // 2]:>7.3f} {values[-1]:>7.3f}"
            )

    low, high = PROPOSED_BAND
    print(f"\ninside the proposed band {low}-{high}:")
    for relation in RELATIONS:
        values = by_relation.get(relation, [])
        inside = [value for value in values if low <= value <= high]
        share = 100.0 * len(inside) / len(values) if values else 0.0
        print(f"  {relation:<14} {len(inside):>2}/{len(values):<2} = {share:5.1f}%")

    # The kindest possible reading of the idea: pick the threshold that sends the
    # fewest non-contradictions to the LLM while still catching most contradictions.
    targets = by_relation.get("contradiction", [])
    others = [value for relation in RELATIONS[1:] for value in by_relation.get(relation, [])]
    best: tuple[float, float, int] | None = None
    for step in range(1, 100):
        threshold = step / 100.0
        caught = sum(1 for value in targets if value >= threshold)
        if not targets or caught / len(targets) < 0.8:
            continue
        noise = sum(1 for value in others if value >= threshold)
        if best is None or noise < best[2]:
            best = (threshold, caught / len(targets), noise)
    print("\nbest single threshold at >=80% recall:")
    if best is None:
        print("  none")
    else:
        threshold, recall, noise = best
        print(
            f"  >= {threshold:.2f} catches {recall:.0%} of contradictions and drags "
            f"{noise}/{len(others)} non-contradictions to the LLM"
        )

    print("\nper pair, descending:")
    for relation, score, left in sorted(scored, key=lambda row: -row[1]):
        print(f"  {score:.3f}  {relation:<14} {left[:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
