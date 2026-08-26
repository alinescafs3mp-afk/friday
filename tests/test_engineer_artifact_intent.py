"""Current-speech authority contracts for Engineer artifact decompilation."""

from __future__ import annotations

import pytest

from friday.organs.engineer.targets import (
    artifact_decompile_request_is_atomic,
    requests_artifact_decompile,
)


@pytest.mark.parametrize(
    "speech",
    (
        "Декомпилируй его",
        "Декомпилируй этот файл",
        "Проанализируй бинарник",
        "Пожалуйста, проанализируй этот исполняемый файл",
        "Можешь декомпилировать этот файл?",
        "Файл уже загружен\nТеперь декомпилируй его",
        "Reverse engineer this executable",
        "Please decompile this binary",
        "Could you analyze this executable?",
        "Декомпилируй sample.exe",
        "декомпилируй foo.dll",
        "decompile sample.exe",
        "decompile the attached sample.exe",
        "The artifact is uploaded\nNow reverse engineer it",
        "Он сказал, что файл готов. А теперь декомпилируй его",
        "В отчёте приведена цитата: «декомпилируй его».\nА теперь декомпилируй этот файл",
    ),
)
def test_direct_current_artifact_decompile_requests_are_admitted(speech: str) -> None:
    assert requests_artifact_decompile(speech) is True


@pytest.mark.parametrize(
    "speech",
    (
        # Quoted/code/blockquote payloads are data.
        "«Декомпилируй его»",
        '"Reverse engineer this executable"',
        "`Декомпилируй этот файл`",
        "```\nReverse engineer this executable\n```",
        "> Проанализируй бинарник",
        # Reported/example/meta text never becomes current authority.
        "Он сказал, декомпилируй его",
        "Он сказал. Декомпилируй его",
        "The report says. Reverse engineer this executable",
        "Система сообщила, декомпилируй его",
        "В документе написано:\nДекомпилируй этот файл",
        "Это пример команды, декомпилируй его",
        "Repeat after me, reverse engineer this executable",
        "Что значит, reverse engineer this executable",
        # Conditional or cancelled actions fail closed.
        "Если файл вредоносный, декомпилируй его",
        "Reverse engineer this executable if it is unsigned",
        "Декомпилируй его, но не делай этого",
        "Декомпилируй его, но не декомпилируй",
        "Reverse engineer this executable, but don't do it",
        "Не декомпилируй этот файл",
        "Do not analyze this binary",
        "Декомпилируй не этот файл, а другой",
        "Decompile not this file but the other one",
        "Декомпилируй любой файл кроме этого",
        "Decompile any file except this one",
        "Он сказал: декомпилируй sample.exe",
        "`decompile sample.exe`",
        '"декомпилируй sample.exe"',
        # Capability and historical questions are not effect requests.
        "Ты умеешь декомпилировать файлы?",
        "Можешь ли ты декомпилировать бинарники?",
        "Есть ли у тебя инструмент для декомпиляции?",
        "Can you reverse engineer executables?",
        "Ты декомпилировала этот файл?",
        "Ты проанализировала бинарник",
        "Did you reverse engineer this executable?",
        "You analyzed this binary",
        # Ordinary document analysis and missing targets own no binary route.
        "Проанализируй этот документ",
        "Analyze this report",
        "Декомпилируй",
    ),
)
def test_inert_or_noncurrent_artifact_language_is_rejected(speech: str) -> None:
    assert requests_artifact_decompile(speech) is False


def test_atomic_decompile_request_has_no_model_remainder() -> None:
    assert artifact_decompile_request_is_atomic("Декомпилируй его.") is True
    assert artifact_decompile_request_is_atomic("Please decompile this binary?") is True


def test_compound_or_prior_clause_is_not_consumed_as_atomic() -> None:
    assert artifact_decompile_request_is_atomic("Декомпилируй его и объясни алгоритм") is False
    assert artifact_decompile_request_is_atomic("Файл загружен. Теперь декомпилируй его") is False
