#!/usr/bin/env python
"""Find out what `JERICHO_DEDUP_THRESHOLD` actually catches on the installed model.

Near-duplicate detection compares whole-document embeddings and flags a pair above
the threshold as a review-gated conflict. The default is 0.92 and it has never been
measured — a detector that never fires and a detector that fires on everything look
identical from the outside, because both produce a quiet review queue.

The measurement uses the production path end to end: `knowledge_search_text` builds
the indexed text exactly as the worker does, `EmbeddingBackend` embeds it with the
configured model, and the score is the same cosine `find_near_duplicates` computes.

Six relations, each a pair of documents of the length this installation actually
stores, in Russian:

* IDENTICAL  — the same note saved twice. Must be caught.
* EDITED     — the same note after typo fixes and a added sentence. Must be caught:
               this is what re-saving a note looks like.
* REFORMATTED— the same facts as prose instead of bullets. Must be caught.
* RETOLD     — the same event described independently, sharing few words. Should be
               caught; this is the hardest true positive.
* SAME_TOPIC — two different notes about one subject. Must NOT be caught: merging
               them destroys the distinction the owner drew.
* UNRELATED  — different subjects. Must NOT be caught.

    python tools/dedup_threshold_probe.py

Rerun whenever the embedding model changes: the number belongs to the model.
"""

from __future__ import annotations

import asyncio
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jericho.config import load_settings  # noqa: E402
from jericho.retrieval import (  # noqa: E402
    EmbeddingBackend,
    knowledge_search_text,
    lexical_vector,
    sparse_cosine,
)

_NUMBER_RE = re.compile(r"\d[\d\s.,]*")


def numeric_overlap(left: str, right: str) -> float:
    """Jaccard over the numbers two documents contain.

    Tested because dense cosine cannot separate the two classes that matter, and the
    difference between them is visible to the naked eye: a reformatted duplicate keeps
    every figure (45 000, 4 000, 31 августа), while two meeting notes about one project
    differ precisely in their figures (5 июня vs 12 июля, different amounts).
    """

    def numbers(text: str) -> set[str]:
        return {match.group(0).strip().replace(" ", "") for match in _NUMBER_RE.finditer(text)}

    a, b = numbers(left), numbers(right)
    union = a | b
    return len(a & b) / len(union) if union else 0.0


_ARENDA = (
    "Съём квартиры на Мира 12. Аренда 45 тысяч рублей в месяц, коммунальные "
    "оплачиваются отдельно, примерно 4 тысячи зимой. Залог — один месяц, вернут "
    "при выезде, если нет повреждений. Договор подписан до 31 августа, продление "
    "обсуждаем в июле. Хозяйку зовут Ирина, телефон записан в контактах. "
    "Интернет включён в стоимость, роутер её. Ремонт мелочей — за наш счёт, "
    "крупное — за хозяйкой."
)
_ARENDA_EDITED = (
    "Съём квартиры на Мира 12. Аренда 45 тысяч рублей в месяц, коммунальные "
    "оплачиваются отдельно, примерно 4 тысячи зимой. Залог — один месяц, вернут "
    "при выезде, если нет повреждений. Договор подписан до 31 августа, продление "
    "обсуждаем в июле. Хозяйку зовут Ирина, телефон записан в контактах. "
    "Интернет включён в стоимость, роутер её. Ремонт мелочей — за наш счёт, "
    "крупное — за хозяйкой. Добавил: счётчики снимаем 25 числа и отправляем ей в "
    "тот же день."
)
_ARENDA_REFORMATTED = (
    "Квартира, Мира 12.\n"
    "- Аренда: 45 000 ₽/мес\n"
    "- Коммуналка: отдельно, ~4 000 ₽ зимой\n"
    "- Залог: 1 месяц, возврат при выезде без повреждений\n"
    "- Договор: до 31 августа, продление обсуждаем в июле\n"
    "- Хозяйка: Ирина, телефон в контактах\n"
    "- Интернет: входит в стоимость, роутер хозяйский\n"
    "- Ремонт: мелочи наши, крупное — хозяйки"
)
_ARENDA_OTHER = (
    "Соседи сверху затопили ванную в квартире на Мира 12. Вызвали сантехника, "
    "составили акт, хозяйка Ирина приехала на следующий день. Ремонт потолка "
    "делают за счёт соседей, договорились без страховой. Заняло две недели, "
    "пришлось перенести стиральную машину. Записываю на случай, если всплывёт "
    "при возврате залога."
)

_VSTRECHA = (
    "Планёрка по проекту Атлас во вторник. Обсудили сроки: интеграция с биллингом "
    "переносится на сентябрь, потому что подрядчик не отдал спецификацию. Решили "
    "не ждать и параллельно готовить миграцию данных. Ответственный — Сергей, "
    "проверка на стенде до пятницы. Риск: если спецификация не придёт до конца "
    "августа, сдвигается вся осень."
)
_VSTRECHA_RETOLD = (
    "Запись после совещания. Атлас: биллинг уедет на осень, спецификации от "
    "подрядчика до сих пор нет. Договорились начинать перенос данных не дожидаясь "
    "её. Сергей берёт стенд, срок — конец недели. Если бумаги не появятся к "
    "сентябрю, поедут все оставшиеся сроки."
)

