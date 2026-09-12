"""A literal filename does not hide a claim that a carrier was delivered."""

import pytest

from friday.agent_runtime import (
    _SUPPORTED_FILE_COMPLETION,
    _named_attachment_carrier_completion,
    _runtime_unconfirmed_supported_deed,
)


@pytest.mark.parametrize("filename", ["sample.txt", "notes_v2.txt", "отчёт.v2.csv"])
@pytest.mark.parametrize("style", ["{}", "`{}`"])
@pytest.mark.parametrize("suffix", ["прикреплён вам в чат", "отправлен тебе", "загружен в чат"])
def test_named_file_delivery_needs_an_output_receipt(filename, style, suffix) -> None:
    answer = f"Файл {style.format(filename)} {suffix}."
    assert _runtime_unconfirmed_supported_deed(
        answer,
        requested_effects=frozenset(),
        has_file=False,
        reminder_succeeded=False,
        read_only_attachment_review=True,
        read_only_attachment_descriptors=(filename,),
    )


@pytest.mark.parametrize("filename", ["sample.txt", "notes_v2.txt", "отчёт.v2.csv"])
@pytest.mark.parametrize("style", ["{}", "`{}`"])
def test_named_authenticated_input_is_not_a_delivered_output(filename, style) -> None:
    answer = f"Файл {style.format(filename)} прикреплён к вопросу этого же хода."
    assert not _runtime_unconfirmed_supported_deed(
        answer,
        requested_effects=frozenset(),
        has_file=False,
        reminder_succeeded=False,
        read_only_attachment_review=True,
        read_only_attachment_descriptors=(filename,),
    )


@pytest.mark.parametrize(
    "answer",
    [
        "Файл sample.txt. Прикреплённый значок описан отдельно.",
        "Файл sample.txt\nПрикреплённый значок описан отдельно.",
        "Файл sample.txt? Прикреплённый значок описан отдельно.",
        "Файл\nsample.txt прикреплённый значок описан отдельно.",
    ],
)
def test_filename_bridge_preserves_sentence_boundaries(answer) -> None:
    assert _SUPPORTED_FILE_COMPLETION.search(answer) is None
    assert _named_attachment_carrier_completion(answer) is None


@pytest.mark.parametrize(
    "answer",
    [
        "Файл sample.txt не прикреплён вам в чат.",
        "Файл sample.txt прикреплён вам в чат?",
        "Если файл sample.txt будет прикреплён вам в чат, вы увидите значок.",
        "> Файл sample.txt прикреплён вам в чат.",
        "Файл sample.txt содержит сведения об отправленных письмах. "
        "Прикреплённый вам в чат значок имеет другой цвет.",
    ],
)
def test_named_carrier_keeps_nonactual_and_reported_boundaries(answer) -> None:
    assert not _runtime_unconfirmed_supported_deed(
        answer,
        requested_effects=frozenset(),
        has_file=False,
        reminder_succeeded=False,
        read_only_attachment_review=True,
        read_only_attachment_descriptors=("sample.txt",),
    )


def test_independent_delivery_after_reported_text_still_needs_receipt() -> None:
    assert _runtime_unconfirmed_supported_deed(
        "> Файл sample.txt не прикреплён.\nФайл sample.txt прикреплён вам в чат.",
        requested_effects=frozenset(),
        has_file=False,
        reminder_succeeded=False,
        read_only_attachment_review=True,
        read_only_attachment_descriptors=("sample.txt",),
    )
