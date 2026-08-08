"""Small fail-closed repairs for explicit text-composition contracts.

The model remains responsible for prose.  This module only fixes a bounded set
of structurally unambiguous slips after generation: duplicate required
literals, unrequested simple emphasis, exact-list overflow, a quoted
explanation line, and a named literal left outside explicitly requested angle
brackets.  Ambiguous requests and answers are returned byte-for-byte unchanged.
"""

from __future__ import annotations

import re

_MAX_REQUEST_CHARS = 4_000
_MAX_ANSWER_CHARS = 16_000

_DIRECT_COMPOSITION = re.compile(
    r"^\s*(?:"
    r"ответь|сформируй|составь|дай|напиши|верни|сделай|оформи|подготовь|"
    r"включи|выдели|помести|отправь|создай|вставь|покажи|подчеркни|"
    r"передай|экранируй|"
    r"финальн\w*\s+доставк\w*\s+должн\w*|"
    r"answer|return|write|format|compose|create"
    r")\b",
    re.IGNORECASE,
)
_NAMED_LITERAL = re.compile(
    r"\b(?:маркер|идентификатор|токен|marker|identifier|token)\b"
    r"(?:\s+(?:со\s+значением|значением|долж\w*\s+быть|равн\w*|value))?"
    r"\s*(?:[:=—–-]\s*)?"
    r"(?P<literal>"
    r"`[^`\n]{1,160}`|«[^»\n]{1,160}»|“[^”\n]{1,160}”|"
    r"\"[^\"\n]{1,160}\"|'[^'\n]{1,160}'|[^\s,;.!?()]{1,160}"
    r")",
    re.IGNORECASE,
)
_LIST_CUE = re.compile(r"\b(?:спис\w*|пункт\w*|элемент\w*|list|items?)\b", re.IGNORECASE)
_WORD_CUE = re.compile(r"\b(?:слов\w*|токен\w*|words?|tokens?)\b", re.IGNORECASE)
_QUOTE_CUE = re.compile(r"\b(?:цитат\w*|blockquote|quote)\b", re.IGNORECASE)
_EXPLANATION_CUE = re.compile(
    r"\b(?:пояснен\w*|поясн\w*|комментар\w*|объяснен\w*|explanation|comment)\b",
    re.IGNORECASE,
)
_ANGLE_CUE = re.compile(
    r"(?:\bуглов\w*\s+скоб\w*\b|"
    r"\bсимвол\w*\b.{0,40}\bменьше\b.{0,40}\bбольше\b|"
    r"\bangle\s+brackets?\b|\bless[- ]than\b.{0,40}\bgreater[- ]than\b)",
    re.IGNORECASE | re.DOTALL,
)
_ONE_SENTENCE_CUE = re.compile(
    r"\b(?:одно|одну|одна|one)\b.{0,32}\b(?:предложен\w*|фраз\w*|строк\w*|sentence|phrase|line)\b",
    re.IGNORECASE,
)
_SINGLE_SENTENCE_CONTRACT = re.compile(
    r"\b(?:"
    r"(?:одно|единственное)\s+(?:[\w-]+\s+){0,3}предложение|"
    r"(?:одним|единственным)\s+(?:[\w-]+\s+){0,3}предложением|"
    r"(?:one|(?:a\s+)?single)\s+(?:[\w-]+\s+){0,3}sentence"
    r")\b",
    re.IGNORECASE,
)
_NON_SINGLE_SENTENCE_CONTRACT = re.compile(
    r"\b(?:"
    r"(?:2|3|4|5|6|7|8|9|10|два|две|двух|двумя|три|тр[её]х|четыре|пять|"
    r"шесть|семь|восемь|девять|десять|несколько|пара)"
    r"\s+(?:[\w-]+\s+){0,3}предложен\w*|"
    r"(?:two|three|four|five|six|seven|eight|nine|ten|multiple|several)"
    r"\s+(?:[\w-]+\s+){0,3}sentences?"
    r")\b",
    re.IGNORECASE,
)
_POST_SINGLE_SENTENCE_NEGATION = re.compile(
    r"^\s*[,;:—–-]?\s*(?:не\s+(?:надо|нужн\w*|следует|пиши|делай|треб\w*)|"
    r"отмен\w*|вместо\b|rather\s+than|not\s+(?:needed|required|wanted))",
    re.IGNORECASE,
)
_NEGATED_SINGLE_SENTENCE_PREFIX = re.compile(
    r"(?:\bне|\bбез|\bnot|\bwithout)(?:[\s,]+[\w-]+){0,6}\s*$",
    re.IGNORECASE,
)
_POST_SINGLE_SENTENCE_REVISION = re.compile(
    r"^.{0,64}\b(?:а|но|лучше|вместо|but|actually|rather|instead)\b.{0,40}"
    r"\b(?:2|3|4|5|6|7|8|9|10|два|две|три|четыре|пять|несколько|"
    r"two|three|four|five|multiple|several)\b",
    re.IGNORECASE,
)
_METALINGUISTIC_SENTENCE_MENTION = re.compile(
    r"\b(?:разбор|значени|определени|обсуждени|анализ)\w*\s+"
    r"(?:термин|фраз|выражени|словосочетани)\w*.{0,32}$",
    re.IGNORECASE,
)
_FACTUAL_SOURCE_CUE = re.compile(
    r"\b(?:файл\w*|вложен\w*|документ\w*|таблиц\w*|архив\w*|данн\w*|"
    r"факт\w*|источник\w*|запис\w*|file|attachment|document|table|archive|data|facts?|source)\b",
    re.IGNORECASE,
)
_NEGATED_SHAPE = re.compile(
    r"\b(?:не|без|not|without)\s+(?:спис\w*|пункт\w*|цитат\w*|углов\w*|"
    r"list|items?|quote|angle)\b",
    re.IGNORECASE,
)
_REPEAT_LITERAL_CUE = re.compile(
    r"\b(?:повтор\w*|дважды|два\s+раза|несколько\s+раз|repeat\w*|twice|two\s+times)\b",
    re.IGNORECASE,
)
_BOLD_REQUEST_CUE = re.compile(r"\b(?:жирн\w*|полужирн\w*|bold|strong)\b", re.IGNORECASE)
_ITALIC_REQUEST_CUE = re.compile(r"\b(?:курсив\w*|italic)\b", re.IGNORECASE)
_GENERIC_EMPHASIS_REQUEST_CUE = re.compile(
    r"\b(?:выдел\w*|подчерк\w*|emphasis|emphasize\w*|markdown|маркдаун\w*|форматирован\w*)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE_CUE = re.compile(
    r"(?:\bне|\bбез|\bnot|\bwithout)(?:\s+[\w-]+){0,2}\s*$",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\([^\n)]*\)")