# The dangerous class. In a personal archive the commonest near-miss is not two
# unrelated notes — it is two notes about the SAME thing at different times: last
# week's meeting and this week's, two entries about one apartment, two receipts from
# one shop. They share vocabulary, subject and often structure, and merging them
# destroys exactly the distinction the owner was keeping. Whatever the highest of
# these scores is, that is the real floor for any threshold.
_PLANERKA_JUNE = (
    "Планёрка по проекту Атлас, 5 июня. Прошли статус по интеграции с биллингом: "
    "подрядчик обещает спецификацию к концу месяца. Обсудили нагрузочное "
    "тестирование, договорились гонять на копии боевых данных. Сергей показал "
    "черновик схемы миграции. Следующая встреча через неделю."
)
_PLANERKA_JULY = (
    "Планёрка по проекту Атлас, 12 июля. Спецификации от подрядчика по-прежнему "
    "нет, интеграция с биллингом уезжает на сентябрь. Нагрузочное тестирование "
    "прошло, узкое место — очередь уведомлений. Сергей доделал схему миграции, "
    "проверяем на стенде. Следующая встреча через две недели."
)
_CHEK_MAY = (
    "Покупка в Ситилинк, 14 мая. Взял SSD на 2 ТБ и планки памяти, вышло 18 400 "
    "рублей. Чек в почте, гарантия три года. Ставил в тот же вечер, система "
    "увидела сразу, без танцев с прошивкой."
)
_CHEK_JUNE = (
    "Покупка в Ситилинк, 20 июня. Взял монитор 27 дюймов и кабель DisplayPort, "
    "вышло 31 200 рублей. Чек в почте, гарантия два года. Подключил вечером, "
    "пришлось поменять частоту обновления вручную."
)
_IRINA_CONTACT = (
    "Хозяйка квартиры Ирина: звонить после десяти утра, в выходные лучше писать. "
    "Оплату принимает переводом на карту, скидывать чек в тот же день. По ремонту "
    "сначала согласовать, потом делать — иначе не компенсирует."
)
_IRINA_MEETING = (
    "Встретились с Ириной по поводу продления договора. Готова продлить на год на "
    "тех же условиях, если поднимем оплату до 48 тысяч с октября. Просила решить "
    "до конца августа. Про залог сказала, что пересчитывать не будем."
)

# The worst case for ANY overlap signal: notes the owner writes from a template.
# Weekly meeting minutes, a daily log, a recurring report — these share not just the
# subject but the wording, and only the specifics differ. If a pair of signals is
# going to produce a false merge proposal, it produces it here.
_SHABLON_1 = (
    "Планёрка по проекту Атлас, неделя 23.\n"
    "Присутствовали: Сергей, Ольга, Дмитрий.\n"
    "Статус: интеграция с биллингом в работе, спецификация у подрядчика.\n"
    "Решения: гоняем нагрузочное на копии боевых данных.\n"
    "Риски: сроки подрядчика.\n"
    "Следующая встреча: через неделю."
)
_SHABLON_2 = (
    "Планёрка по проекту Атлас, неделя 24.\n"
    "Присутствовали: Сергей, Ольга, Дмитрий.\n"
    "Статус: интеграция с биллингом в работе, спецификация у подрядчика.\n"
    "Решения: переносим нагрузочное на следующую неделю, освобождаем стенд.\n"
    "Риски: сроки подрядчика.\n"
    "Следующая встреча: через неделю."
)
_OTCHET_1 = (
    "Отчёт за понедельник. Сделано: разобрал почту, ответил по договору аренды, "
    "проверил бэкапы. Не сделано: не дошли руки до отчёта по расходам. "
    "Завтра: созвон в 11, отчёт по расходам, зал в 19."
)
_OTCHET_2 = (
    "Отчёт за вторник. Сделано: созвон в 11 прошёл, отчёт по расходам сдал, "
    "проверил бэкапы. Не сделано: не разобрал почту. "
    "Завтра: планёрка в 10, разобрать почту, зал в 19."
)

_UNRELATED = (
    "Рецепт борща, как готовит мама. Свёклу натереть и потушить отдельно с "
    "томатной пастой и уксусом, иначе цвет уйдёт. Капусту закладывать за десять "
    "минут до конца, картошку — раньше. Бульон на говяжьей грудинке, варить два "
    "часа на медленном огне. Дать настояться ночь, на следующий день вкуснее."
)

