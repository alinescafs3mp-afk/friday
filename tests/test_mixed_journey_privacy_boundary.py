from __future__ import annotations

from dataclasses import replace

import pytest
from test_owner_web_and_file_live_regressions import (
    OWNER,
    PUBLIC_FACT,
    PUBLIC_URL,
    _actor,
    _ScriptedModel,
    _store_generic_text,
    _SyntheticWebKernel,
)

from friday.agent_runtime import AgentRuntime
from friday.orchestration.current_file_web_query import extract_compare_current_file_public_web_query
from friday.orchestration.mixed_file_archive_web_query import (
    extract_authorized_archive_filename,
    extract_mixed_file_archive_public_web_query,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "архивной копией",
        "архивным слепком",
        "архивным снимком",
        "предыдущей копией",
        "образцом",
        "локальной копией",
    ],
)
async def test_archive_alias_with_malformed_raw_never_reaches_runtime_web(
    storage, settings, reference
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    document = _store_generic_text(settings, storage)
    kernel = _SyntheticWebKernel()
    request = (
        f"Сравни этот файл с {reference} raw_ABCDEF0123456789 и текущими публичными правилами в интернете."
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_ScriptedModel({document.marker: "Локальная сверка."}, web_fact=PUBLIC_FACT, web_url=PUBLIC_URL),
        kernel=kernel,
    )
    response = await runtime.chat(OWNER, request, actor=_actor(), attachments=[document.attachment])
    assert kernel.calls == []
    assert response["web_evidence_status"] != "sourced"


@pytest.mark.parametrize(
    "reference", ["архивной копией", "архивным слепком", "архивным снимком", "предыдущей копией"]
)
def test_exact_archive_alias_still_selects_the_whole_filename(reference) -> None:
    request = (
        f'Сравни этот файл с {reference} "private deal.txt" и текущими публичными правилами в интернете.'
    )
    assert extract_authorized_archive_filename(request) == "private deal.txt"
    assert extract_mixed_file_archive_public_web_query(request) == "текущими публичными правилами"


@pytest.mark.parametrize(
    "marker", ["raw_ABCDEF0123456789", "RAW_0123456789abcdef", "raw_0123456789abcde", "raw_0123456789abcdef0"]
)
def test_generic_file_web_extractor_rejects_raw_marker_even_without_known_archive_grammar(marker) -> None:
    request = f"Сравни этот файл с образцом {marker} и текущими публичными правилами в интернете."
    assert extract_compare_current_file_public_web_query(request) == ""


@pytest.mark.parametrize("public_identifier", ["raw_input", "raw_data"])
def test_public_programming_term_is_not_a_private_raw_identity(public_identifier) -> None:
    request = f"Сравни этот файл с публичными правилами Python {public_identifier} в интернете."
    assert public_identifier in extract_compare_current_file_public_web_query(request)


def test_mixed_filename_and_public_identifier_are_not_misclassified_as_raw_ids() -> None:
    request = (
        "Сравни этот файл с архивной копией raw_input.txt и публичными правилами Python raw_data в интернете."
    )
    assert extract_authorized_archive_filename(request) == "raw_input.txt"
    assert extract_mixed_file_archive_public_web_query(request) == "публичными правилами Python raw_data"