_SIMPLE_EMPHASIS = re.compile(
    r"(?P<bold_star>\*\*(?P<bold_star_text>[^\s*\n](?:[^*\n]*[^\s*\n])?)\*\*)|"
    r"(?P<bold_under>(?<!_)__(?!_)(?P<bold_under_text>[^\s_\n](?:[^_\n]*[^\s_\n])?)__(?!_))|"
    r"(?P<italic_star>(?<!\*)\*(?![\s*])(?P<italic_star_text>[^\s*\n](?:[^*\n]*[^\s*\n])?)\*(?!\*))|"
    r"(?P<italic_under>(?<![\w_])_(?![\s_])(?P<italic_under_text>[^\s_\n](?:[^_\n]*[^\s_\n])?)_(?![\w_]))"
)
_LIST_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<prefix>[-*+•]|\d{1,2}[.)])(?P<space>[ \t]+)(?P<value>\S.*)$")
_QUOTED_ATOM = re.compile(
    r"(?P<open>«|“|\"|')(?P<target>[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{0,63})(?P<close>»|”|\"|')"
)
_SAFE_WORD_ATOM = re.compile(r"(?<![\w-])[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{0,79}(?![\w-])")
_HTML_TAG_NAMES = frozenset({"a", "b", "blockquote", "code", "em", "i", "pre", "s", "strong", "u"})
_TERMINAL_SENTENCE_BOUNDARY = re.compile(r"[.!?…]+(?=\s|$)")
_COMMON_ABBREVIATION_PREFIX = re.compile(
    r"(?:\b(?:т\.\s*е|т\.\s*к|т\.\s*д|т\.\s*п|и\.\s*т\.\s*д|"
    r"и\.\s*т\.\s*п|e\.g|i\.e|etc|mr|mrs|ms|dr|prof|vs|ул|стр|рис|г|д|им|см|мин|сек)|"
    r"(?:\b[A-Za-zА-Яа-яЁё]\.){1,4}[A-Za-zА-Яа-яЁё])$",
    re.IGNORECASE,
)
_LONE_ATOM_PREFIX = re.compile(r"(?:^|\s)[A-Za-zА-Яа-яЁё0-9]$")
_NUMERIC_ENUMERATOR_PREFIX = re.compile(r"(?:^|[:;]\s)\d{1,3}$")
_ROMAN_ENUMERATOR_PREFIX = re.compile(r"(?:^|\s)[IVXLCDM]{1,8}$")
_PAREN_ENUMERATOR_PREFIX = re.compile(r"(?:^|\s)\((?:\d{1,3}|[IVXLCDM]{1,8})\)$")
_SHORT_MEASURE_PREFIX = re.compile(r"\b\d+(?:[.,]\d+)?\s+[A-Za-zА-Яа-яЁё]{1,4}$")

