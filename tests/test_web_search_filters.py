"""Strict site/freshness contracts for the existing web-search provider fan-out."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

import friday.web_surfer as web_surfer_module
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import (
    SEARCH_DOMAIN_LIST_MAX,
    SEARCH_FRESHNESS_VALUES,
    AllProvidersRefusedError,
    FreshnessUnavailableError,
    ProviderRefusedError,
    SearchFilterUnavailableError,
    SearchResult,
    UnsupportedSearchFilterError,
    WebSurfer,
    normalize_search_domains,
    normalize_search_filters,
    normalize_search_freshness,
    normalize_search_language,
    normalize_search_region,
    normalize_search_site,
)


def test_web_search_schema_is_closed_and_declares_both_filters() -> None:
    schema = ExecutionKernel()._tools["web_search"].parameters  # noqa: SLF001

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["query"]
    assert schema["properties"]["freshness"]["enum"] == list(SEARCH_FRESHNESS_VALUES)
    assert "site" in schema["properties"]
    assert "pattern" in schema["properties"]["site"]
    for field in ("include_domains", "exclude_domains"):
        domain_list = schema["properties"][field]
        assert domain_list["type"] == "array"
        assert domain_list["maxItems"] == SEARCH_DOMAIN_LIST_MAX
        assert domain_list["uniqueItems"] is True
        assert "pattern" in domain_list["items"]
    assert schema["properties"]["lang"]["pattern"] == r"^(?:[A-Za-z]{2})?$"
    assert schema["properties"]["region"]["pattern"] == r"^(?:[A-Za-z]{2})?$"


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        (" Docs.Example.Test. ", "docs.example.test"),
        ("пример.рф", "xn--e1afmkfd.xn--p1ai"),
        ("", ""),
    ],
)
def test_site_is_canonicalized_before_it_can_reach_a_provider(raw: str, canonical: str) -> None:
    assert normalize_search_site(raw) == canonical


@pytest.mark.parametrize(
    "site",
    [
        "https://docs.example.test",
        "docs.example.test/path",
        "person@docs.example.test",
        "docs.example.test:443",
        "docs.example.test?q=private",
        "*.example.test",
        "single-label",
        "127.0.0.1",
        "bad_label.example",
        "double..example",
    ],
)
def test_site_rejects_everything_except_a_bare_dns_hostname(site: str) -> None:
    with pytest.raises(ValueError, match="bare DNS hostname") as caught:
        normalize_search_site(site)

    assert site not in str(caught.value), "validation error echoed a private domain or URL"


@pytest.mark.parametrize("freshness", ["hour", "all", "Week", " week ", None, 7])
def test_freshness_is_a_closed_enum(freshness: object) -> None:
    with pytest.raises(ValueError, match="freshness must be one of"):
        normalize_search_freshness(freshness)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("ru", "ru"), ("EN", "en"), ("uk", "uk"), ("", "")],
)
def test_language_is_a_closed_iso_code(raw: str, canonical: str) -> None:
    assert normalize_search_language(raw) == canonical


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("ru", "ru"), ("US", "us"), ("ua", "ua"), ("", "")],
)
def test_region_is_a_closed_iso_code(raw: str, canonical: str) -> None:
    assert normalize_search_region(raw) == canonical


@pytest.mark.parametrize("raw", ["zz", "en-US", " en", "en ", 7, None])
def test_language_rejects_unknown_or_non_exact_codes_without_echo(raw: object) -> None:
    with pytest.raises(ValueError) as caught:
        normalize_search_language(raw)  # type: ignore[arg-type]
    assert str(raw) not in str(caught.value)


@pytest.mark.parametrize("raw", ["uk", "zz", "russia", " ru", "ru ", 7, None])
def test_region_rejects_aliases_and_unknown_codes_without_echo(raw: object) -> None:
    with pytest.raises(ValueError) as caught:
        normalize_search_region(raw)  # type: ignore[arg-type]
    assert str(raw) not in str(caught.value)


def test_domain_lists_are_canonical_bounded_and_unique() -> None:
    assert normalize_search_domains(
        ["Docs.Example.Test.", "пример.рф"],
        filter_name="include_domains",
    ) == ("docs.example.test", "xn--e1afmkfd.xn--p1ai")

    private_canary = "Private-Customer.Example.Test."
    invalid_lists: list[object] = [
        private_canary,
        [""],
        ["https://private-customer.example.test/path"],
        [private_canary, private_canary.casefold().rstrip(".")],
        ["a.example.test"] * (SEARCH_DOMAIN_LIST_MAX + 1),
        [7],
    ]
    for raw in invalid_lists:
        with pytest.raises(ValueError) as caught:
            normalize_search_domains(raw, filter_name="include_domains")  # type: ignore[arg-type]
        assert private_canary not in str(caught.value)
        assert private_canary.casefold().rstrip(".") not in str(caught.value)


def test_domain_filter_combinations_reject_ambiguity_but_allow_nested_exclusions() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        normalize_search_filters(
            site="docs.example.test",
            include_domains=["docs.example.test"],
        )
    with pytest.raises(ValueError, match="cannot cover"):
        normalize_search_filters(
            include_domains=["docs.example.test"],
            exclude_domains=["example.test"],
        )

    assert normalize_search_filters(
        include_domains=["example.test"],
        exclude_domains=["ads.example.test"],
    )[1:3] == (("example.test",), ("ads.example.test",))


@pytest.mark.parametrize(
    ("freshness", "boundary"),
    [
        ("day", "2026-08-05"),
        ("week", "2026-07-30"),
        ("month", "2026-07-06"),
        ("year", "2025-08-06"),
    ],
)
def test_freshness_windows_have_frozen_calendar_boundaries(
    monkeypatch,
    freshness: str,
    boundary: str,
) -> None:
    class FrozenDateTime:
        @classmethod
        def now(cls, zone):
            assert zone is UTC
            return datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(web_surfer_module, "datetime", FrozenDateTime)
    assert web_surfer_module._freshness_after(freshness) == boundary  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        {"site": "https://private.example.test/path"},
        {"freshness": "hour"},
        {"include_domains": "private.example.test"},
        {"include_domains": None},
        {"exclude_domains": ["https://private.example.test/path"]},
        {"exclude_domains": None},
        {"lang": "zz"},
        {"region": "uk"},
        {"site": "private.example.test", "include_domains": ["private.example.test"]},
    ],
)
async def test_invalid_filters_stop_before_provider_selection(settings, filters) -> None:
    surfer = WebSurfer(settings)
    selected = False

    def provider_chain(*_args, **_kwargs):
        nonlocal selected
        selected = True
        return []

    surfer._provider_chain = provider_chain  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(ValueError):
        await surfer.search("needle", **filters)

    assert selected is False


@pytest.mark.asyncio
async def test_provider_stubs_pin_only_verified_freshness_parameters(settings, monkeypatch) -> None:
    """No socket is opened: every provider is exercised through one MockTransport."""

    monkeypatch.setattr(web_surfer_module, "_freshness_after", lambda _freshness: "2026-07-30")
    seen: dict[str, dict[str, object]] = {}
    yandex_xml = base64.b64encode(
        b"<yandexsearch><response><results><grouping><group><doc>"
        b"<url>https://docs.example.test/yandex</url><title>Yandex</title>"
        b"</doc></group></grouping></results></response></yandexsearch>"
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "searchapi.api.cloud.yandex.net":
            seen["yandex"] = json.loads(request.content)
            return httpx.Response(200, json={"rawData": yandex_xml})
        if host == "api.search.brave.com":
            seen["brave"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Brave",
                                "url": "https://docs.example.test/brave",
                                "description": "stub",
                            }
                        ]
                    }
                },
            )
        if host == "api.tavily.com":
            seen["tavily"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Tavily",
                            "url": "https://docs.example.test/tavily",
                            "content": "stub",
                        }
                    ]
                },
            )
        if host == "google.serper.dev":
            seen["serper"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {
                            "title": "Serper",
                            "link": "https://docs.example.test/serper",
                            "snippet": "stub",
                        }
                    ]
                },
            )
        if host == "search.brave.com":
            seen["brave-html"] = dict(request.url.params)
            return httpx.Response(
                200,
                text=(
                    "<div id='results'><div class='snippet' data-type='web'>"
                    "<a href='https://docs.example.test/brave-html'>Brave HTML</a>"
                    "<div class='snippet-description'>stub</div></div></div>"
                ),
            )
        if host == "html.duckduckgo.com":
            seen["duckduckgo"] = dict(request.url.params)
            return httpx.Response(
                200,
                text=(
                    "<div class='result'><a class='result__a' "
                    "href='https://docs.example.test/ddg'>DuckDuckGo</a>"
                    "<div class='result__snippet'>stub</div></div>"
                ),
            )
        if host.endswith(".wikipedia.org"):
            seen.setdefault("wikipedia", dict(request.url.params))
            return httpx.Response(
                200,
                json={"query": {"search": [{"title": "Stub", "snippet": "stub"}]}},
            )
        raise AssertionError(f"unexpected synthetic provider: {host}")

    configured = replace(
        settings,
        yandex_search_api_key="synthetic-yandex-key",
        brave_search_api_key="synthetic-brave-key",
        tavily_api_key="synthetic-tavily-key",
        serper_api_key="synthetic-serper-key",
    )
    surfer = WebSurfer(configured)
    surfer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    try:
        arguments = {"site": "docs.example.test", "freshness": "week"}
        await surfer._search_yandex("needle", 2, **arguments)  # noqa: SLF001
        await surfer._search_brave("needle", 2, **arguments)  # noqa: SLF001
        await surfer._search_tavily("needle", 2, **arguments)  # noqa: SLF001
        await surfer._search_serper("needle", 2, **arguments)  # noqa: SLF001
        site_only = {"site": arguments["site"]}
        await surfer._search_brave_html("needle", 2, **site_only)  # noqa: SLF001
        await surfer._search_duckduckgo_html("needle", 2, **site_only)  # noqa: SLF001
        await surfer._search_wikipedia("needle", 2, site="wikipedia.org")  # noqa: SLF001
    finally:
        await surfer.close()

    site_query = "needle site:docs.example.test"
    assert seen["yandex"]["query"]["queryText"] == (  # type: ignore[index]
        "needle site:docs.example.test date:>20260730"
    )
    assert seen["brave"] == {
        "q": "needle site:docs.example.test",
        "count": "2",
        "freshness": "pw",
    }
    assert seen["tavily"]["query"] == "needle"
    assert seen["tavily"]["include_domains"] == ["docs.example.test"]
    assert seen["tavily"]["time_range"] == "week"
    assert seen["serper"] == {
        "q": "needle site:docs.example.test",
        "num": 2,
        "tbs": "qdr:w",
    }
    assert seen["brave-html"]["q"] == site_query
    assert seen["duckduckgo"]["q"] == site_query
    assert seen["wikipedia"]["srsearch"] == "needle"


@pytest.mark.asyncio
async def test_provider_stubs_apply_new_hints_but_never_send_excluded_domains(
    settings,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_surfer_module, "_freshness_after", lambda _freshness: "2026-07-30")
    seen: dict[str, dict[str, object]] = {}
    excluded = "private-customer.example.test"
    yandex_xml = base64.b64encode(
        b"<yandexsearch><response><results><grouping><group><doc>"
        b"<url>https://docs.example.test/yandex</url><title>Yandex</title>"
        b"</doc></group></grouping></results></response></yandexsearch>"
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "searchapi.api.cloud.yandex.net":
            seen["yandex"] = json.loads(request.content)
            return httpx.Response(200, json={"rawData": yandex_xml})
        if host == "api.search.brave.com":
            seen["brave"] = dict(request.url.params)
            return httpx.Response(200, json={"web": {"results": []}})
        if host == "api.tavily.com":
            seen["tavily"] = json.loads(request.content)
            return httpx.Response(200, json={"results": []})
        if host == "google.serper.dev":
            seen["serper"] = json.loads(request.content)
            return httpx.Response(200, json={"organic": []})
        if host == "search.brave.com":
            seen["brave-html"] = dict(request.url.params)
            return httpx.Response(
                200,
                text=(
                    "<div id='results'><div class='snippet' data-type='web'>"
                    "<a href='https://docs.example.test/a'>A</a></div></div>"
                ),
            )
        if host == "html.duckduckgo.com":
            seen["duckduckgo"] = dict(request.url.params)
            return httpx.Response(
                200,
                text=(
                    "<div class='result'><a class='result__a' href='https://docs.example.test/b'>B</a></div>"
                ),
            )
        if host == "ru.wikipedia.org":
            seen["wikipedia"] = dict(request.url.params)
            return httpx.Response(200, json={"query": {"search": []}})
        raise AssertionError(f"unexpected synthetic provider: {host}")

    configured = replace(
        settings,
        yandex_search_api_key="synthetic-yandex-key",
        brave_search_api_key="synthetic-brave-key",
        tavily_api_key="synthetic-tavily-key",
        serper_api_key="synthetic-serper-key",
    )
    surfer = WebSurfer(configured)
    surfer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    domains = ["docs.example.test", "news.example.test"]
    common = {"include_domains": domains, "exclude_domains": [excluded]}
    try:
        await surfer._search_yandex(  # noqa: SLF001
            "needle", 4, **common, freshness="week", lang="RU", region="RU"
        )
        await surfer._search_brave(  # noqa: SLF001
            "needle", 4, **common, freshness="week", lang="RU", region="RU"
        )
        await surfer._search_tavily(  # noqa: SLF001
            "needle", 4, **common, freshness="week", region="RU"
        )
        await surfer._search_serper(  # noqa: SLF001
            "needle", 4, **common, freshness="week", lang="RU", region="RU"
        )
        await surfer._search_brave_html("needle", 4, **common)  # noqa: SLF001
        await surfer._search_duckduckgo_html("needle", 4, **common)  # noqa: SLF001
        await surfer._search_wikipedia(  # noqa: SLF001
            "needle",
            4,
            include_domains=["wikipedia.org"],
            exclude_domains=[excluded],
            lang="RU",
        )
    finally:
        await surfer.close()

    assert seen["yandex"]["query"] == {  # type: ignore[index]
        "searchType": "SEARCH_TYPE_RU",
        "queryText": ("needle (site:docs.example.test | site:news.example.test) date:>20260730 lang:ru"),
    }
    assert seen["brave"] == {
        "q": "needle (site:docs.example.test OR site:news.example.test)",
        "count": "4",
        "freshness": "pw",
        "search_lang": "ru",
        "country": "RU",
    }
    assert seen["tavily"]["include_domains"] == domains
    assert seen["tavily"]["country"] == "russia"
    assert seen["tavily"]["topic"] == "general"
    assert seen["serper"]["hl"] == "ru"
    assert seen["serper"]["gl"] == "ru"
    assert seen["wikipedia"]["srsearch"] == "needle"
    assert excluded not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["_search_brave", "_search_tavily", "_search_serper"])
async def test_keyed_provider_200_without_known_result_shape_is_a_refusal(
    settings,
    method_name: str,
) -> None:
    def malformed_success(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    configured = replace(
        settings,
        brave_search_api_key="synthetic-brave-key",
        tavily_api_key="synthetic-tavily-key",
        serper_api_key="synthetic-serper-key",
    )
    surfer = WebSurfer(configured)
    surfer._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(malformed_success)
    )
    try:
        with pytest.raises(ProviderRefusedError):
            await getattr(surfer, method_name)("needle", 2)
    finally:
        await surfer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "provider_name"),
    [
        ("_search_brave_html", "brave-html"),
        ("_search_duckduckgo_html", "duckduckgo"),
        ("_search_wikipedia", "wikipedia"),
    ],
)
async def test_unverified_freshness_adapters_refuse_before_opening_a_socket(
    settings,
    method_name: str,
    provider_name: str,
) -> None:
    """Mutation proof: deleting any local guard reaches the exploding transport."""

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"stale-capable request escaped to {request.url.host}")

    surfer = WebSurfer(settings)
    surfer._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(unexpected_request)
    )
    try:
        method = getattr(surfer, method_name)
        with pytest.raises(UnsupportedSearchFilterError) as caught:
            await method(
                "needle",
                2,
                site="docs.example.test",
                freshness="week",
            )
    finally:
        await surfer.close()

    assert caught.value.reason == "unsupported_filter"
    assert caught.value.provider == provider_name
    assert caught.value.filter_name == "freshness"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "provider_name", "filters", "filter_name"),
    [
        ("_search_brave_html", "brave-html", {"lang": "ru"}, "lang"),
        ("_search_duckduckgo_html", "duckduckgo", {"region": "ru"}, "region"),
        ("_search_wikipedia", "wikipedia", {"region": "ru"}, "region"),
        ("_search_wikipedia", "wikipedia", {"lang": "de"}, "lang"),
        ("_search_tavily", "tavily", {"lang": "ru"}, "lang"),
        ("_search_yandex", "yandex", {"region": "us"}, "region"),
        ("_search_brave", "brave", {"lang": "zh"}, "lang"),
    ],
)
async def test_unverified_locale_adapters_refuse_before_opening_a_socket(
    settings,
    method_name: str,
    provider_name: str,
    filters: dict[str, str],
    filter_name: str,
) -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unlocalised request escaped to {request.url.host}")

    surfer = WebSurfer(settings)
    surfer._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(unexpected_request)
    )
    try:
        with pytest.raises(UnsupportedSearchFilterError) as caught:
            await getattr(surfer, method_name)("needle", 2, **filters)
    finally:
        await surfer.close()

    assert caught.value.provider == provider_name
    assert caught.value.filter_name == filter_name


@pytest.mark.asyncio
async def test_no_capable_locale_provider_is_structural_failure_without_a_socket(settings) -> None:
    configured = replace(
        settings,
        yandex_search_api_key="",
        brave_search_api_key="",
        tavily_api_key="",
        serper_api_key="",
    )
    surfer = WebSurfer(configured)

    with pytest.raises(SearchFilterUnavailableError) as caught:
        await surfer.search("needle", region="US")

    assert caught.value.reason == "search_filter_unavailable"
    assert caught.value.filter_name == "region"
    assert "needle" not in str(caught.value)
    assert caught.value.unsupported_providers == (
        "brave-html",
        "duckduckgo",
        "wikipedia",
    )
    assert surfer._client is None


@pytest.mark.asyncio
async def test_combined_capability_failure_reports_every_observed_blocker(settings) -> None:
    surfer = WebSurfer(settings)

    async def lacks_freshness() -> list[SearchResult]:
        raise UnsupportedSearchFilterError(provider="one", filter_name="freshness")

    async def lacks_region() -> list[SearchResult]:
        raise UnsupportedSearchFilterError(provider="two", filter_name="region")

    def chain(*_args, **_kwargs):
        return [("one", lacks_freshness), ("two", lacks_region)]

    surfer._provider_chain = chain  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(SearchFilterUnavailableError) as caught:
        await surfer.search("needle", freshness="day", region="US")

    assert caught.value.filter_name == "freshness"
    assert caught.value.filter_names == ("freshness", "region")
    assert caught.value.unsupported_providers == ("one", "two")


@pytest.mark.asyncio
async def test_empty_wikipedia_fallback_is_not_reported_as_empty_open_web(settings) -> None:
    surfer = WebSurfer(settings)

    async def refused() -> list[SearchResult]:
        raise ProviderRefusedError("synthetic general provider refusal")

    async def empty_wikipedia() -> list[SearchResult]:
        return []

    def general_chain(*_args, **_kwargs):
        return [("general", refused), ("wikipedia", empty_wikipedia)]

    surfer._provider_chain = general_chain  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(AllProvidersRefusedError):
        await surfer.search("курс на сегодня")

    assert await surfer.search("редкая статья", site="wikipedia.org") == []


@pytest.mark.asyncio
async def test_domain_boundary_overfetches_once_and_never_backfills_forbidden_rows(settings) -> None:
    surfer = WebSurfer(settings)
    calls: list[str] = []

    async def mixed() -> list[SearchResult]:
        calls.append("first")
        return [
            SearchResult("evil", "https://example.test.evil.invalid/a", "", "stub"),
            SearchResult("ad", "https://ads.example.test/b", "", "stub"),
            SearchResult("one", "https://docs.example.test/c", "", "stub"),
            SearchResult("two", "https://sub.example.test/d", "", "stub"),
        ]

    async def must_not_run() -> list[SearchResult]:
        calls.append("second")
        raise AssertionError("a second provider saw a query despite valid survivors")

    def chain(query: str, limit: int, **options):
        assert query == "needle"
        assert limit == 6
        assert options["include_domains"] == ("example.test",)
        assert options["exclude_domains"] == ("ads.example.test",)
        return [("one", mixed), ("two", must_not_run)]

    surfer._provider_chain = chain  # type: ignore[assignment]  # noqa: SLF001
    results = await surfer.search(
        "needle",
        max_results=3,
        include_domains=["Example.Test."],
        exclude_domains=["ads.example.test"],
    )

    assert calls == ["first"]
    assert [item.url for item in results] == [
        "https://docs.example.test/c",
        "https://sub.example.test/d",
    ]


@pytest.mark.asyncio
async def test_exclude_only_boundary_rejects_urls_without_public_web_hosts(settings) -> None:
    surfer = WebSurfer(settings)
    calls: list[str] = []

    async def opaque() -> list[SearchResult]:
        calls.append("first")
        return [
            SearchResult("relative", "/private", "", "stub"),
            SearchResult("mail", "mailto:person@example.test", "", "stub"),
            SearchResult("opaque", "javascript:void(0)", "", "stub"),
        ]

    async def public() -> list[SearchResult]:
        calls.append("second")
        return [SearchResult("public", "https://public.example.test/a", "", "stub")]

    def chain(*_args, **_kwargs):
        return [("one", opaque), ("two", public)]

    surfer._provider_chain = chain  # type: ignore[assignment]  # noqa: SLF001
    results = await surfer.search("needle", exclude_domains=["blocked.example.test"])

    assert calls == ["first", "second"]
    assert [item.url for item in results] == ["https://public.example.test/a"]


@pytest.mark.asyncio
async def test_absent_new_filters_preserve_legacy_adapter_override_signature(settings) -> None:
    configured = replace(
        settings,
        yandex_search_api_key="synthetic-present-key",
        brave_search_api_key="",
        tavily_api_key="",
        serper_api_key="",
    )
    surfer = WebSurfer(configured)
    seen: list[tuple[str, int, str, str]] = []

    async def legacy_adapter(
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
    ) -> list[SearchResult]:
        seen.append((query, limit, site, freshness))
        return [SearchResult("legacy", "https://example.test/a", "", "legacy")]

    surfer._search_yandex = legacy_adapter  # type: ignore[method-assign]  # noqa: SLF001
    results = await surfer.search("needle", max_results=3)

    assert seen == [("needle", 3, "", "")]
    assert [item.title for item in results] == ["legacy"]


@pytest.mark.asyncio
async def test_no_capable_freshness_provider_is_an_explicit_failure_not_empty_results(settings) -> None:
    configured = replace(
        settings,
        yandex_search_api_key="",
        brave_search_api_key="",
        tavily_api_key="",
        serper_api_key="",
    )
    surfer = WebSurfer(configured)

    with pytest.raises(FreshnessUnavailableError) as caught:
        await surfer.search("needle", freshness="month")

    assert caught.value.reason == "freshness_unavailable"
    assert caught.value.filter_name == "freshness"
    assert "needle" not in str(caught.value)
    assert caught.value.refused_providers == ()
    assert caught.value.unsupported_providers == (
        "brave-html",
        "duckduckgo",
        "wikipedia",
    )
    assert surfer._client is None, "unsupported adapters opened a socket before refusing"


@pytest.mark.asyncio
async def test_wikipedia_refuses_an_external_site_before_opening_a_socket(settings) -> None:
    """Mutation: treating `site:` as CirrusSearch syntax reaches the transport."""

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"external-site fallback escaped to {request.url.host}")

    surfer = WebSurfer(settings)
    surfer._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(unexpected_request)
    )
    try:
        with pytest.raises(UnsupportedSearchFilterError) as caught:
            await surfer._search_wikipedia(  # noqa: SLF001
                "needle",
                2,
                site="docs.example.test",
            )
    finally:
        await surfer.close()

    assert caught.value.provider == "wikipedia"
    assert caught.value.filter_name == "site"


@pytest.mark.asyncio
async def test_no_capable_site_provider_is_a_refusal_not_an_honest_empty_result(settings) -> None:
    configured = replace(
        settings,
        yandex_search_api_key="",
        brave_search_api_key="",
        tavily_api_key="",
        serper_api_key="",
    )
    surfer = WebSurfer(configured)

    async def refused_html(*_args, **_kwargs):
        raise ProviderRefusedError("synthetic refusal without network")

    surfer._search_brave_html = refused_html  # type: ignore[method-assign]  # noqa: SLF001
    surfer._search_duckduckgo_html = refused_html  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(AllProvidersRefusedError):
        await surfer.search("needle", site="docs.example.test")

    assert surfer._client is None, "unsupported Wikipedia fallback opened a socket"


@pytest.mark.asyncio
async def test_failed_capable_provider_remains_distinct_from_unsupported_fallbacks(settings) -> None:
    configured = replace(
        settings,
        yandex_search_api_key="synthetic-present-key",
        brave_search_api_key="",
        tavily_api_key="",
        serper_api_key="",
    )
    surfer = WebSurfer(configured)

    async def refused_yandex(*_args, **_kwargs):
        raise ProviderRefusedError("synthetic refusal without network")

    surfer._search_yandex = refused_yandex  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(FreshnessUnavailableError) as caught:
        await surfer.search("needle", freshness="day")

    assert caught.value.refused_providers == ("yandex",)
    assert caught.value.unsupported_providers == (
        "brave-html",
        "duckduckgo",
        "wikipedia",
    )
    assert surfer._client is None


@pytest.mark.asyncio
async def test_site_mismatch_falls_through_and_never_escapes_the_result_boundary(
    settings,
) -> None:
    surfer = WebSurfer(settings)
    calls: list[str] = []
    forwarded: dict[str, str] = {}

    async def unrelated() -> list[SearchResult]:
        calls.append("first")
        return [
            SearchResult(
                "wrong",
                "https://docs.example.test.evil.invalid/a",
                "",
                "stub-one",
            )
        ]

    async def matching() -> list[SearchResult]:
        calls.append("second")
        return [SearchResult("right", "https://sub.docs.example.test/b", "", "stub-two")]

    def chain(query: str, limit: int, *, site: str, freshness: str):
        assert query == "needle"
        assert limit == 3
        forwarded.update(site=site, freshness=freshness)
        return [("one", unrelated), ("two", matching)]

    surfer._provider_chain = chain  # type: ignore[assignment]  # noqa: SLF001
    results = await surfer.search(
        "needle",
        max_results=3,
        site="Docs.Example.Test.",
        freshness="month",
    )

    assert calls == ["first", "second"]
    assert forwarded == {"site": "docs.example.test", "freshness": "month"}
    assert [item.url for item in results] == ["https://sub.docs.example.test/b"]


@pytest.mark.asyncio
async def test_kernel_forwards_filters_but_audit_and_logs_keep_no_raw_values(
    settings,
    storage,
    caplog,
) -> None:
    query = "canary-query-never-log-7f41"
    include_domains = ["Private-Customer.Example.Test.", "Docs.Example.Test"]
    exclude_domains = ["Ads.Private-Customer.Example.Test"]
    calls: list[dict[str, object]] = []

    class RefusingWeb:
        async def search(self, outbound_query: str, **options):
            calls.append({"query": outbound_query, **options})
            raise AllProvidersRefusedError("synthetic refusal")

    storage.ensure_user("operator", preset_key="owner")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        graph,
        RefusingWeb(),  # type: ignore[arg-type]
        IngestionPipeline(settings, storage, graph),
    )
    actor = authorization.actor_for_user("operator", source="test")

    with caplog.at_level(logging.WARNING):
        outcome = await kernel.execute(
            "web_search",
            {
                "query": query,
                "max_results": 3,
                "include_domains": include_domains,
                "exclude_domains": exclude_domains,
                "freshness": "year",
                "lang": "RU",
                "region": "RU",
            },
            actor=actor,
        )

    assert outcome.success is True
    assert calls == [
        {
            "query": query,
            "max_results": 3,
            "include_domains": ["private-customer.example.test", "docs.example.test"],
            "exclude_domains": ["ads.private-customer.example.test"],
            "freshness": "year",
            "lang": "ru",
            "region": "ru",
        }
    ]
    audit_rows = [
        row for row in storage.list_audit_log("operator", limit=20) if row["target_id"] == "web_search"
    ]
    assert audit_rows
    raw_audit = audit_rows[0]["after_json"]
    after = json.loads(raw_audit)
    assert query not in raw_audit
    for domain in include_domains + exclude_domains:
        assert domain not in raw_audit
        assert domain.casefold().rstrip(".") not in raw_audit
    assert query not in caplog.text
    for domain in include_domains + exclude_domains:
        assert domain not in caplog.text
        assert domain.casefold().rstrip(".") not in caplog.text
    assert str(after["query_ref"]).startswith("fpref_")
    assert str(after["include_domains_ref"]).startswith("fpref_")
    assert str(after["exclude_domains_ref"]).startswith("fpref_")
    assert "query_sha256" not in after
    assert "include_domains_sha256" not in after
    assert "exclude_domains_sha256" not in after
    assert after["query_chars"] == len(query)
    assert after["include_domains_count"] == 2
    assert after["exclude_domains_count"] == 1
    assert after["freshness"] == "year"
    assert after["lang"] == "ru"
    assert after["region"] == "ru"


def test_audit_helper_fingerprints_the_site_instead_of_storing_it() -> None:
    site = "private-customer.example.test"
    details = ExecutionKernel._audit_details(  # noqa: SLF001
        "web_search",
        {"query": "needle", "site": site, "freshness": "day"},
    )

    assert details["site_sha256"] == hashlib.sha256(site.encode()).hexdigest()
    assert details["site_chars"] == len(site)
    assert details["freshness"] == "day"
    assert site not in json.dumps(details)


def test_audit_helper_uses_order_independent_aggregate_domain_fingerprints() -> None:
    first = ExecutionKernel._audit_details(  # noqa: SLF001
        "web_search",
        {
            "query": "needle",
            "include_domains": ["Docs.Example.Test.", "news.example.test"],
            "exclude_domains": ["ads.example.test"],
            "lang": "RU",
            "region": "US",
        },
    )
    reversed_order = ExecutionKernel._audit_details(  # noqa: SLF001
        "web_search",
        {
            "query": "needle",
            "include_domains": ["news.example.test", "docs.example.test"],
            "exclude_domains": ["ads.example.test"],
            "lang": "ru",
            "region": "us",
        },
    )

    assert first["include_domains_sha256"] == reversed_order["include_domains_sha256"]
    assert first["exclude_domains_sha256"] == reversed_order["exclude_domains_sha256"]
    assert first["include_domains_count"] == 2
    assert first["exclude_domains_count"] == 1
    assert first["lang"] == "ru"
    assert first["region"] == "us"
    rendered = json.dumps(first)
    assert "docs.example.test" not in rendered
    assert "ads.example.test" not in rendered


@pytest.mark.asyncio
async def test_kernel_reports_domain_filter_underfill_without_weakening_it(settings, storage) -> None:
    class SparseWeb:
        async def search(self, outbound_query: str, **options):
            assert outbound_query == "needle"
            assert options == {
                "max_results": 5,
                "include_domains": ["example.test"],
                "exclude_domains": ["ads.example.test"],
            }
            return [
                SearchResult("one", "https://docs.example.test/a", "", "stub"),
                SearchResult("two", "https://news.example.test/b", "", "stub"),
            ]

    storage.ensure_user("operator", preset_key="owner")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        graph,
        SparseWeb(),  # type: ignore[arg-type]
        IngestionPipeline(settings, storage, graph),
    )
    actor = authorization.actor_for_user("operator", source="test")

    outcome = await kernel._web_search(  # noqa: SLF001
        actor=actor,
        query="needle",
        include_domains=["example.test"],
        exclude_domains=["ads.example.test"],
    )

    assert len(outcome["results"]) == 2
    assert outcome["requested_results"] == 5
    assert outcome["returned_results"] == 2
    assert outcome["underfilled"] is True
    assert "не ослаблялись" in outcome["note"]


@pytest.mark.asyncio
async def test_kernel_sends_canonical_idn_without_archive_name_gate(settings, storage) -> None:
    raw_domain = "Хасанов.рф"
    canonical_domain = normalize_search_site(raw_domain)
    checked: list[str] = []
    calls: list[dict[str, object]] = []

    class EmptyWeb:
        async def search(self, outbound_query: str, **options):
            calls.append({"query": outbound_query, **options})
            return []

    storage.ensure_user("operator", preset_key="owner")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        graph,
        EmptyWeb(),  # type: ignore[arg-type]
        IngestionPipeline(settings, storage, graph),
    )
    actor = authorization.actor_for_user("operator", source="test")

    async def forbidden_archive_name_gate(text: str, _actor):
        checked.append(text)
        raise AssertionError("archive name privacy gate must not run")

    kernel._what_must_not_leave = forbidden_archive_name_gate  # type: ignore[attr-defined]  # noqa: SLF001
    await kernel._web_search(actor=actor, query="needle", site=raw_domain)  # noqa: SLF001

    assert checked == []
    assert calls == [{"query": "needle", "max_results": 5, "site": canonical_domain}]
