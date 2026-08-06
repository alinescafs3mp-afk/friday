"""Privacy-safe Russian messages for errors crossing the HTTP boundary."""

from __future__ import annotations

import re

_EARLIEST_BOUNDARY = re.compile(r"earliest boundary is (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z)")


def relation_history_http_detail(error: BaseException) -> str:
    """Translate a snapshot refusal without echoing arbitrary internal data."""

    message = str(error)
    boundary = _EARLIEST_BOUNDARY.search(message)
    if boundary:
        return f"История связей полна только начиная с {boundary.group(1)}"
    lowered = message.casefold()
    if "merge" in lowered or "topology" in lowered or "слиян" in lowered:
        return "Исторический снимок недоступен после изменения слияния сущностей"
    if "recorded existence" in lowered or "entity existence" in lowered:
        return "Выбранная сущность ещё не существовала в историческом снимке"
    if "changed while" in lowered or "racing" in lowered:
        return "Исторический снимок изменился во время чтения — повторите запрос"
    return "Исторический снимок графа недоступен или неполон"