_NUMBER_WORDS = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одну": 1,
    "one": 1,
    "два": 2,
    "две": 2,
    "двух": 2,
    "two": 2,
    "три": 3,
    "трех": 3,
    "трёх": 3,
    "three": 3,
    "четыре": 4,
    "четырех": 4,
    "четырёх": 4,
    "four": 4,
    "пять": 5,
    "пяти": 5,
    "five": 5,
    "шесть": 6,
    "шести": 6,
    "six": 6,
    "семь": 7,
    "семи": 7,
    "seven": 7,
    "восемь": 8,
    "восьми": 8,
    "eight": 8,
    "девять": 9,
    "девяти": 9,
    "nine": 9,
    "десять": 10,
    "десяти": 10,
    "ten": 10,
}
_NUMBER_TOKEN = re.compile(
    r"(?<![\w-])(?:[1-9]|10)(?![\w-])|\b(?:" + "|".join(_NUMBER_WORDS) + r")\b",
    re.IGNORECASE,
)


def _inside_reported_quote(text: str, position: int) -> bool:
    for opening, closing in (("«", "»"), ("“", "”"), ('"', '"'), ("'", "'")):
        left = text.rfind(opening, 0, position)
        if left < 0:
            continue
        right = text.find(closing, left + len(opening))
        if left < position < right:
            return True
    return False


def _unwrap_literal(raw: str) -> tuple[str, bool]:
    pairs = {"`": "`", "«": "»", "“": "”", '"': '"', "'": "'"}
    closing = pairs.get(raw[:1])
    quoted = bool(closing and raw.endswith(closing) and len(raw) > 2)
    return (raw[1:-1] if quoted else raw), quoted


def _single_named_literal(request: str) -> str | None:
    candidates: set[str] = set()
    for match in _NAMED_LITERAL.finditer(request):
        if _inside_reported_quote(request, match.start()):
            continue
        literal, quoted = _unwrap_literal(match.group("literal"))
        literal = literal.strip()
        if (
            not literal
            or len(literal) > 160
            or any(character.isspace() for character in literal)
            or any(character in literal for character in "<>&/\\")
            or not re.search(r"[A-Za-zА-Яа-яЁё0-9]", literal)
            or (not quoted and not re.search(r"[0-9_.:-]", literal))
            or request.count(literal) != 1
        ):
            continue
        candidates.add(literal)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _instruction_before_literal(request: str) -> str:
    match = _NAMED_LITERAL.search(request)
    return request[: match.start()] if match is not None else request


def _requested_list_size(request: str) -> int | None:
    request = _instruction_before_literal(request)
    sizes: set[int] = set()
    for cue in _LIST_CUE.finditer(request):
        window = request[max(0, cue.start() - 72) : min(len(request), cue.end() + 72)]
        for match in _NUMBER_TOKEN.finditer(window):
            token = match.group(0).casefold()
            sizes.add(int(token) if token.isdigit() else _NUMBER_WORDS[token])
    return next(iter(sizes)) if len(sizes) == 1 else None


def _is_word_list(request: str) -> bool:
    request = _instruction_before_literal(request)
    list_matches = list(_LIST_CUE.finditer(request))
    word_matches = list(_WORD_CUE.finditer(request))
    for list_match in list_matches:
        for word_match in word_matches:
            left = min(list_match.end(), word_match.end())
            right = max(list_match.start(), word_match.start())
            if right - left <= 72 and not re.search(r"[.!?\n]", request[left:right]):
                return True
    return False


def _enumerates_word_values(request: str) -> bool:
    """Conservatively detect a user-owned value set that must not lose an item."""

    before_literal = _NAMED_LITERAL.split(request, maxsplit=1)[0]
    if re.search(r":\s*[^:;\n]+(?:,|;)\s*[^:;\n]+", before_literal):
        return True
    quoted_values = re.findall(r"(?:«[^»\n]+»|“[^”\n]+”|\"[^\"\n]+\"|'[^'\n]+')", before_literal)
    return len(quoted_values) >= 2


