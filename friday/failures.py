"""Класс сбоя, который можно хранить долго и показывать наружу.

Разница между тем, что видно в ответе сейчас, и тем, что ложится в базу
навсегда, — не формальная. Немедленный `ToolResult` уже безопасен: «Tool failed:
RuntimeError», без подробностей. А в заявку (`action_approvals.error`) писался
полный текст исключения, и оттуда он отдаётся через `GET /api/me/approvals`
целиком.

Сообщение исключения недоверенное целиком: URL/ключ/путь можно вычистить
регулярным выражением, но имя человека или фрагмент документа на естественном
языке неотличимы от диагностического текста. Поэтому долговечная запись хранит
только строго проверенное имя класса.
"""

from __future__ import annotations

import re

_MAX_FAILURE_TEXT = 300
_FAILURE_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}(?:Error|Exception|Failure|Timeout)$")


def safe_failure_text(error: BaseException | str, *, limit: int = _MAX_FAILURE_TEXT) -> str:
    """Вернуть только allowlisted имя класса, никогда сообщение исключения."""

    del limit  # kept for the public signature; payload length is no longer configurable
    if isinstance(error, BaseException):
        candidate = type(error).__name__
    else:
        candidate = str(error).partition(":")[0].strip()
    return candidate if _FAILURE_CLASS_RE.fullmatch(candidate) else "Error"
