"""Regression boundary for B correlation metadata and file routing."""

from __future__ import annotations

import base64

import pytest

from friday.agent_runtime import (
    _attachment_body_query_surface,
    _filename_clue_request,
    _project_attachments_for_request,
    _without_attachment_request_correlation,
)
from friday.server import _current_turn_file_attachment
from tools import synthetic_live_battery as battery

OWNER = "synthetic-b-correlation-owner"

_B07_CASES = tuple(
    case
    for case in battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS["B"]))
    if case.pass_index == 7
)

_FILENAME_COLLISIONS = (
    "Найди в графе событие, валидное в течение 15 июня 2024 года. Проверка SYN-B02-15.",
    "Отдай ссылочный токен [SYN-LINK-B05-07](https://example.invalid/b/07) "
    "в поддерживаемой разметке. Проверка SYN-B05-07.",
    "Сделай курсивной строку SYN-TELEGRAM-B10-04 в безопасном Markdown. Проверка SYN-B10-04.",
    "Вставь SYN-TELEGRAM-B10-13 в нейтральное Markdown-предложение. Проверка SYN-B10-13.",
    "Подчеркни словом готово значение SYN-TELEGRAM-B10-16 в коротком ответе. Проверка SYN-B10-16.",
)


def _owned_attachment(*, filename: str, text: str) -> dict[str, object]:
    return _current_turn_file_attachment(
        filename=filename,
        file_ingestion={
            "extraction": {
                "success": True,
                "text_success": True,
                "chars": len(text),
            }
        },
        raw={
            "raw_content": text,
            "metadata_json": {
                "filename": filename,
                "uploaded_by": OWNER,
                "extraction_success": True,
                "text_extraction_success": True,
            },
        },
    )


def test_b07_fixture_slice_is_complete() -> None:
    assert [case.id for case in _B07_CASES] == [f"SYN-B07-{index:02d}" for index in range(1, 21)]


@pytest.mark.parametrize("case", _B07_CASES, ids=lambda case: case.id)
def test_b07_terminal_correlation_never_becomes_a_body_target(case) -> None:
    document = battery._case_document(case)
    assert document is not None
    text = base64.b64decode(document["content_base64"]).decode("utf-8")
    marker = battery._marker(case, "ATTACHMENT")
    assert marker in text and case.id not in text

    projected, state = _project_attachments_for_request(
        case.question,
        [_owned_attachment(filename=document["filename"], text=text)],
    )

    assert state.status != "not_found"
    assert projected and marker in str(projected[0].get("transient_text") or "")
    assert case.id not in _attachment_body_query_surface(case.question)


@pytest.mark.parametrize("question", _FILENAME_COLLISIONS)
def test_b_filename_collisions_do_not_claim_file_selection(question: str) -> None:
    assert _filename_clue_request(question) is None


def test_real_approximate_filename_navigation_is_preserved() -> None:
    pair = _filename_clue_request("Скажи, Цветков Никита Андреевич с АК-74 №609416 — БПЛА штат, он там есть?")
    natural = _filename_clue_request("я раньше присылал файл, в alias666 посмотри что внутри")
    explicit_graph_name = _filename_clue_request("я раньше загружал файл, в графе посмотри что внутри")

    assert pair is not None and pair.display_clue == "БПЛА штат"
    assert natural is not None and natural.display_clue == "alias666"
    assert explicit_graph_name is not None and explicit_graph_name.display_clue == "графе"


def test_non_file_action_scopes_do_not_claim_file_selection() -> None:
    for question in (
        "Найди в графе событие",
        "Найди в интернете свежие новости",
        "В интернете найди, я кидал уже",
        "В сети посмотри, я уже присылал",
        "В онлайн найди, я загружал раньше",
    ):
        assert _filename_clue_request(question) is None


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "Найди CASE-404 в документе. Проверка TRACE-501.",
            "Найди CASE-404 в документе",
        ),
        (
            "Найди CASE-404 в документе. Проверка: TRACE_501.",
            "Найди CASE-404 в документе",
        ),
        (
            "Найди CASE-404 в документе. Контроль RUN-77.",
            "Найди CASE-404 в документе",
        ),
        (
            "Найди CASE-404 в документе. Идентификатор запроса REQUEST-42.",
            "Найди CASE-404 в документе",
        ),
        (
            "Найди CASE-404 в документе. Request ID: TRACE-501.",
            "Найди CASE-404 в документе",
        ),
    ],
)
def test_terminal_correlation_labels_are_generic_and_bounded(question: str, expected: str) -> None:
    assert _without_attachment_request_correlation(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "Проверка результата CASE-404 в документе ещё продолжается.",
        "Найди в файле «Проверка SYN-B07-01».",
        "Найди идентификатор RUN-77 в этом файле.",
        "Найди RUN-77 в файле Проверка TRACE-501.",
        "Найди CASE-404. Проверка TRACE-501 и RUN-77.",
    ],
)
def test_nonterminal_and_body_identifiers_are_preserved(question: str) -> None:
    assert _without_attachment_request_correlation(question) == question


def test_real_body_identifier_remains_a_required_absence_anchor() -> None:
    attachment = _owned_attachment(
        filename="lookup.txt",
        text="Здесь нет искомого идентификатора.",
    )

    _projected, state = _project_attachments_for_request(
        "Найди идентификатор RUN-77 в этом файле.",
        [attachment],
    )

    assert state.status == "not_found" and state.scan_complete


def test_footer_is_removed_without_erasing_the_real_body_identifier() -> None:
    attachment = _owned_attachment(filename="lookup.txt", text="Искомый идентификатор: CASE-404.")

    projected, state = _project_attachments_for_request(
        "Найди CASE-404 в документе. Проверка TRACE-501.",
        [attachment],
    )

    assert state.status != "not_found"
    assert projected and "CASE-404" in str(projected[0].get("transient_text") or "")


@pytest.mark.parametrize(
    "question",
    [
        "Прочитай base64-документ и сообщи токен.",
        "Прочитай base64-документ текущего запроса и сообщи синтетический canary.",
        "Найди токен внутри приложенного TXT.",
        "Найди токен внутри вложенного TXT.",
        "Найди токен внутри прикреплённого TXT.",
        "Найди токен внутри загруженного TXT.",
        "Покажи точный токен из байтов вложения.",
    ],
)
def test_source_qualifiers_cannot_prove_body_absence(question: str) -> None:
    projected, state = _project_attachments_for_request(
        question, [_owned_attachment(filename="source.txt", text="Токен: SYN-CANARY-77.")]
    )
    assert state.status != "not_found"
    assert projected and "SYN-CANARY-77" in str(projected[0].get("transient_text") or "")


@pytest.mark.parametrize(
    "target",
    [
        "BASE64-SECRET",
        "CASE-404",
        "RUN-77",
        "TXT-CANARY",
        "API",
        "«base64-документ»",
        "«TXT»",
        "«байтов»",
        "Чайковского",
        "Иванова",
        "Байтова",
        "иванова",
        "Синтетического",
    ],
)
def test_source_qualifier_fix_keeps_real_lookup_targets(target: str) -> None:
    _, state = _project_attachments_for_request(
        f"Найди {target} в приложенном файле.",
        [_owned_attachment(filename="source.txt", text="Здесь только нейтральный текст.")],
    )
    assert state.status == "not_found" and state.scan_complete