def _literal_occurrences(text: str, literal: str) -> list[re.Match[str]]:
    return list(re.finditer(rf"(?<![\w-]){re.escape(literal)}(?![\w-])", text))


def _has_positive_cue(request: str, pattern: re.Pattern[str]) -> bool:
    for match in pattern.finditer(request):
        prefix = request[max(0, match.start() - 48) : match.start()]
        if not _NEGATION_BEFORE_CUE.search(prefix):
            return True
    return False


def _requests_literal_repetition(request: str) -> bool:
    return _has_positive_cue(request, _REPEAT_LITERAL_CUE)


def _single_emphasis_target(request: str) -> str | None:
    """Extract one explicit, locally bound quoted word to emphasize."""

    cue_pattern = re.compile(
        r"\b(?:выдел\w*|подчерк\w*|жирн\w*|полужирн\w*|курсив\w*|"
        r"emphasis|emphasize\w*|bold|italic)\b",
        re.IGNORECASE,
    )
    pairs = {"«": "»", "“": "”", '"': '"', "'": "'"}
    candidates: set[str] = set()
    for quoted in _QUOTED_ATOM.finditer(request):
        if pairs.get(str(quoted.group("open"))) != str(quoted.group("close")):
            continue
        if _inside_reported_quote(request, quoted.start()):
            continue
        window_start = max(0, quoted.start() - 96)
        prefix = request[window_start : quoted.start()]
        cues = list(cue_pattern.finditer(prefix))
        if not cues:
            continue
        cue = cues[-1]
        between = prefix[cue.end() :]
        if re.search(r"[.!?\n]", between):
            continue
        cue_position = window_start + cue.start()
        if _inside_reported_quote(request, cue_position):
            continue
        before_cue = request[max(0, cue_position - 48) : cue_position]
        if _NEGATION_BEFORE_CUE.search(before_cue):
            continue
        candidates.add(str(quoted.group("target")))
    return next(iter(candidates)) if len(candidates) == 1 else None


def _dedupe_named_literal(request: str, answer: str, literal: str) -> str:
    occurrences = _literal_occurrences(answer, literal)
    if len(occurrences) < 2 or _requests_literal_repetition(request):
        return answer

    sentinel = "\x00"
    if sentinel in answer:
        return answer
    seen = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        return match.group(0) if seen == 1 else sentinel

    result = re.sub(rf"(?<![\w-]){re.escape(literal)}(?![\w-])", replace, answer)
    while sentinel in result:
        position = result.index(sentinel)
        line_start = result.rfind("\n", 0, position) + 1
        line_end = result.find("\n", position + 1)
        line_end = len(result) if line_end < 0 else line_end
        if not result[line_start:position].strip() and not result[position + 1 : line_end].strip():
            if line_end < len(result):
                result = f"{result[:line_start]}{result[line_end + 1 :]}"
            elif line_start:
                result = result[: line_start - 1]
            else:
                result = ""
            continue
        left = position
        while left > 0 and result[left - 1] in " \t":
            left -= 1
        right = position + 1
        while right < len(result) and result[right] in " \t":
            right += 1
        previous = result[left - 1] if left else "\n"
        following = result[right] if right < len(result) else "\n"
        joiner = "" if following in ",.;:!?…" or previous in "\n([{<«“\"'" or following == "\n" else " "
        result = f"{result[:left]}{joiner}{result[right:]}"
    return result


def _parsed_exact_list(request: str, answer: str) -> tuple[int, list[re.Match[str]]] | None:
    expected = _requested_list_size(request)
    lines = answer.splitlines()
    if expected is None or len(lines) != expected or any(not line.strip() for line in lines):
        return None
    parsed = [_LIST_LINE.fullmatch(line) for line in lines]
    if any(match is None for match in parsed):
        return None
    matches = [match for match in parsed if match is not None]
    numbered = [match.group("prefix")[0].isdigit() for match in matches]
    if any(numbered) and not all(numbered):
        return None
    if all(numbered):
        values = [int(match.group("prefix")[:-1]) for match in matches]
        if values != list(range(1, expected + 1)):
            return None
    return expected, matches


