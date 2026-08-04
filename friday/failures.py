"""Текст сбоя, который можно хранить долго и показывать наружу.

Разница между тем, что видно в ответе сейчас, и тем, что ложится в базу
навсегда, — не формальная. Немедленный `ToolResult` уже безопасен: «Tool failed:
RuntimeError», без подробностей. А в заявку (`action_approvals.error`) писался
полный текст исключения, и оттуда он отдаётся через `GET /api/me/approvals`
целиком.

Что в этот текст попадает на практике: адрес с токеном в query-строке, заголовок
`Authorization`, абсолютный путь к файлу человека, кусок документа, попавший в
сообщение об ошибке разбора. Проба Сола воспроизвела это синтетическим
`credential=…` и путём.

Здесь остаётся ровно столько, сколько нужно, чтобы понять, ЧТО случилось: имя
класса исключения и вычищенный остаток. Всё, что похоже на секрет, путь или
адрес, заменяется меткой — не вырезается молча, чтобы по записи было видно, что
там что-то было.
"""

from __future__ import annotations

import re

#: Сколько текста сбоя хранить. Длинный текст — это почти всегда трассировка или
#: содержимое документа, а не объяснение.
_MAX_FAILURE_TEXT = 300

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Адреса целиком: в них живут и токены, и внутренние имена хостов.
    (re.compile(r"https?://\S+", re.IGNORECASE), "‹адрес›"),
    # Пары «ключ=значение», где ключ выглядит как секрет.
    (
        # Схема («Bearer», «Basic») съедается вместе со значением: без неё в
        # записи оставалось «‹секрет› sk-secret-value-here» — то есть сам секрет.
        re.compile(
            r"(?i)\b(?:token|key|secret|password|passwd|pwd|credential|authorization|api[_-]?key)"
            r"\s*[=:]\s*(?:bearer\s+|basic\s+|token\s+)?\S+"
        ),
        "‹секрет›",
    ),
    # Абсолютные пути: и Unix, и Windows.
    (re.compile(r"(?:/[\w.\-]+){2,}/?"), "‹путь›"),
    (re.compile(r"[A-Za-z]:\\[^\s\"']+"), "‹путь›"),
    # Длинные строки без пробелов — обычно ключ, хеш или base64.
    (re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b"), "‹длинная строка›"),
)


def safe_failure_text(error: BaseException | str, *, limit: int = _MAX_FAILURE_TEXT) -> str:
    """Имя класса сбоя и вычищенный остаток — пригодно для долгого хранения."""

    if isinstance(error, BaseException):
        head = type(error).__name__
        tail = str(error)
    else:
        head, _, tail = str(error).partition(":")
        head = head.strip() or "Error"
        tail = tail.strip()
    for pattern, replacement in _REDACTIONS:
        tail = pattern.sub(replacement, tail)
    tail = " ".join(tail.split())
    if not tail:
        return head
    if len(tail) > limit:
        # Обрез называется вслух: молча укороченный текст читается как полный.
        tail = tail[: limit - 1].rstrip() + "…"
    return f"{head}: {tail}"
