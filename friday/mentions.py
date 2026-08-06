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

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from friday.morphology import stem_to_fixpoint
from friday.retrieval import _search_form, _snippet_fold

# Потолок на документ. Подсветка — вспомогательный слой: на тексте, где сущность
# встречается тысячу раз, разметка перестаёт помогать и начинает мешать, а ответ
# раздувается. Обрезка называет себя в ответе, а не молчит.
_MAX_SPANS = 500
# Сколько собрать, прежде чем отобрать первые по тексту. Запас нужен, чтобы обрезка
# считалась по положению: см. цикл ниже.
_COLLECT_LIMIT = 5000
# Основа короче трёх знаков совпадает с половиной алфавита.
_MIN_STEM = 3
# Что разрешено дописать к основе, чтобы совпадение осталось ТЕМ ЖЕ словом.
#
# Первая версия считала знаки: не больше трёх. Правило ошибалось в обе стороны, и
# обе замерены на живом корпусе (600 документов, 18713 совпадений подтверждённых
# сущностей, стенд `~/.jericho/eval/mention_endings_probe.py`):
#
#   впускало чужое — «Работа» подсвечивала «работать» (работ+ать), «Победа» →
#   «победили» (побед+или), и глагол показывался как решение человека;
#   теряло своё — 10.2% совпадений имеют дописку длиннее трёх знаков, и почти всё
#   это законные прилагательные от названий: «ского» 771 раз, «ской» 446, «ская»
#   369, «ский» 204. Их подсветка не появлялась вовсе.
#
# Поэтому список, а не длина. Он закрытый и выведен из замера: перечисленное
# покрывает 99.8% встреченного. Дописка не равна падежному окончанию — стеммер
# режет «Иванов» до «иван», — поэтому здесь остаток основы вместе с окончанием.
#
# Что список отсекает намеренно: «ать», «или», «ость», «ичи» (Москва → Москвичи),
# а также «илл», «илов», «кин», «нос» — обрывки чужих фамилий, начинающихся с той
# же основы. Ошибаться этот список обязан в сторону пропуска: не подсветить
# упоминание — мелкая потеря, подсветить чужое слово как подтверждённое человеком
# решение — та самая подмена догадки решением, которую проект чинит везде.
_NOMINAL_TAILS = frozenset(
    {
        # Пустая дописка: имя стоит ровно в той форме, в какой записано.
        "",
        "а",
        "е",
        "и",
        "й",
        "о",
        "у",
        "ы",
        "ь",
        "ю",
        "я",
        # Двухбуквенные: падежи существительных и прилагательных.
        "ая",
        "ев",
        "ей",
        "ем",
        "ер",
        "еэ",
        "ие",
        "ии",
        "ий",
        "им",
        "ин",
        "их",
        "ию",
        "ия",
        "ов",
        "ое",
        "ой",
        "ом",
        "ою",
        "ск",
        "ую",
        "ые",
        "ый",
        "ым",
        "ых",
        "ье",
        "ью",
        "ья",
        "ям",
        "ях",
        # Трёхбуквенные.
        "ами",
        "ева",
        "еве",
        "еву",
        "евы",
        "его",
        "ием",
        "ими",
        "ина",
        "ине",
        "ину",
        "ины",
        "ких",
        "ова",
        "ове",
        "ову",
        "овы",
        "ого",
        "ому",
        "ска",
        "ями",
        # Длинные: прилагательные от названий и остатки основы у фамилий —
        # та самая десятая доля, которую прежнее правило теряло.
        "евым",
        "иным",
        "ного",
        "овым",
        "ская",
        "ские",
        "ский",
        "ским",
        "ских",
        "ской",
        "ском",
        "скую",
        "нская",
        "нские",
        "нский",
        "нских",
        "нской",
        "нском",
        "скими",
        "ского",
        "скому",
        "евская",
        "евский",
        "евской",
        "нского",
        "нскому",
        "овская",
        "овские",
        "овский",
        "овских",
        "овской",
        "евского",
        "овского",
        "овскому",
    }
)
# Длиннее этого дописка не бывает ни в одной форме списка — дешёвый отсев до поиска
# по множеству.
_MAX_ENDING = max(len(tail) for tail in _NOMINAL_TAILS)
_COOPERATIVE_MENTION_CHARS = 8_192
_COOPERATIVE_INFLECTION_HALO = 8_192
_MAX_COOPERATIVE_ENTITY_NAME_CHARS = 240
# A 240-character canonical card made only of accepted three-letter stems and
# separators contains at most sixty signature tokens; storage retains width-1.
MAX_INFLECTED_NAME_TOKENS = 60
_EXACT_PATTERN_WORK_LIMIT = 32