def _repair_word_list_values(request: str, answer: str, literal: str) -> str:
    """Project a free-form exact word list onto one generated atom per item."""

    if not _is_word_list(request) or _enumerates_word_values(request):
        return answer
    parsed = _parsed_exact_list(request, answer)
    if parsed is None or len(_literal_occurrences(answer, literal)) != 1:
        return answer
    _expected, matches = parsed
    if (
        "`" in answer
        or "<" in answer
        or ">" in answer
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
    ):
        return answer
    marker_rows = [
        index
        for index, match in enumerate(matches)
        if len(_literal_occurrences(match.group("value"), literal)) == 1
    ]
    if len(marker_rows) != 1:
        return answer
    marker_row = marker_rows[0]
    selected: list[str] = []
    used: set[str] = set()
    for index, match in enumerate(matches):
        if index == marker_row:
            selected.append(literal)
            continue
        atoms = _SAFE_WORD_ATOM.findall(match.group("value"))
        atom = next((value for value in atoms if value.casefold() not in used), "")
        if not atom:
            return answer
        selected.append(atom)
        used.add(atom.casefold())
    if all(match.group("value") == value for match, value in zip(matches, selected, strict=True)):
        return answer
    return "\n".join(
        f"{match.group('indent')}{match.group('prefix')}{match.group('space')}{value}"
        for match, value in zip(matches, selected, strict=True)
    )


def _repair_blockquoted_exact_list(request: str, answer: str, literal: str) -> str:
    """Remove one unrequested blockquote wrapper around an otherwise exact list."""

    expected = _requested_list_size(request)
    if (
        expected is None
        or _QUOTE_CUE.search(request)
        or _enumerates_word_values(request)
        or len(_literal_occurrences(answer, literal)) != 1
        or "`" in answer
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
    ):
        return answer
    raw_lines = answer.splitlines()
    quoted = [re.fullmatch(r"[ \t]*>[ \t]+(?P<inner>\S.*)", line) for line in raw_lines]
    if not raw_lines or any(match is None for match in quoted):
        return answer
    inner_lines = [match.group("inner") for match in quoted if match is not None]
    if any(line.lstrip().startswith(">") for line in inner_lines):
        return answer
    list_rows: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(inner_lines):
        match = _LIST_LINE.fullmatch(line)
        if match is not None:
            list_rows.append((index, match))
    if len(list_rows) != expected:
        return answer
    list_matches = [match for _index, match in list_rows]
    numbered = [match.group("prefix")[0].isdigit() for match in list_matches]
    if any(numbered) and not all(numbered):
        return answer
    if all(numbered):
        values = [int(match.group("prefix")[:-1]) for match in list_matches]
        if values != list(range(1, expected + 1)):
            return answer
    first_list_index = list_rows[0][0]
    last_list_index = list_rows[-1][0]
    if [index for index, _match in list_rows] != list(range(first_list_index, last_list_index + 1)):
        return answer
    prefix = inner_lines[:first_list_index]
    suffix = inner_lines[last_list_index + 1 :]
    if (
        len(prefix) > 1
        or len(suffix) > 1
        or sum(len(line) for line in [*prefix, *suffix]) > 512
        or any("<" in line or ">" in line or re.match(r"\s*(?:#{1,6}\s|\|)", line) for line in inner_lines)
    ):
        return answer
    rendered = [
        f"{match.group('indent')}{match.group('prefix')}{match.group('space')}{match.group('value')}"
        for match in list_matches
    ]
    if prefix:
        first = list_matches[0]
        rendered[0] = (
            f"{first.group('indent')}{first.group('prefix')}{first.group('space')}"
            f"{' '.join(prefix)} {first.group('value').lstrip()}"
        )
    if suffix:
        last = list_matches[-1]
        rendered[-1] = (
            f"{last.group('indent')}{last.group('prefix')}{last.group('space')}"
            f"{last.group('value').rstrip()} {' '.join(suffix)}"
        )
    result = "\n".join(rendered)
    return result if len(_literal_occurrences(result, literal)) == 1 else answer


