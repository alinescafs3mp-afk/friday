"""Слова бланка: те, что стоят в дословно повторяющемся окружении.

Последний класс тегового мусора, который не берётся ни списком, ни частотой.
Замер на живом архиве владельца после того, как убраны служебные слова, обрывки
ФИО и слишком частые слова: наверх вышли «фио» (144 объекта), «телефона» (129),
«проживает» (124), «работает» (122), «абонентский» (120). Это не тема документа,
а надпись графы анкеты: «Супруга (ФИО, дата рождения, где проживает, где
работает, абонентский номер телефона)».

Ни один простой признак их не отделяет, и это ЗАМЕРЕНО:

* доля корпуса — «фио» стоит у 9.4% документов, ниже любого разумного потолка;
* доля документов своего ВИДА — правило «слово покрывает ≥60% вида» ловит
  надписи анкеты, но на памятках убивает «гранат», а на нормативке «стрельбы»:
  там это темы, и документы вида действительно все про них;
* частота ВНУТРИ документа — надписи дают медиану 1–6, темы 1–26. Перекрытие
  полное, разделения нет.

Разделяет четвёртый признак: **надпись стоит в одном и том же окружении**.
Бланк повторяется дословно, тема — нет. Замер на тех же словах (доля вхождений,
у которых трёхсловное окно встречается не меньше чем в десяти документах):

    проживает 1.00   абонентский 1.00   фио 0.94   работает 0.93   прошу 0.88
    боеприпасов 0.45   стрельбы 0.36   вооружения 0.26   гранат 0.24   отпуск 0.02

Порог 0.65 отделяет одно от другого и не трогает ни одного слова-темы.

Список считается ПО КОРПУСУ, а не задаётся: у другого архива бланки другие, и
перечислять их значило бы написать шаблон под один архив. Здесь корпус сам
рассказывает, из чего состоят его формы.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from friday.storage import FridayStorage

#: Ключ, под которым список живёт в служебном словаре.
BOILERPLATE_KEY = "tags:boilerplate"

#: Доля вхождений в повторяющемся окружении, начиная с которой слово считается
#: надписью бланка. Замер выше: у надписей 0.68–1.00, у тем 0.02–0.45.
_REPEATED_CONTEXT_SHARE = 0.65

#: В скольких документах должно повториться окружение, чтобы считаться бланком.
#: Десять — это меньше самого малого вида архива (памятка, 12 документов), то
#: есть правило не требует, чтобы бланк был у частого вида.
_MIN_CONTEXT_DOCUMENTS = 10

#: Слово, встречающееся в двух-трёх документах, не стоит решения: его окружение
#: повторится случайно, а вреда от такого тега нет — он и так редкий.
_MIN_WORD_DOCUMENTS = 5

_WORD = re.compile(r"[А-ЯЁа-яёA-Za-z-]{2,}")


def _words(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORD.finditer(text or "")]


def learn_boilerplate(texts: list[str]) -> dict[str, Any]:
    """Найти слова бланка в корпусе. Возвращает список и то, чем он обоснован."""

    tokenised = [_words(text) for text in texts]
    context_documents: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    word_documents: Counter[str] = Counter()
    for index, tokens in enumerate(tokenised):
        for word in set(tokens):
            word_documents[word] += 1
        for position in range(1, len(tokens) - 1):
            context_documents[(tokens[position - 1], tokens[position], tokens[position + 1])].add(index)

    repeated: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for tokens in tokenised:
        for position in range(1, len(tokens) - 1):
            word = tokens[position]
            if word_documents[word] < _MIN_WORD_DOCUMENTS:
                continue
            total[word] += 1
            context = (tokens[position - 1], word, tokens[position + 1])
            if len(context_documents[context]) >= _MIN_CONTEXT_DOCUMENTS:
                repeated[word] += 1

    found: dict[str, float] = {}
    for word, count in total.items():
        share = repeated[word] / count
        if share >= _REPEATED_CONTEXT_SHARE:
            found[word] = round(share, 3)
    return {
        "words": sorted(found),
        "shares": found,
        "documents": len(texts),
        "considered": len(total),
    }


def store_boilerplate(storage: FridayStorage, learned: dict[str, Any]) -> None:
    storage.kv_set(
        BOILERPLATE_KEY,
        json.dumps(
            {"words": learned["words"], "documents": learned["documents"]},
            ensure_ascii=False,
        ),
    )


def stored_boilerplate(storage: FridayStorage) -> frozenset[str]:
    """Список бланковых слов, посчитанный проходом. Пусто — значит не считали.

    Пусто и должно означать «не считали», а не «их нет»: молча подставить
    догадку тут значило бы выдать пустой список за проверенный.
    """

    raw = storage.kv_get(BOILERPLATE_KEY)
    if not raw:
        return frozenset()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return frozenset()
    if not isinstance(parsed, dict):
        return frozenset()
    return frozenset(str(word) for word in (parsed.get("words") or []) if str(word))