def _cursor_integer(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError("cursor integer required")


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    name: str
    entity_id: str


def _is_word_char(character: str) -> bool:
    return character.isalnum() or character in "_-"


def _is_token_char(character: str) -> bool:
    r"""The single-character equivalent of Python's Unicode ``(?u)\w``."""

    return character.isalnum() or character == "_"


_TOKEN_RE = re.compile(r"(?u)\w+")
_ALL_CYRILLIC = re.compile(r"^[а-яё]+$")


def _fold_word(token: str) -> str:
    """Свёртка ОДНОГО слова — ровно та, которой нормализовано имя узла графа.

    `normalize_entity_name` складывает падежи пословно (`_fold_russian`), поэтому
    «Кублику Александру Юрьевичу» и «Кублик Александр Юрьевич» — один и тот же
    `normalized_name`, то есть один узел. Здесь та же функция применяется к
    словам ТЕКСТА, чтобы упоминание узла считалось упоминанием того же узла.
    """

    low = token.casefold().replace("ё", "е")
    return stem_to_fixpoint(low) if _ALL_CYRILLIC.match(low) else low


def _inflected_spans(
    body: str,
    tokens: list[tuple[int, int, str]],
    name: str,
    entity_id: str,
    taken: list[bool],
    found: list[Mention],
) -> None:
    """Места, где многословное имя стоит в косвенном падеже.

    Без этого прохода имя из нескольких слов не находится ВООБЩЕ, как только
    склоняется: `_search_form` — стеммер одного токена, а на строке «Кублик
    Александр Юрьевич» он не срабатывает, и поиск подстроки ищет именительный
    падеж в тексте, где стоит дательный. Замерено на архиве владельца: в рапорте
    «Прошу… Прапорщику Кублику Александру Юрьевичу» не находился ни один из
    четырёх военнослужащих, ради которых рапорт написан, — привязывались только
    их родственники, названные в именительном. Отсюда же брались неверные связи:
    субъекта документа не было среди его сущностей, и родство доставалось
    соседнему имени.

    Правило узкое намеренно:

    * только имена из ДВУХ и более слов. У однословных свёртка склеивает разных
      людей — «Андрей» и «Андреев» оба дают «андр», — а многословное совпадение
      требует совпадения всех слов подряд. Замерено: все 4349 человек в архиве
      владельца записаны ровно тремя словами, так что ограничение не теряет людей;
    * только целиком кириллические слова: у идентификаторов и латиницы падежей
      нет, а стемминг там опасен;
    * основа каждого слова не короче трёх знаков.
    """

    parts = name.split()
    if len(parts) < 2:
        return
    if not all(_ALL_CYRILLIC.match(part.casefold().replace("ё", "е")) for part in parts):
        return
    words = tuple(_fold_word(part) for part in parts)
    if any(len(word) < _MIN_STEM for word in words):
        return
    span = len(words)
    for start_index in range(len(tokens) - span + 1):
        if any(tokens[start_index + offset][2] != words[offset] for offset in range(span)):
            continue
        start = tokens[start_index][0]
        end = tokens[start_index + span - 1][1]
        if any(taken[start:end]):
            continue
        for index in range(start, end):
            taken[index] = True
        found.append(Mention(start=start, end=end, name=name, entity_id=entity_id))
        if len(found) >= _COLLECT_LIMIT:
            return


def inflected_mentions(text: str, entities: list[tuple[str, str]]) -> dict[str, tuple[int, int]]:
    """Кто из ``entities`` назван в тексте в косвенном падеже. Пары (имя, id).

    Отдельная от `mention_spans` дверь для тех, кому нужен не список мест, а
    ответ «упомянута ли»: разбор при приёме и обратный проход по архиву.
    Оба уже проверяют БУКВАЛЬНОЕ вхождение имени и должны продолжать — у
    идентификаторов вроде `BRK.A` и `PK-04-04` границы слова строже, а падежей
    нет вовсе. Эта проверка добавляется рядом, а не вместо.
    """

    body = text or ""
    if not body or not entities:
        return {}
    tokens = [(m.start(), m.end(), _fold_word(m.group(0))) for m in _TOKEN_RE.finditer(body)]
    if not tokens:
        return {}
    taken = [False] * len(body)
    found: list[Mention] = []
    for name, entity_id in sorted(entities, key=lambda pair: -len(pair[0])):
        clean = " ".join(str(name or "").split()).strip()
        if clean:
            _inflected_spans(body, tokens, clean, entity_id, taken, found)
    return {item.entity_id: (item.start, item.end) for item in found}


def exact_mentions_page(
    text: str,
    entities: Sequence[tuple[str, str, Sequence[str]]],
    *,
    cursor: Mapping[str, object] | None,
    char_limit: int = _COOPERATIVE_MENTION_CHARS,
    pattern_limit: int = _EXACT_PATTERN_WORK_LIMIT,
) -> tuple[set[str], dict[str, int], bool, bool]:
    """Run a bounded number of literal name/alias searches.

    The old "page" bounded only document characters.  With 800 entities carrying
    1,365 short aliases each, one call still compiled and searched more than a
    million regular expressions before its caller could observe a deadline.
    ``entity`` and ``material`` are therefore first-class cooperative cursors.
    The checkpoint contains numbers only; names and aliases are re-read from the
    tenant-checked entity rows on every invocation.
    """

    body = text or ""
    raw = dict(cursor or {})
    try:
        start = _cursor_integer(raw.get("char", 0))
        entity_index = _cursor_integer(raw.get("entity", 0))
        material_index = _cursor_integer(raw.get("material", 0))
        bounded = max(256, min(_cursor_integer(char_limit), 65_536))
        work_limit = max(1, min(_cursor_integer(pattern_limit), 256))
    except (TypeError, ValueError):
        return set(), {"char": 0, "entity": 0, "material": 0}, False, False
    reset = {"char": 0, "entity": 0, "material": 0}
    if (
        not 0 <= start <= len(body)
        or not 0 <= entity_index <= len(entities)
        or material_index < 0
        or (start < len(body) and start % bounded != 0)
    ):
        return set(), reset, False, False
    if entity_index == len(entities):
        if start < len(body) or material_index:
            return set(), reset, False, False
    else:
        name, _entity_id, aliases = entities[entity_index]
        if material_index > len((name, *aliases)):
            return set(), reset, False, False
    if start == len(body) or not entities:
        return set(), {"char": len(body), "entity": 0, "material": 0}, False, True

    central_end = min(len(body), start + bounded)
    matched: set[str] = set()
    consumed = 0
    while consumed < work_limit and entity_index < len(entities):
        name, entity_id, aliases = entities[entity_index]
        materials = (name, *aliases)
        if material_index >= len(materials):
            entity_index += 1
            material_index = 0
            continue
        clean = str(materials[material_index] or "").strip()[:8_192]
        material_index += 1
        consumed += 1
        if len(clean) < 3:
            continue

        # One extra character on each side preserves both look-arounds even when
        # a possible match begins exactly at the owned window boundary.
        halo = len(clean) + 1
        chunk_start = max(0, start - halo)
        chunk_end = min(len(body), central_end + halo)
        chunk = body[chunk_start:chunk_end]
        pattern = re.compile(rf"(?<![\w.]){re.escape(clean)}", re.I)
        search_at = 0
        while True:
            found = pattern.search(chunk, search_at)
            if found is None:
                break
            following = found.end()
            right_is_word = following < len(chunk) and bool(re.fullmatch(r"(?u)\w", chunk[following]))
            right_is_dotted_identifier = (
                following + 1 < len(chunk)
                and chunk[following] == "."
                and bool(re.fullmatch(r"(?u)\w", chunk[following + 1]))
            )
            if right_is_word or right_is_dotted_identifier:
                search_at = max(found.end(), found.start() + 1)
                continue
            absolute = chunk_start + found.start()
            if start <= absolute < central_end:
                matched.add(str(entity_id))
                break
            search_at = max(found.end(), found.start() + 1)

    if entity_index >= len(entities):
        start = central_end
        entity_index = 0
        material_index = 0
    state = {"char": start, "entity": entity_index, "material": material_index}
    return matched, state, start < len(body), True


def inflected_mention_signature(name: str) -> tuple[str, ...] | None:
    """The exact token signature accepted by ``_inflected_spans``."""

    clean = " ".join(str(name or "").split()).strip()
    parts = clean.split()
    if len(parts) < 2:
        return None
    if not all(_ALL_CYRILLIC.match(part.casefold().replace("ё", "е")) for part in parts):
        return None
    words = tuple(_fold_word(part) for part in parts)
    if any(len(word) < _MIN_STEM for word in words):
        return None
    return words


def inflected_token_position_page(
    text: str,
    *,
    cursor: Mapping[str, object] | None,
    limit: int = 64,
    char_limit: int = _COOPERATIVE_MENTION_CHARS,
) -> tuple[list[tuple[int, int]], dict[str, int], bool, bool]:
    """Read one bounded page of ``_TOKEN_RE`` positions.

    Only numeric offsets cross the cooperative boundary.  A token longer than
    every accepted entity name is skipped in bounded pieces: no suffix of such a
    token may become a synthetic word after a resume, and none of its private
    characters has to be written to a durable checkpoint.
    """

    body = text or ""
    raw = dict(cursor or {})
    reset = {"char": 0, "skip": 0}
    try:
        position = _cursor_integer(raw.get("char", 0))
        skipping = _cursor_integer(raw.get("skip", 0))
        bounded_chars = max(64, min(_cursor_integer(char_limit), 65_536))
        bounded_tokens = max(1, min(_cursor_integer(limit), 256))
    except (TypeError, ValueError, AttributeError):
        return [], reset, False, False
    if not 0 <= position <= len(body) or skipping not in {0, 1}:
        return [], reset, False, False
    if skipping and (position == len(body) or not _is_token_char(body[position])):
        return [], reset, False, False

    page: list[tuple[int, int]] = []
    remaining = bounded_chars
    while position < len(body) and remaining > 0 and len(page) < bounded_tokens:
        before = position
        if skipping:
            ceiling = min(len(body), position + remaining)
            while position < ceiling and _is_token_char(body[position]):
                position += 1
            remaining -= max(1, position - before)
            if position < len(body) and _is_token_char(body[position]):
                continue
            skipping = 0
            continue

        ceiling = min(len(body), position + remaining)
        while position < ceiling and not _is_token_char(body[position]):
            position += 1
        remaining -= max(1, position - before)
        if position >= len(body) or remaining <= 0:
            break

        start = position
        token_ceiling = min(len(body), start + _MAX_COOPERATIVE_ENTITY_NAME_CHARS + 1)
        while position < token_ceiling and _is_token_char(body[position]):
            position += 1
        remaining = max(0, remaining - (position - start))
        if position - start > _MAX_COOPERATIVE_ENTITY_NAME_CHARS:
            skipping = int(position < len(body) and _is_token_char(body[position]))
            # A too-long token cannot equal entity material, but it still breaks
            # adjacency between the matchable tokens on its sides.
            page.append((start, start))
            continue
        page.append((start, position))

    state = {"char": position, "skip": skipping}
    return page, state, position < len(body), True


def _valid_token_positions(
    body: str,
    token_positions: Sequence[tuple[int, int]],
) -> list[tuple[int, int]] | None:
    if len(token_positions) > 256:
        return None
    clean: list[tuple[int, int]] = []
    previous_end = -1
    for raw in token_positions:
        if not isinstance(raw, (tuple, list)) or len(raw) != 2:
            return None
        try:
            start, end = _cursor_integer(raw[0]), _cursor_integer(raw[1])
        except (TypeError, ValueError):
            return None
        if not 0 <= start <= end <= len(body) or start < previous_end:
            return None
        if start == end:
            clean.append((start, end))
            previous_end = end
            continue
        if not _is_token_char(body[start]) or not _is_token_char(body[end - 1]):
            return None
        if start and _is_token_char(body[start - 1]):
            return None
        if end < len(body) and _is_token_char(body[end]):
            return None
        clean.append((start, end))
        previous_end = end
    return clean


def inflected_mentions_present_tokens(
    text: str,
    entities: Sequence[tuple[str, str]],
    token_positions: Sequence[tuple[int, int]],
) -> tuple[set[str], bool]:
    """Return candidates present in a bounded numeric token context."""

    body = text or ""
    positions = _valid_token_positions(body, token_positions)
    if positions is None:
        return set(), False
    tokens = ["\0" if start == end else _fold_word(body[start:end]) for start, end in positions]
    present: set[str] = set()
    for name, entity_id in entities:
        signature = inflected_mention_signature(name)
        if signature is None or len(signature) > len(tokens):
            continue
        width = len(signature)
        if any(tuple(tokens[index : index + width]) == signature for index in range(len(tokens) - width + 1)):
            present.add(str(entity_id))
    return present, True


def inflected_mentions_tokens(
    text: str,
    entities: Sequence[tuple[str, str]],
    token_positions: Sequence[tuple[int, int]],
    *,
    owned_start: int,
    owned_count: int,
) -> tuple[dict[str, tuple[int, int]], set[str], bool]:
    """Resolve longest-first matches in one fixed numeric token context.

    ``active`` includes halo matches so a longer name beginning to the left can
    continue to suppress its shorter suffix.  ``matches`` owns only starts in the
    requested central token interval, which makes adjacent pages equivalent to
    one unbounded ``inflected_mentions`` pass.
    """

    body = text or ""
    positions = _valid_token_positions(body, token_positions)
    try:
        first = _cursor_integer(owned_start)
        count = _cursor_integer(owned_count)
    except (TypeError, ValueError):
        return {}, set(), False
    if positions is None or first < 0 or count < 0 or first > len(positions):
        return {}, set(), False
    owned_end = min(len(positions), first + count)
    folded = ["\0" if start == end else _fold_word(body[start:end]) for start, end in positions]
    taken = [False] * len(positions)
    matches: dict[str, tuple[int, int]] = {}
    active: set[str] = set()
    for name, entity_id in sorted(entities, key=lambda pair: -len(pair[0])):
        signature = inflected_mention_signature(name)
        if signature is None or len(signature) > len(folded):
            continue
        width = len(signature)
        clean_id = str(entity_id)
        for index in range(len(folded) - width + 1):
            if tuple(folded[index : index + width]) != signature:
                continue
            if any(taken[index : index + width]):
                continue
            for occupied in range(index, index + width):
                taken[occupied] = True
            active.add(clean_id)
            if first <= index < owned_end:
                matches[clean_id] = (positions[index][0], positions[index + width - 1][1])
    return matches, active, True


def _cooperative_token_chunk(
    body: str,
    start: int,
    central_end: int,
) -> tuple[int, int, str]:
    """Return a bounded chunk whose first/last token is never a cut suffix.

    A previous left-halo repair stopped walking at ``start``.  When ``start`` was
    still inside a token longer than the halo, its suffix became a brand-new token
    and could produce a false person match.  Entity names are capped at 240 chars,
    so a token for which no boundary exists in that bounded distance cannot match
    and is safely excluded rather than exposed as a suffix.
    """

    size = len(body)
    chunk_start = max(0, start - _COOPERATIVE_INFLECTION_HALO)
    if chunk_start > 0 and _is_token_char(body[chunk_start - 1]) and _is_token_char(body[chunk_start]):
        lower = max(0, chunk_start - _MAX_COOPERATIVE_ENTITY_NAME_CHARS)
        probe = chunk_start
        while probe > lower and _is_token_char(body[probe - 1]):
            probe -= 1
        if probe == 0 or not _is_token_char(body[probe - 1]):
            chunk_start = probe
        else:
            # The leading token is longer than every entity material this reader
            # accepts. Skip its suffix, but never scan farther than this bounded
            # central window plus the right halo.
            ceiling = min(
                size,
                central_end + _COOPERATIVE_INFLECTION_HALO + _MAX_COOPERATIVE_ENTITY_NAME_CHARS,
            )
            probe = chunk_start
            while probe < ceiling and _is_token_char(body[probe]):
                probe += 1
            chunk_start = probe

    chunk_end = min(size, central_end + _COOPERATIVE_INFLECTION_HALO)
    if chunk_end < size and _is_token_char(body[chunk_end - 1]) and _is_token_char(body[chunk_end]):
        ceiling = min(size, chunk_end + _MAX_COOPERATIVE_ENTITY_NAME_CHARS)
        probe = chunk_end
        while probe < ceiling and _is_token_char(body[probe]):
            probe += 1
        if probe == size or not _is_token_char(body[probe]):
            chunk_end = probe
        else:
            # Exclude the incomplete trailing token. Walking back is bounded by
            # the maximum matchable entity material.
            probe = chunk_end
            lower = max(chunk_start, chunk_end - _MAX_COOPERATIVE_ENTITY_NAME_CHARS)
            while probe > lower and _is_token_char(body[probe - 1]):
                probe -= 1
            chunk_end = probe

    chunk_end = max(chunk_start, chunk_end)
    return chunk_start, chunk_end, body[chunk_start:chunk_end]


def inflected_mentions_present_page(
    text: str,
    entities: Sequence[tuple[str, str]],
    *,
    cursor: int,
    char_limit: int = _COOPERATIVE_MENTION_CHARS,
) -> tuple[set[str], int, bool, bool]:
    """Entities with an inflected occurrence in the bounded window plus halo.

    Unlike the final winner pass this deliberately has no occupancy mask.  It is
    used to retain one best rowid per token signature while candidate rows arrive
    in cooperative pages; overlap is resolved only after the complete candidate
    set for the window has been seen.
    """

    body = text or ""
    try:
        start = _cursor_integer(cursor)
        bounded = max(64, min(_cursor_integer(char_limit), 65_536))
    except (TypeError, ValueError):
        return set(), 0, False, False
    if not 0 <= start <= len(body):
        return set(), 0, False, False
    central_end = min(len(body), start + bounded)
    if start == len(body):
        return set(), len(body), False, True
    if not entities:
        return set(), central_end, central_end < len(body), True
    _chunk_start, _chunk_end, chunk = _cooperative_token_chunk(body, start, central_end)
    tokens = [_fold_word(match.group(0)) for match in _TOKEN_RE.finditer(chunk)]
    present: set[str] = set()
    for name, entity_id in entities:
        signature = inflected_mention_signature(name)
        if signature is None or len(signature) > len(tokens):
            continue
        width = len(signature)
        if any(tuple(tokens[index : index + width]) == signature for index in range(len(tokens) - width + 1)):
            present.add(str(entity_id))
    return present, central_end, central_end < len(body), True


def inflected_mentions_window(
    text: str,
    entities: Sequence[tuple[str, str]],
    *,
    cursor: int,
    char_limit: int = _COOPERATIVE_MENTION_CHARS,
) -> tuple[dict[str, tuple[int, int]], set[str], int, bool, bool]:
    """Resolve longest-first winners for one bounded window.

    The second result contains winners anywhere in the halo-bearing chunk.  A
    cooperative caller keeps their numeric rowids while it pages more candidates;
    otherwise a match beginning just left of the owned window could be forgotten
    before it suppresses an overlapping shorter name inside the window.
    """

    body = text or ""
    try:
        start = _cursor_integer(cursor)
        bounded = max(64, min(_cursor_integer(char_limit), 65_536))
    except (TypeError, ValueError):
        return {}, set(), 0, False, False
    if not 0 <= start <= len(body):
        return {}, set(), 0, False, False
    central_end = min(len(body), start + bounded)
    if start == len(body):
        return {}, set(), len(body), False, True
    if not entities:
        return {}, set(), central_end, central_end < len(body), True
    chunk_start, _chunk_end, chunk = _cooperative_token_chunk(body, start, central_end)
    tokens = [(m.start(), m.end(), _fold_word(m.group(0))) for m in _TOKEN_RE.finditer(chunk)]
    if not tokens:
        return {}, set(), central_end, central_end < len(body), True
    taken = [False] * len(chunk)
    found: list[Mention] = []
    for name, entity_id in sorted(entities, key=lambda pair: -len(pair[0])):
        clean = " ".join(str(name or "").split()).strip()
        if clean:
            _inflected_spans(chunk, tokens, clean, str(entity_id), taken, found)

    matches: dict[str, tuple[int, int]] = {}
    active: set[str] = set()
    for item in found:
        active.add(item.entity_id)
        absolute_start = chunk_start + item.start
        if start <= absolute_start < central_end:
            matches[item.entity_id] = (absolute_start, chunk_start + item.end)
    return matches, active, central_end, central_end < len(body), True


def inflected_mentions_page(
    text: str,
    entities: Sequence[tuple[str, str]],
    *,
    cursor: int,
    char_limit: int = _COOPERATIVE_MENTION_CHARS,
) -> tuple[dict[str, tuple[int, int]], int, bool, bool]:
    """A resumable window with the same longest-first, non-overlap rule.

    Each window recomputes a bounded halo in memory.  Thus a longer name beginning
    immediately before the cursor still owns the overlapping words, but no token,
    name or span is written to the durable worker checkpoint.  Entity search cards
    cap canonical names at 240 characters.  The compatibility reader uses a
    fixed 8 KiB halo; the storage worker itself carries the exact bounded token
    context instead of relying on a character-distance guess.
    """

    matches, _active, next_cursor, remains, valid = inflected_mentions_window(
        text,
        entities,
        cursor=cursor,
        char_limit=char_limit,
    )
    return matches, next_cursor, remains, valid


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
    # Свёрнутые слова текста считаются ОДИН раз на документ: их перебирает
    # `_inflected_spans` для каждого имени, а документы бывают в миллион знаков.
    tokens = [(m.start(), m.end(), _fold_word(m.group(0))) for m in _TOKEN_RE.finditer(body)]
    for name, entity_id in sorted(entities, key=lambda pair: -len(pair[0])):
        clean = " ".join(str(name or "").split()).strip()
        if not clean:
            continue
        # Косвенный падеж разбирается ПЕРВЫМ: он занимает слово целиком, а
        # буквальный проход ниже поставил бы разметку на первое слово имени и
        # оставил остальные снаружи — «Кублику» вместо «Кублику Александру
        # Юрьевичу». Занятые места уважают оба прохода.
        _inflected_spans(body, tokens, clean, entity_id, taken, found)
        needle = _search_form(clean)
        if len(needle) < _MIN_STEM:
            continue
        start = 0
        # Потолок на СБОР намеренно выше показанного: обрезать надо по положению в
        # тексте, а не по порядку разбора имён. Прежняя версия резала прямо здесь, и
        # документ с шестьюстами упоминаниями одного имени съедал весь запас — вторая
        # подтверждённая сущность не получала ни одной подсветки, хотя подпись обещала
        # «первые 500 упоминаний». Первые — значит первые по тексту.
        while len(found) < _COLLECT_LIMIT:
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
            tail = body[position + len(needle) : end].casefold()
            if len(tail) > _MAX_ENDING or tail not in _NOMINAL_TAILS:
                # Дописано не окончание, а продолжение другого слова: «работ»+«ать»,
                # «побед»+«или», «москв»+«ичи». См. `_NOMINAL_TAILS`.
                continue
            if any(taken[position:end]):
                continue
            for index in range(position, end):
                taken[index] = True
            found.append(Mention(start=position, end=end, name=clean, entity_id=entity_id))
    found.sort(key=lambda item: item.start)
    # Обрезка по положению, а не по порядку разбора: подпись обещает «первые N».
    return found[:_MAX_SPANS]