def _repair_list_overflow(request: str, answer: str, literal: str) -> str:
    expected = _requested_list_size(request)
    if expected is None or len(_literal_occurrences(answer, literal)) != 1:
        return answer
    lines = answer.splitlines()
    overflow = lines[-1].strip() if lines else ""
    if (
        len(lines) != expected + 1
        or not overflow
        or _LIST_LINE.fullmatch(lines[-1]) is not None
        or len(_literal_occurrences(overflow, literal)) != 1
    ):
        return answer
    parsed = [_LIST_LINE.fullmatch(line) for line in lines[:-1]]
    if any(match is None for match in parsed):
        return answer
    matches = [match for match in parsed if match is not None]
    numbered = [match.group("prefix")[0].isdigit() for match in matches]
    if any(numbered) and not all(numbered):
        return answer
    if all(numbered):
        values = [int(match.group("prefix")[:-1]) for match in matches]
        if values != list(range(1, expected + 1)):
            return answer
    word_list = _is_word_list(request)
    if word_list:
        if _enumerates_word_values(request) or any(
            len(match.group("value").split()) != 1 for match in matches
        ):
            return answer
        replacement = literal
    else:
        replacement = f"{matches[-1].group('value').rstrip()} {overflow}"
    last = matches[-1]
    lines[-2] = f"{last.group('indent')}{last.group('prefix')}{last.group('space')}{replacement}"
    return "\n".join(lines[:-1])


def _repair_quoted_explanation(request: str, answer: str, literal: str) -> str:
    if not (_QUOTE_CUE.search(request) and _EXPLANATION_CUE.search(request)):
        return answer
    quote_counts = _requested_count_near(request, _QUOTE_CUE)
    if quote_counts and quote_counts != {1}:
        return answer
    if answer.count(literal) != 1:
        return answer
    lines = answer.splitlines()
    if len(lines) != 2 or not all(re.match(r"^[ \t]*>[ \t]+\S", line) for line in lines):
        return answer
    lines[1] = re.sub(r"^([ \t]*)>[ \t]+", r"\1", lines[1], count=1)
    return "\n".join(lines)


def _requested_count_near(request: str, cue_pattern: re.Pattern[str]) -> set[int]:
    request = _instruction_before_literal(request)
    sizes: set[int] = set()
    for cue in cue_pattern.finditer(request):
        window = request[max(0, cue.start() - 40) : min(len(request), cue.end() + 40)]
        for match in _NUMBER_TOKEN.finditer(window):
            token = match.group(0).casefold()
            sizes.add(int(token) if token.isdigit() else _NUMBER_WORDS[token])
    return sizes


def _repair_angle_literal(request: str, answer: str, literal: str) -> str:
    angle_cues = [
        match
        for match in _ANGLE_CUE.finditer(request)
        if not _inside_reported_quote(request, match.start())
        and not _NEGATION_BEFORE_CUE.search(request[max(0, match.start() - 48) : match.start()])
    ]
    if not angle_cues or not _ONE_SENTENCE_CUE.search(request):
        return answer
    explicit_targets = {
        str(match.group("target"))
        for match in _QUOTED_ATOM.finditer(request)
        if not _inside_reported_quote(request, match.start())
        and any(abs(match.start() - cue.start()) <= 120 for cue in angle_cues)
    }
    explicit_targets.update(
        match.group(1)
        for match in re.finditer(
            r"\b(?:слово|литерал|word|literal)\s+([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{0,63})\b",
            request,
            re.IGNORECASE,
        )
        if not _inside_reported_quote(request, match.start())
        and any(abs(match.start() - cue.start()) <= 120 for cue in angle_cues)
    )
    if any(target.casefold() != literal.casefold() for target in explicit_targets):
        return answer
    if (
        "\n" in answer
        or "\r" in answer
        or "<" in answer
        or ">" in answer
        or "`" in answer
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
        or len(_literal_occurrences(answer, literal)) != 1
        or literal.casefold() in _HTML_TAG_NAMES
    ):
        return answer
    occurrence = _literal_occurrences(answer, literal)[0]
    if _inside_reported_quote(answer, occurrence.start()):
        return answer
    previous = answer[occurrence.start() - 1 : occurrence.start()]
    following = answer[occurrence.end() : occurrence.end() + 1]
    if (previous and previous in "/\\") or (following and following in "/\\"):
        return answer
    return f"{answer[: occurrence.start()]}<{occurrence.group(0)}>{answer[occurrence.end() :]}"