PAIRS: list[tuple[str, str, str]] = [
    ("identical", _ARENDA, _ARENDA),
    ("identical", _VSTRECHA, _VSTRECHA),
    ("edited", _ARENDA, _ARENDA_EDITED),
    ("edited", _VSTRECHA, _VSTRECHA + " Добавил: протокол разослан всем участникам."),
    ("reformatted", _ARENDA, _ARENDA_REFORMATTED),
    ("retold", _VSTRECHA, _VSTRECHA_RETOLD),
    ("retold", _ARENDA_EDITED, _ARENDA_REFORMATTED),
    ("same_topic", _ARENDA, _ARENDA_OTHER),
    ("same_topic", _VSTRECHA, _ARENDA),
    ("same_topic", _PLANERKA_JUNE, _PLANERKA_JULY),
    ("same_topic", _VSTRECHA, _PLANERKA_JULY),
    ("same_topic", _VSTRECHA, _PLANERKA_JUNE),
    ("same_topic", _CHEK_MAY, _CHEK_JUNE),
    ("same_topic", _IRINA_CONTACT, _IRINA_MEETING),
    ("same_topic", _ARENDA, _IRINA_MEETING),
    ("same_topic", _SHABLON_1, _SHABLON_2),
    ("same_topic", _OTCHET_1, _OTCHET_2),
    ("same_topic", _SHABLON_1, _PLANERKA_JULY),
    ("unrelated", _ARENDA, _UNRELATED),
    ("unrelated", _VSTRECHA, _UNRELATED),
]

RELATIONS = ("identical", "edited", "reformatted", "retold", "same_topic", "unrelated")
# Relations a near-duplicate detector exists to catch.
DUPLICATES = ("identical", "edited", "reformatted", "retold")


def as_object(content: str) -> dict[str, str]:
    """Wrap raw text the way the indexer sees a Knowledge Object."""
    title = content.strip().split(".", 1)[0][:80]
    return {
        "title": title,
        "summary": title,
        "content": content,
        "tags_json": "[]",
        "knowledge_kind": "note",
    }


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


async def main() -> int:
    settings = load_settings()
    backend = EmbeddingBackend(settings)
    if not backend.remote_enabled:
        print("embeddings disabled — set JERICHO_EMBEDDINGS_* and retry", file=sys.stderr)
        return 2

    texts = [knowledge_search_text(as_object(text)) for _, left, right in PAIRS for text in (left, right)]
    vectors = await backend.embed(texts)
    if vectors is None:
        print("embeddings backend returned nothing — see the warning in the log", file=sys.stderr)
        return 1

    by_relation: dict[str, list[float]] = {}
    rows: list[tuple[str, float, float, float]] = []
    for index, (relation, left, right) in enumerate(PAIRS):
        dense = cosine(vectors[index * 2], vectors[index * 2 + 1])
        lexical = sparse_cosine(lexical_vector(left), lexical_vector(right))
        numeric = numeric_overlap(left, right)
        by_relation.setdefault(relation, []).append(dense)
        rows.append((relation, dense, lexical, numeric))

    print(f"model: {settings.embeddings_model}, threshold in use: {settings.dedup_threshold}\n")
    print(f"{'relation':<13} {'n':>2} {'min':>7} {'max':>7}   caught at 0.92")
    for relation in RELATIONS:
        values = sorted(by_relation.get(relation, []))
        if not values:
            continue
        caught = sum(1 for value in values if value >= 0.92)
        print(
            f"{relation:<13} {len(values):>2} {values[0]:>7.3f} {values[-1]:>7.3f}   {caught}/{len(values)}"
        )

    targets = [value for relation in DUPLICATES for value in by_relation.get(relation, [])]
    others = [
        value
        for relation in RELATIONS
        if relation not in DUPLICATES
        for value in by_relation.get(relation, [])
    ]
    print("\nthreshold sweep (caught duplicates / false flags):")
    for step in range(70, 100, 2):
        threshold = step / 100.0
        caught = sum(1 for value in targets if value >= threshold)
        false = sum(1 for value in others if value >= threshold)
        mark = "  <- default" if step == 92 else ""
        print(f"  {threshold:.2f}  {caught}/{len(targets)}  {false}/{len(others)}{mark}")

    print(f"\n{'relation':<13} {'dense':>7} {'lexical':>8} {'numeric':>8}")
    for relation, dense, lexical, numeric in rows:
        print(f"{relation:<13} {dense:>7.3f} {lexical:>8.3f} {numeric:>8.3f}")

    print("\nseparability by signal (duplicates vs everything else):")
    for name, column in (("dense", 1), ("lexical", 2), ("numeric", 3)):
        target = [row[column] for row in rows if row[0] in DUPLICATES]
        other = [row[column] for row in rows if row[0] not in DUPLICATES]
        gap = min(target) - max(other)
        verdict = f"separable, gap {gap:+.3f}" if gap > 0 else f"overlapping by {-gap:.3f}"
        print(f"  {name:<8} duplicates >= {min(target):.3f}, others <= {max(other):.3f}  -> {verdict}")

    highest_false = max(others) if others else 0.0
    lowest_true = min(targets) if targets else 0.0
    print(f"\nhighest non-duplicate: {highest_false:.3f}")
    print(f"lowest true duplicate: {lowest_true:.3f}")
    if lowest_true > highest_false:
        print(
            f"separable — any threshold in ({highest_false:.3f}, {lowest_true:.3f}] catches all, flags none"
        )
    else:
        print("NOT separable by a single cosine on this model at document scale")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
