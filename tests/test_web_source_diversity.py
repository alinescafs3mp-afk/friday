from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.web_source_diversity import (
    MAX_RESEARCH_SOURCES,
    WEB_SOURCE_DIVERSITY_SCHEMA,
    WebSourceDiversityError,
    WebSourceDiversityNote,
    WebSourceDiversityV1,
    build_web_source_diversity,
    validate_web_source_diversity,
)


def _raw(urls: list[str]) -> dict[str, object]:
    return {
        "schema": WEB_SOURCE_DIVERSITY_SCHEMA,
        "diversity_id": "diversity:2026-09-04:1",
        "authenticated_turn_id": "turn:2026-09-04:1",
        "source_urls": urls,
    }


def test_empty_admitted_set_is_explicitly_empty() -> None:
    result = build_web_source_diversity(_raw([]))

    assert isinstance(result, WebSourceDiversityV1)
    assert result.admitted_source_count == 0
    assert result.unique_host_count == 0
    assert result.duplicate_host_count == 0
    assert result.unique_hosts == ()
    assert result.diversity_note is WebSourceDiversityNote.EMPTY


def test_one_host_is_single_host_and_duplicates_are_extra_occurrences() -> None:
    result = build_web_source_diversity(
        _raw(
            [
                "https://Docs.Example.com/one",
                "https://docs.example.com/two",
                "https://docs.example.com/three",
            ]
        )
    )

    assert result.unique_hosts == ("docs.example.com",)
    assert result.unique_host_count == 1
    assert result.duplicate_host_count == 2
    assert result.diversity_note is WebSourceDiversityNote.SINGLE_HOST


def test_two_hosts_with_no_majority_are_diverse_even_with_duplicate_hosts() -> None:
    result = build_web_source_diversity(
        _raw(
            [
                "https://one.example.com/a",
                "https://two.example.com/a",
                "https://one.example.com/b",
                "https://two.example.com/b",
            ]
        )
    )

    assert result.unique_host_count == 2
    assert result.duplicate_host_count == 2
    assert result.diversity_note is WebSourceDiversityNote.DIVERSE


def test_one_host_majority_is_concentrated() -> None:
    result = build_web_source_diversity(
        _raw(
            [
                "https://one.example.com/a",
                "https://one.example.com/b",
                "https://one.example.com/c",
                "https://two.example.com/a",
            ]
        )
    )

    assert result.unique_host_count == 2
    assert result.duplicate_host_count == 2
    assert result.diversity_note is WebSourceDiversityNote.CONCENTRATED


def test_host_identity_is_casefolded_hostname_without_registrable_domain_lookup() -> None:
    result = build_web_source_diversity(
        _raw(
            [
                "https://WWW.Example.CO.UK/first",
                "https://other.example.co.uk/second",
            ]
        )
    )

    assert result.unique_hosts == ("www.example.co.uk", "other.example.co.uk")
    assert result.diversity_note is WebSourceDiversityNote.DIVERSE


def test_optional_query_plan_is_context_only_and_never_creates_hosts() -> None:
    urls = ["https://one.example.com/a", "https://two.example.com/b"]
    without_context = build_web_source_diversity(_raw(urls))
    with_context = build_web_source_diversity(
        {
            **_raw(urls),
            "query_plan": ["private report.pdf", "this attached file"],
        }
    )

    assert with_context == without_context


def test_source_mapping_and_round_trip_are_supported_without_retaining_urls() -> None:
    result = build_web_source_diversity(
        {
            "diversity_id": "diversity:1",
            "authenticated_turn_id": "turn:1",
            "sources": [
                {"canonical_url": "https://one.example.com/report"},
                {"url": "https://two.example.com/notes"},
            ],
        }
    )

    assert build_web_source_diversity(result.to_mapping()) == result
    assert "source_urls" not in result.to_mapping()
    assert "https://one.example.com/report" not in result.to_mapping()


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1:8080/private",
        "https://10.0.0.4/results",
        "https://localhost/private",
        "https://docs.example.test/results",
        "https://service.internal/results",
        "https://user:password@example.com/results",
        "https://example.com/results?api_key=secret",
    ),
)
def test_private_and_credential_bearing_urls_fail_closed(url: str) -> None:
    with pytest.raises(WebSourceDiversityError, match="source_urls"):
        build_web_source_diversity(_raw([url]))


def test_global_ip_literal_is_public_but_private_ip_literal_is_not() -> None:
    result = build_web_source_diversity(_raw(["https://8.8.8.8/public"]))
    assert result.unique_hosts == ("8.8.8.8",)

    with pytest.raises(WebSourceDiversityError):
        build_web_source_diversity(_raw(["https://192.168.1.1/private"]))


def test_admitted_sources_are_bounded_by_the_shared_research_limit() -> None:
    urls = [f"https://host{index}.example.com/source" for index in range(MAX_RESEARCH_SOURCES + 1)]

    with pytest.raises(WebSourceDiversityError, match="source_urls_bound"):
        build_web_source_diversity(_raw(urls))


def test_diversity_contract_is_frozen_and_validator_is_fail_closed() -> None:
    result = build_web_source_diversity(_raw(["https://one.example.com/source"]))
    with pytest.raises(FrozenInstanceError):
        result.admitted_source_count = 3  # type: ignore[misc]

    malformed = _raw(["https://one.example.com/source"])
    malformed["unknown"] = "not admitted"
    assert validate_web_source_diversity(malformed) is False