def _repair_single_sentence_punctuation(
    request: str,
    answer: str,
    literal: str | None,
) -> str:
    """Join plainly separated clauses when exactly one sentence was requested."""

    sentence_contracts = [
        match
        for match in _SINGLE_SENTENCE_CONTRACT.finditer(request)
        if not _inside_reported_quote(request, match.start())
        and not _NEGATION_BEFORE_CUE.search(request[max(0, match.start() - 48) : match.start()])
        and not _NEGATED_SINGLE_SENTENCE_PREFIX.search(request[max(0, match.start() - 96) : match.start()])
        and not _METALINGUISTIC_SENTENCE_MENTION.search(request[: match.start()])
        and not _POST_SINGLE_SENTENCE_NEGATION.search(request[match.end() : match.end() + 64])
        and not _POST_SINGLE_SENTENCE_REVISION.search(request[match.end() : match.end() + 112])
    ]
    if len(sentence_contracts) != 1 or any(
        not _inside_reported_quote(request, match.start())
        for match in _NON_SINGLE_SENTENCE_CONTRACT.finditer(request)
    ):
        return answer
    if (
        "\n" in answer
        or "\r" in answer
        or "`" in answer
        or any(character in answer for character in '«»“”"')
        or "~" in answer
        or _MARKDOWN_LINK.search(answer)
        or not _has_only_balanced_simple_emphasis(answer)
        or re.match(r"\s*(?:#{1,6}\s|>\s|[-+•]\s|\d{1,2}[.)]\s|\|)", answer)
    ):
        return answer

    # The angle-literal repair deliberately runs first.  Permit exactly that
    # request-owned carrier, but reject every other raw angle/HTML construct.
    visible = answer
    if "<" in visible or ">" in visible:
        wrapped = f"<{literal}>" if literal is not None else ""
        angle_cues = [
            match
            for match in _ANGLE_CUE.finditer(request)
            if not _inside_reported_quote(request, match.start())
            and not _NEGATION_BEFORE_CUE.search(request[max(0, match.start() - 48) : match.start()])
        ]
        if (
            not wrapped
            or not angle_cues
            or literal is None
            or literal.casefold() in _HTML_TAG_NAMES
            or visible.count(wrapped) != 1
        ):
            return answer
        visible = visible.replace(wrapped, literal, 1)
        if "<" in visible or ">" in visible:
            return answer

    boundaries = list(_TERMINAL_SENTENCE_BOUNDARY.finditer(answer))
    if len(boundaries) <= 1:
        return answer
    if boundaries[-1].end() != len(answer.rstrip()):
        return answer
    intermediate = boundaries[:-1]
    if any(match.group(0) != "." for match in intermediate):
        return answer
    if any(
        _COMMON_ABBREVIATION_PREFIX.search(answer[: match.start()].rstrip())
        or _LONE_ATOM_PREFIX.search(answer[: match.start()].rstrip())
        or _NUMERIC_ENUMERATOR_PREFIX.search(answer[: match.start()].rstrip())
        or _ROMAN_ENUMERATOR_PREFIX.search(answer[: match.start()].rstrip())
        or _PAREN_ENUMERATOR_PREFIX.search(answer[: match.start()].rstrip())
        or _SHORT_MEASURE_PREFIX.search(answer[: match.start()].rstrip())
        for match in intermediate
    ):
        return answer

    pieces: list[str] = []
    cursor = 0
    for match in intermediate:
        pieces.append(answer[cursor : match.start()])
        pieces.append(";")
        cursor = match.end()
    pieces.append(answer[cursor:])
    repaired = "".join(pieces)
    return repaired if len(_TERMINAL_SENTENCE_BOUNDARY.findall(repaired)) <= 1 else answer


def _has_unresolved_emphasis_marker(text: str) -> bool:
    for position, character in enumerate(text):
        if character == "*":
            line_start = text.rfind("\n", 0, position) + 1
            before_on_line = text[line_start:position]
            following = text[position + 1 : position + 2]
            previous = text[position - 1 : position]
            if not before_on_line.strip() and following in " \t":
                continue
            if previous.isspace() and following.isspace():
                continue
            return True
        if character == "_":
            previous = text[position - 1 : position]
            following = text[position + 1 : position + 2]
            if previous.isalnum() and following.isalnum():
                continue
            return True
    return False


def _has_only_balanced_simple_emphasis(text: str) -> bool:
    """Return whether every emphasis marker belongs to a parsed simple span."""

    masked = list(text)
    for match in _SIMPLE_EMPHASIS.finditer(text):
        content = next(
            (
                str(match.group(name))
                for name in (
                    "bold_star_text",
                    "bold_under_text",
                    "italic_star_text",
                    "italic_under_text",
                )
                if match.group(name) is not None
            ),
            "",
        )
        if not content or _has_unresolved_emphasis_marker(content):
            return False
        for position in range(match.start(), match.end()):
            if masked[position] != "\n":
                masked[position] = " "
    return not _has_unresolved_emphasis_marker("".join(masked))


