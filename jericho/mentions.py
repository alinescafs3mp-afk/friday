"""Где в тексте документа упомянуты его сущности — позициями, а не догадкой.

Связь знание↔сущность хранит имя и метод, но НЕ хранит места в тексте: позиции
нигде не персистятся, и вычислять их приходится при показе. Это и правильно —
текст объекта можно править, а сохранённое смещение пережило бы правку и стало бы
указывать не туда.

Главная опасность здесь известна заранее и стоила отдельной починки в выдержке:
позиции, найденные в СВЁРНУТОЙ строке, нельзя применять к исходной, если свёртка
меняет длину. `casefold` её меняет ('ß' → 'ss', 'ﬁ' → 'fi'), а pypdf отдаёт
лигатуры как есть — на таком документе подсветка молча уехала бы на соседние слова.
Поэтому используется свёртка, сохраняющая длину.

Имя ищется по основе: «Иванов» находится в «Иванову», но выделяется слово целиком —
подсветка половины слова читается как ошибка разметки, а не как совпадение.
"""

from __future__ import annotations

from dataclasses import dataclass

from jericho.retrieval import _search_form, _snippet_fold

# Потолок на документ. Подсветка — вспомогательный слой: на тексте, где сущность
# встречается тысячу раз, разметка перестаёт помогать и начинает мешать, а ответ
# раздувается. Обрезка называет себя в ответе, а не молчит.
_MAX_SPANS = 500
# Основа короче трёх знаков совпадает с половиной алфавита.
_MIN_STEM = 3
# Сколько знаков разрешено дописать к основе, чтобы совпадение осталось тем же
# словом. Русское падежное окончание — один-три знака («Иванову» = «иван» + «ову»),
# а «новость» = «нов» + «ость»: четыре, и это уже другое слово. Без этого предела
# основа короткого имени цепляет всё, что с неё начинается — поймано тестом.
_MAX_ENDING = 3


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    name: str
    entity_id: str


def _is_word_char(character: str) -> bool:
    return character.isalnum() or character in "_-"


def mention_spans(text: str, entities: list[tuple[str, str]]) -> list[Mention]:
    """Непересекающиеся места упоминаний. ``entities`` — пары (имя, id сущности).

    Порядок разбора — от длинных имён к коротким: «Иван Петров» должен выиграть у
    «Иван», иначе внутри длинного имени окажется вложенная разметка.
    """
    body = text or ""
    if not body or not entities:
        return []
    folded = _snippet_fold(body)
    taken = [False] * len(body)
    found: list[Mention] = []
    for name, entity_id in sorted(entities, key=lambda pair: -len(pair[0])):
        clean = " ".join(str(name or "").split()).strip()
        if not clean:
            continue
        needle = _search_form(clean)
        if len(needle) < _MIN_STEM:
            continue
        start = 0
        while len(found) < _MAX_SPANS:
            position = folded.find(needle, start)
            if position < 0:
                break
            start = position + len(needle)
            # Совпадение должно начинаться на границе слова: «нов» внутри «Иванов»
            # это не упоминание «Нов».
            if position > 0 and _is_word_char(body[position - 1]):
                continue
            # Слово целиком: имя ищется по основе, поэтому «Иванов» находится в
            # «Иванову», и выделять надо до конца слова, а не до конца основы.
            end = position + len(needle)
            while end < len(body) and _is_word_char(body[end]):
                end += 1
            if end - (position + len(needle)) > _MAX_ENDING:
                # Дописано слишком много: это не падеж, а другое слово.
                continue
            if any(taken[position:end]):
                continue
            for index in range(position, end):
                taken[index] = True
            found.append(Mention(start=position, end=end, name=clean, entity_id=entity_id))
    found.sort(key=lambda item: item.start)
    return found