def _requested_emphasis_styles(request: str) -> set[str]:
    styles: set[str] = set()
    bold_requested = _has_positive_cue(request, _BOLD_REQUEST_CUE)
    italic_requested = _has_positive_cue(request, _ITALIC_REQUEST_CUE)
    if bold_requested:
        styles.add("bold")
    if italic_requested:
        styles.add("italic")
    if not styles and _has_positive_cue(request, _GENERIC_EMPHASIS_REQUEST_CUE):
        styles.update(("bold", "italic"))
    return styles


def _repair_missing_requested_emphasis(
    request: str,
    answer: str,
    literal: str | None,
) -> str:
    target = _single_emphasis_target(request)
    if target is None or target == literal or "\n" in answer or "\r" in answer:
        return answer
    bold_requested = _has_positive_cue(request, _BOLD_REQUEST_CUE)
    italic_requested = _has_positive_cue(request, _ITALIC_REQUEST_CUE)
    generic_requested = _has_positive_cue(request, _GENERIC_EMPHASIS_REQUEST_CUE)
    if (bold_requested and italic_requested) or not (bold_requested or italic_requested or generic_requested):
        return answer
    if (
        "`" in answer
        or "<" in answer
        or ">" in answer
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
    ):
        return answer
    occurrences = _literal_occurrences(answer, target)
    if len(occurrences) != 1:
        return answer
    match = occurrences[0]
    opening, closing = ("*", "*") if italic_requested and not bold_requested else ("**", "**")
    return f"{answer[: match.start()]}{opening}{match.group(0)}{closing}{answer[match.end() :]}"


def _repair_unrequested_emphasis(request: str, answer: str, literal: str | None) -> str:
    del literal
    if "`" in answer or _MARKDOWN_LINK.search(answer) or "***" in answer or "___" in answer:
        return answer
    matches = list(_SIMPLE_EMPHASIS.finditer(answer))
    if not matches:
        return answer

    masked = list(answer)
    spans: list[tuple[int, int, str, str]] = []
    for match in matches:
        if match.group("bold_star") is not None:
            style, content = "bold", match.group("bold_star_text")
        elif match.group("bold_under") is not None:
            style, content = "bold", match.group("bold_under_text")
        elif match.group("italic_star") is not None:
            style, content = "italic", match.group("italic_star_text")
        else:
            style, content = "italic", match.group("italic_under_text")
        if not content or _has_unresolved_emphasis_marker(content):
            return answer
        spans.append((match.start(), match.end(), content, style))
        for position in range(match.start(), match.end()):
            if masked[position] != "\n":
                masked[position] = " "
    if _has_unresolved_emphasis_marker("".join(masked)):
        return answer

    requested = _requested_emphasis_styles(request)
    pieces: list[str] = []
    cursor = 0
    changed = False
    for start, end, content, style in spans:
        pieces.append(answer[cursor:start])
        if style in requested:
            pieces.append(answer[start:end])
        else:
            pieces.append(content)
            changed = True
        cursor = end
    pieces.append(answer[cursor:])
    return "".join(pieces) if changed else answer


def repair_explicit_text_shape(request: str, answer: str) -> str:
    """Apply bounded high-confidence shape repairs or return ``answer`` unchanged."""

    if (
        not isinstance(request, str)
        or not isinstance(answer, str)
        or not request
        or not answer
        or len(request) > _MAX_REQUEST_CHARS
        or len(answer) > _MAX_ANSWER_CHARS
        or not _DIRECT_COMPOSITION.search(request)
        or _NEGATED_SHAPE.search(request)
    ):
        return answer
    literal = _single_named_literal(request)
    factual_source = bool(_FACTUAL_SOURCE_CUE.search(request))
    candidate = (
        _dedupe_named_literal(request, answer, literal)
        if literal is not None and not factual_source
        else answer
    )
    candidate = _repair_unrequested_emphasis(request, candidate, literal)
    if literal is not None and not factual_source:
        repairs = (
            _repair_blockquoted_exact_list,
            _repair_word_list_values,
            _repair_list_overflow,
            _repair_quoted_explanation,
            _repair_angle_literal,
        )
        for repair in repairs:
            repaired = repair(request, candidate, literal)
            if repaired != candidate:
                candidate = repaired
                break
    if not factual_source:
        candidate = _repair_single_sentence_punctuation(request, candidate, literal)
    candidate = _repair_missing_requested_emphasis(request, candidate, literal)
    candidate = _repair_unrequested_emphasis(request, candidate, literal)
    return candidate
