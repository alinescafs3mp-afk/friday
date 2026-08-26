"""Budgeted public-web search and fetching with SSRF protection.

Only public HTTP(S) destinations are reachable by default. DNS answers are
checked before every request and every redirect, reducing exposure to DNS
rebinding and blocking redirects into the local network. Private-network
fetching can be enabled explicitly for trusted, single-user installations.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import re
import socket
import urllib.parse
import urllib.robotparser
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpcore
import httpx
from bs4 import BeautifulSoup

from friday.config import FridaySettings
from friday.public_web_url import canonical_public_web_url_key
from friday.retrieval import best_snippet
from friday.web_surfer._direct import direct_answers

LOGGER = logging.getLogger(__name__)


def _log_safe_host(url: str) -> str:
    """Hostname-only form of a URL for log lines.

    Full URLs (and httpx exception strings, which embed them) can carry the
    user's query in path or parameters — logs get the host and nothing else.
    """
    try:
        return urllib.parse.urlsplit(str(url)).hostname or "<invalid-url>"
    except ValueError:
        return "<invalid-url>"


def _failure_url(url: str) -> str:
    """Host-only identifier for an unsuccessful fetch payload."""

    host = _log_safe_host(url)
    return "" if host == "<invalid-url>" else host


_SEARCH_TIMEOUT = 15.0
_FETCH_TIMEOUT = 20.0
# Ceiling on ONE fetch including redirects and body streaming. Sized from the byte
# limit rather than picked: 5 MiB in 60 s is ~87 KiB/s, slow enough that an honest
# page on a poor link still arrives, fast enough that a drip feed does not sit
# forever on one of twenty pooled connections.
_FETCH_TOTAL_BUDGET = 60.0
# `ExecutionKernel` gives every non-code tool 30 seconds. Research returns at 27
# even when provider fallback consumes the search stage; page fetches get at most
# 12 of the remaining seconds. Three stay for scheduling, rendering and audit.
# Without an inner deadline the outer timeout cancelled `gather()` and erased two
# completed pages because one peer was still streaming.
_RESEARCH_TOTAL_BUDGET = 27.0
_RESEARCH_FETCH_BUDGET = 12.0
_RESEARCH_DIRECT_BUDGET = 8.0
#: Срок на чтение `robots.txt`. Отдельный и короткий: файл правил крошечный, а
#: без своего срока он становится дорогой в обход страничного бюджета.
_ROBOTS_TIMEOUT = 10.0


def _host_of(url: str) -> str:
    """Домен ссылки — чтобы не ломиться повторно туда, откуда уже отказали.

    `www.` отбрасывается: `dns-shop.ru` и `www.dns-shop.ru` — один и тот же
    магазин, и считать их разными сайтами значит повторить отказ ещё раз.
    """
    try:
        host = urllib.parse.urlparse(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# CPU wall-clock for pypdf after the body is already in memory. Declared with the
# PDF allowlist (G22 / PROPOSALS №3): measured 9/10 public PDFs extracted clean
# text in <0.5 s; 8 s leaves headroom for a multi-megabyte report without letting
# a pathological file pin the event-loop thread pool.
#
# Запасное значение: боевой срок берётся из `settings.pdf_parse_budget_sec` —
# одна ручка на веб-путь и на загрузку файла. Константа остаётся для стендов,
# где в `settings` этого поля ещё нет.
_PDF_PARSE_BUDGET = 8.0
_DEFAULT_MAX_RESULTS = 5
_DEFAULT_MAX_SOURCES = 3
_MAX_REDIRECTS = 5
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
#: Wikimedia требует осмысленный User-Agent с адресом проекта, а не подделку под
#: браузер. Их правило: клиент, который представляется честно, не блокируется.
_WIKIPEDIA_USER_AGENT = "Friday/1.0 (local knowledge assistant; +https://github.com/alinescafs3mp/friday)"
_DROP_TAGS = {"nav", "footer", "header", "aside", "script", "style", "svg", "noscript", "form"}
_DROP_CLASS_RE = re.compile(
    r"(?:^|[-_\s])(sidebar|navigation|nav|menu|footer|header|advert|ads?|cookie|consent|popup)(?:$|[-_\s])",
    re.IGNORECASE,
)
_ALLOWED_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "application/json",
    "application/xml",
    "text/xml",
    # G22 measurement (threshold ≥7/10 declared first): 9/10 public PDFs yielded
    # meaningful text via DocumentExtractor; dummy short PDF was the only miss.
    "application/pdf",
)


class UnsafeURLError(ValueError):
    """The requested URL can reach a non-public or otherwise unsafe target."""


class ProviderRefusedError(RuntimeError):
    """Поисковик не ответил по существу — это НЕ «в интернете ничего нет».

    Разница видна только здесь, и она решающая. DuckDuckGo на анти-бот
    срабатывание отдаёт **HTTP 202** со страницей-заглушкой: `raise_for_status`
    её пропускает (202 — успех), разметки результатов в ней нет, и функция
    возвращала пустой список. Снаружи «нас отшили» становилось неотличимо от
    «ничего не найдено», и человек слышал «внешние источники недоступны» в обоих
    случаях. Замерено на 20 запросах: 19 из 20 ответов DuckDuckGo — именно 202.

    Отказ означает «спроси следующего провайдера», пустой список — «этот честно
    ничего не нашёл, спрашивать дальше незачем».
    """


class AllProvidersRefusedError(RuntimeError):
    """Ни один поисковик не ответил по существу — искать было нечем."""


class UnsupportedSearchFilterError(ProviderRefusedError):
    """One adapter cannot enforce a requested filter and was not called.

    This is deliberately a provider refusal, not an empty result: returning an
    unfiltered batch would turn "nothing recent" into a false fact.  Attributes
    are closed structural values so callers never need to parse exception text.
    """

    reason = "unsupported_filter"
    _FILTER_NAMES = frozenset({"site", "freshness", "include_domains", "exclude_domains", "lang", "region"})

    def __init__(self, *, provider: str, filter_name: str) -> None:
        self.provider = provider
        self.filter_name = filter_name if filter_name in self._FILTER_NAMES else "unknown"
        super().__init__(f"{provider} cannot enforce {self.filter_name}")


class SearchFilterUnavailableError(AllProvidersRefusedError):
    """No available provider could honestly apply one requested search filter."""

    reason = "search_filter_unavailable"

    def __init__(
        self,
        *,
        filter_name: str = "",
        filter_names: Sequence[str] = (),
        unsupported_providers: Sequence[str],
        refused_providers: Sequence[str] = (),
    ) -> None:
        requested_names = (*filter_names, filter_name) if filter_name else tuple(filter_names)
        safe_names = tuple(
            dict.fromkeys(
                name for name in requested_names if name in UnsupportedSearchFilterError._FILTER_NAMES
            )
        )
        self.filter_names = safe_names or ("unknown",)
        self.filter_name = self.filter_names[0]
        self.unsupported_providers = tuple(unsupported_providers)
        self.refused_providers = tuple(refused_providers)
        super().__init__("no search provider could apply a requested filter")


class FreshnessUnavailableError(SearchFilterUnavailableError):
    """Compatibility-specialised failure for the established freshness filter."""

    reason = "freshness_unavailable"

    def __init__(
        self,
        *,
        unsupported_providers: Sequence[str],
        refused_providers: Sequence[str] = (),
    ) -> None:
        super().__init__(
            filter_name="freshness",
            unsupported_providers=unsupported_providers,
            refused_providers=refused_providers,
        )
        self.reason = "freshness_unavailable"


#: Ответы, которые означают отказ, а не выдачу. 202 — заглушка DuckDuckGo,
#: 401/403 — неверный или истёкший ключ, 429 — исчерпанный лимит.
_REFUSAL_STATUS = frozenset({202, 401, 403, 429})

#: Человек прямо просит источники не из русского сегмента. Замерено на живом
#: вопросе: «какие новости не из ру сегмента есть за сегодня и вчера?» приносило
#: lenta.ru и rbc.ru — ровно то, о чём просили НЕ давать.
_ASKS_FOR_FOREIGN_SOURCES = re.compile(
    r"не\s+из\s+ру\w*|не\s+рос\w+|не\s+рунет\w*|вне\s+рунет\w*|"
    r"зарубежн\w+|иностранн\w+|западн\w+\s+(?:сми|пресс\w*|источник\w*)|"
    r"мировы\w+\s+(?:сми|пресс\w*|новост\w*)|на\s+английск\w+|"
    r"english\s+sources?|foreign\s+(?:press|media|sources?)",
    re.IGNORECASE,
)

# Closed vocabulary exposed by the `web_search` tool.  Providers use different
# spellings for the same window, so accepting arbitrary strings here would turn
# a typo into an unfiltered (and therefore misleading) search.
SEARCH_FRESHNESS_VALUES = ("", "day", "week", "month", "year")
SEARCH_DOMAIN_LIST_MAX = 10
SEARCH_SOURCE_CLASS_VALUES = ("", "foreign")
SEARCH_FILTER_ATTESTATION_KEY = "applied_search_filters"
_SEARCH_FILTER_CAPABILITIES_ATTR = "__friday_search_filter_capabilities__"


def declares_search_filter_support(*filter_names: str):  # noqa: ANN201
    """Mark an in-process adapter as deliberately enforcing closed filters.

    Accepting ``**kwargs`` is not a capability declaration: legacy wrappers do
    that routinely and may silently discard a newly added filter.  Adapters
    outside :class:`WebSurfer` must opt in explicitly and must also attest the
    applied value in their returned batch/report.
    """

    safe_names = frozenset(
        name for name in filter_names if name in UnsupportedSearchFilterError._FILTER_NAMES
    )

    def decorate(function):  # noqa: ANN001, ANN202
        setattr(function, _SEARCH_FILTER_CAPABILITIES_ATTR, safe_names)
        return function

    return decorate


def search_callable_supports_filter(function: Any, filter_name: str) -> bool:
    """Whether one exact callable explicitly declared a filter capability."""

    target = getattr(function, "__func__", function)
    capabilities: object = getattr(target, _SEARCH_FILTER_CAPABILITIES_ATTR, frozenset())
    return isinstance(capabilities, frozenset) and filter_name in capabilities


def search_filter_is_attested(value: Any, filter_name: str, expected: str) -> bool:
    """Validate the returned, structural proof for one applied filter value."""

    if isinstance(value, Mapping):
        attestation = value.get(SEARCH_FILTER_ATTESTATION_KEY)
    else:
        attestation = getattr(value, SEARCH_FILTER_ATTESTATION_KEY, None)
    return isinstance(attestation, Mapping) and attestation.get(filter_name) == expected


def normalize_search_source_class(value: str) -> str:
    """Return one closed, code-owned source class or reject the request.

    ``foreign`` is deliberately a DNS-level promise: Russian country domains
    and the Russian Wikipedia language host are excluded after every provider,
    rather than merely asking a ranking API for international results.
    """

    if not isinstance(value, str):
        raise ValueError("source_class must be a string")
    normalized = value.strip().casefold()
    if normalized not in SEARCH_SOURCE_CLASS_VALUES:
        raise ValueError("unsupported source_class")
    return normalized


_RUSSIAN_SOURCE_SUFFIXES = (".ru", ".su", ".xn--p1ai", ".xn--p1acf")


def web_source_matches_class(url: str, source_class: str) -> bool:
    """Whether a public URL satisfies a closed source-class constraint."""

    normalized = normalize_search_source_class(source_class)
    if not normalized:
        return True
    host = _host_of(url).casefold().rstrip(".")
    if not host:
        return False
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return not (
        host == "ru"
        or host.startswith("ru.")
        or any(host.endswith(suffix) for suffix in _RUSSIAN_SOURCE_SUFFIXES)
    )


def _closed_codes(values: str) -> frozenset[str]:
    return frozenset(values.split())


# ISO lists are project-owned instead of a runtime dependency.  The compact
# whitespace form keeps the public tool schema small: the model sees a two-letter
# pattern and examples, while the handler still rejects invented codes before a
# quota is charged or a socket is opened.
_ISO_639_1 = _closed_codes(
    """aa ab ae af ak am an ar as av ay az ba be bg bi bm bn bo br bs ca ce ch co cr cs cu cv cy
    da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu
    hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb
    lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om
    or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw
    ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu"""
)
_ISO_3166_1_ALPHA2 = _closed_codes(
    """ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm bn bo bq
    br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do
    dz ec ee eg eh er es et fi fj fk fm fo fr ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu
    gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw
    ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq mr ms mt mu
    mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw
    py qa re ro rs ru rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st sv sx sy sz tc td tf
    tg th tj tk tl tm tn to tr tt tv tw tz ua ug um us uy uz va vc ve vg vi vn vu wf ws ye yt za
    zm zw"""
)
_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 31, "year": 365}
_BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
_SERPER_FRESHNESS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
_BRAVE_SEARCH_LANGUAGES = _closed_codes(
    """ar eu bn bg ca hr cs da nl en et fi fr gl de el gu he hi hu is it ja kn ko lv lt ms ml mr
    nb pl pa ro ru sr sk sl es sv ta te th tr uk vi"""
)
_BRAVE_COUNTRIES = _closed_codes(
    """ar au at be br ca cl dk fi fr de gr hk in id it jp kr my mx nl nz no cn pl pt ph ru sa za es
    se ch tw tr gb us"""
)
_YANDEX_REGION_TO_SEARCH_TYPE = {"ru": "SEARCH_TYPE_RU", "tr": "SEARCH_TYPE_TR"}
_WIKIPEDIA_LANGUAGE_HOST = {"ru": "ru.wikipedia.org", "en": "en.wikipedia.org"}
# Tavily's public API accepts lowercase English country names rather than ISO
# codes.  This complete explicit mapping comes from its closed `country` enum;
# absent codes are refused locally.  `cd` is intentionally absent because the
# provider's sole `congo` value does not distinguish the two countries.
_TAVILY_COUNTRY_BY_ISO = {
    "ad": "andorra",
    "ae": "united arab emirates",
    "af": "afghanistan",
    "al": "albania",
    "am": "armenia",
    "ao": "angola",
    "ar": "argentina",
    "at": "austria",
    "au": "australia",
    "az": "azerbaijan",
    "ba": "bosnia and herzegovina",
    "bb": "barbados",
    "bd": "bangladesh",
    "be": "belgium",
    "bf": "burkina faso",
    "bg": "bulgaria",
    "bh": "bahrain",
    "bi": "burundi",
    "bj": "benin",
    "bn": "brunei",
    "bo": "bolivia",
    "br": "brazil",
    "bs": "bahamas",
    "bt": "bhutan",
    "bw": "botswana",
    "by": "belarus",
    "bz": "belize",
    "ca": "canada",
    "cf": "central african republic",
    "cg": "congo",
    "ch": "switzerland",
    "cl": "chile",
    "cm": "cameroon",
    "cn": "china",
    "co": "colombia",
    "cr": "costa rica",
    "cu": "cuba",
    "cv": "cape verde",
    "cy": "cyprus",
    "cz": "czech republic",
    "de": "germany",
    "dj": "djibouti",
    "dk": "denmark",
    "do": "dominican republic",
    "dz": "algeria",
    "ec": "ecuador",
    "ee": "estonia",
    "eg": "egypt",
    "er": "eritrea",
    "es": "spain",
    "et": "ethiopia",
    "fi": "finland",
    "fj": "fiji",
    "fr": "france",
    "ga": "gabon",
    "gb": "united kingdom",
    "ge": "georgia",
    "gh": "ghana",
    "gm": "gambia",
    "gn": "guinea",
    "gq": "equatorial guinea",
    "gr": "greece",
    "gt": "guatemala",
    "hn": "honduras",
    "hr": "croatia",
    "ht": "haiti",
    "hu": "hungary",
    "id": "indonesia",
    "ie": "ireland",
    "il": "israel",
    "in": "india",
    "iq": "iraq",
    "ir": "iran",
    "is": "iceland",
    "it": "italy",
    "jm": "jamaica",
    "jo": "jordan",
    "jp": "japan",
    "ke": "kenya",
    "kg": "kyrgyzstan",
    "kh": "cambodia",
    "km": "comoros",
    "kp": "north korea",
    "kr": "south korea",
    "kw": "kuwait",
    "kz": "kazakhstan",
    "lb": "lebanon",
    "li": "liechtenstein",
    "lk": "sri lanka",
    "lr": "liberia",
    "ls": "lesotho",
    "lt": "lithuania",
    "lu": "luxembourg",
    "lv": "latvia",
    "ly": "libya",
    "ma": "morocco",
    "mc": "monaco",
    "md": "moldova",
    "me": "montenegro",
    "mg": "madagascar",
    "mk": "north macedonia",
    "ml": "mali",
    "mm": "myanmar",
    "mn": "mongolia",
    "mr": "mauritania",
    "mt": "malta",
    "mu": "mauritius",
    "mv": "maldives",
    "mw": "malawi",
    "mx": "mexico",
    "my": "malaysia",
    "mz": "mozambique",
    "na": "namibia",
    "ne": "niger",
    "ng": "nigeria",
    "ni": "nicaragua",
    "nl": "netherlands",
    "no": "norway",
    "np": "nepal",
    "nz": "new zealand",
    "om": "oman",
    "pa": "panama",
    "pe": "peru",
    "pg": "papua new guinea",
    "ph": "philippines",
    "pk": "pakistan",
    "pl": "poland",
    "pt": "portugal",
    "py": "paraguay",
    "qa": "qatar",
    "ro": "romania",
    "rs": "serbia",
    "ru": "russia",
    "rw": "rwanda",
    "sa": "saudi arabia",
    "sd": "sudan",
    "se": "sweden",
    "sg": "singapore",
    "si": "slovenia",
    "sk": "slovakia",
    "sn": "senegal",
    "so": "somalia",
    "ss": "south sudan",
    "sv": "el salvador",
    "sy": "syria",
    "td": "chad",
    "tg": "togo",
    "th": "thailand",
    "tj": "tajikistan",
    "tm": "turkmenistan",
    "tn": "tunisia",
    "tr": "turkey",
    "tt": "trinidad and tobago",
    "tw": "taiwan",
    "tz": "tanzania",
    "ua": "ukraine",
    "ug": "uganda",
    "us": "united states",
    "uy": "uruguay",
    "uz": "uzbekistan",
    "ve": "venezuela",
    "vn": "vietnam",
    "ye": "yemen",
    "za": "south africa",
    "zm": "zambia",
    "zw": "zimbabwe",
}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_search_site(site: str) -> str:
    """Return one canonical bare DNS hostname or reject the filter.

    A site filter becomes part of a provider query, so it is deliberately much
    narrower than a URL: no scheme, port, path, userinfo, wildcard or query
    syntax.  IDNs are accepted and converted to their ASCII DNS form before the
    provider sees them.
    """

    if not isinstance(site, str):
        raise ValueError("site must be a bare DNS hostname")
    candidate = site.strip()
    if not candidate:
        return ""
    if any(character.isspace() for character in candidate) or any(
        marker in candidate for marker in (":", "/", "\\", "@", "?", "#", "*")
    ):
        raise ValueError("site must be a bare DNS hostname")
    candidate = candidate.removesuffix(".")
    try:
        canonical = candidate.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("site must be a bare DNS hostname") from exc
    labels = canonical.split(".")
    if (
        len(canonical) > 253
        or len(labels) < 2
        or labels[-1].isdigit()
        or any(not _DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("site must be a bare DNS hostname")
    try:
        ipaddress.ip_address(canonical)
    except ValueError:
        return canonical
    raise ValueError("site must be a bare DNS hostname")


def normalize_search_freshness(freshness: str) -> str:
    """Validate the public freshness enum without silently widening the search."""

    if not isinstance(freshness, str) or freshness not in SEARCH_FRESHNESS_VALUES:
        raise ValueError("freshness must be one of: day, week, month, year, or empty")
    return freshness


def normalize_search_language(language: str) -> str:
    """Validate an optional ISO-639-1 code and return lowercase canonical form."""

    if not isinstance(language, str):
        raise ValueError("lang must be an empty value or a two-letter ISO-639-1 code")
    canonical = language.casefold()
    if canonical and canonical not in _ISO_639_1:
        raise ValueError("lang must be an empty value or a two-letter ISO-639-1 code")
    return canonical


def normalize_search_region(region: str) -> str:
    """Validate an ISO-3166-1 alpha-2 market and return lowercase canonical form."""

    if not isinstance(region, str):
        raise ValueError("region must be an empty value or a two-letter ISO-3166-1 alpha-2 code")
    canonical = region.casefold()
    if canonical and canonical not in _ISO_3166_1_ALPHA2:
        raise ValueError("region must be an empty value or a two-letter ISO-3166-1 alpha-2 code")
    return canonical


def normalize_search_domains(domains: Sequence[str], *, filter_name: str) -> tuple[str, ...]:
    """Canonicalise one bounded domain list without reflecting its values."""

    filter_name = filter_name if filter_name in {"include_domains", "exclude_domains"} else "domain list"
    if isinstance(domains, str | bytes) or not isinstance(domains, Sequence):
        raise ValueError(f"{filter_name} must be an array of bare DNS hostnames")
    if len(domains) > SEARCH_DOMAIN_LIST_MAX:
        raise ValueError(f"{filter_name} accepts at most {SEARCH_DOMAIN_LIST_MAX} domains")
    canonical: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        try:
            item = normalize_search_site(domain)
        except ValueError as exc:
            raise ValueError(f"{filter_name} must contain only bare DNS hostnames") from exc
        if not item:
            raise ValueError(f"{filter_name} must contain only non-empty hostnames")
        if item in seen:
            raise ValueError(f"{filter_name} must contain unique canonical hostnames")
        seen.add(item)
        canonical.append(item)
    return tuple(canonical)


def _host_matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def normalize_search_filters(
    *,
    site: str = "",
    include_domains: Sequence[str] = (),
    exclude_domains: Sequence[str] = (),
    freshness: str = "",
    lang: str = "",
    region: str = "",
) -> tuple[str, tuple[str, ...], tuple[str, ...], str, str, str]:
    """Validate the complete public filter combination before provider work."""

    canonical_site = normalize_search_site(site)
    included = normalize_search_domains(include_domains, filter_name="include_domains")
    excluded = normalize_search_domains(exclude_domains, filter_name="exclude_domains")
    if canonical_site and included:
        raise ValueError("site and include_domains cannot be combined")
    effective_includes = (canonical_site,) if canonical_site else included
    if any(
        _host_matches_domain(included_domain, excluded_domain)
        for included_domain in effective_includes
        for excluded_domain in excluded
    ):
        raise ValueError("exclude_domains cannot cover an included domain")
    return (
        canonical_site,
        included,
        excluded,
        normalize_search_freshness(freshness),
        normalize_search_language(lang),
        normalize_search_region(region),
    )


def _freshness_after(freshness: str) -> str:
    """Calendar boundary used by Yandex's documented ``date:>`` operator."""

    freshness = normalize_search_freshness(freshness)
    if not freshness:
        return ""
    today = datetime.now(UTC).date()
    return (today - timedelta(days=_FRESHNESS_DAYS[freshness])).isoformat()


def _query_with_domain_filter(
    query: str,
    *,
    site: str,
    include_domains: Sequence[str] = (),
    union_operator: str = "OR",
) -> str:
    """Append provider hints for strict code-owned include-domain filtering."""

    site, included, _, _, _, _ = normalize_search_filters(
        site=site,
        include_domains=include_domains,
    )
    targets = (site,) if site else included
    if not targets:
        return query
    if len(targets) == 1:
        clause = f"site:{targets[0]}"
    else:
        clause = "(" + f" {union_operator} ".join(f"site:{domain}" for domain in targets) + ")"
    return f"{query} {clause}"


def _query_with_site_filter(query: str, *, site: str) -> str:
    """Backward-compatible spelling for the original one-domain helper."""

    return _query_with_domain_filter(query, site=site)


def _query_with_yandex_filters(
    query: str,
    *,
    site: str,
    include_domains: Sequence[str] = (),
    freshness: str,
    lang: str = "",
) -> str:
    """Use Yandex's documented date syntax, never Google's ``after:``."""

    freshness = normalize_search_freshness(freshness)
    lang = normalize_search_language(lang)
    pieces = [
        _query_with_domain_filter(
            query,
            site=site,
            include_domains=include_domains,
            union_operator="|",
        )
    ]
    if freshness:
        pieces.append(f"date:>{_freshness_after(freshness).replace('-', '')}")
    if lang:
        # Yandex documents this query-language operator for the document
        # language.  `l10n` is deliberately not used: it changes error-message
        # localisation, not search results.
        pieces.append(f"lang:{lang}")
    return " ".join(pieces)


def _reject_unsupported_freshness(provider: str, freshness: str) -> None:
    """Fail before opening a socket when an adapter cannot enforce freshness."""

    freshness = normalize_search_freshness(freshness)
    if freshness:
        raise UnsupportedSearchFilterError(provider=provider, filter_name="freshness")


def _reject_unsupported_locales(
    provider: str,
    *,
    lang: str,
    region: str,
    languages: frozenset[str] | None = None,
    regions: frozenset[str] | None = None,
) -> None:
    """Reject an unapplied locale before the adapter can allocate a client."""

    lang = normalize_search_language(lang)
    region = normalize_search_region(region)
    if lang and (languages is None or lang not in languages):
        raise UnsupportedSearchFilterError(provider=provider, filter_name="lang")
    if region and (regions is None or region not in regions):
        raise UnsupportedSearchFilterError(provider=provider, filter_name="region")


def _result_matches_site(result: SearchResult, site: str) -> bool:
    """Provider hints are advisory; this makes the returned site filter strict."""

    if not site:
        return True
    try:
        host = urllib.parse.urlsplit(result.url).hostname or ""
        canonical = host.rstrip(".").encode("idna").decode("ascii").casefold()
    except (UnicodeError, ValueError):
        return False
    return _host_matches_domain(canonical, site)


def _result_matches_domains(
    result: SearchResult,
    *,
    site: str,
    include_domains: Sequence[str],
    exclude_domains: Sequence[str],
) -> bool:
    """Enforce exact/subdomain allow and deny lists after every provider."""

    effective_includes = (site,) if site else tuple(include_domains)
    try:
        parsed = urllib.parse.urlsplit(result.url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname
        canonical = host.rstrip(".").encode("idna").decode("ascii").casefold()
    except (UnicodeError, ValueError):
        return False
    if effective_includes and not any(
        _host_matches_domain(canonical, domain) for domain in effective_includes
    ):
        return False
    return not any(_host_matches_domain(canonical, domain) for domain in exclude_domains)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
        }


class AttestedSearchResults(list[SearchResult]):
    """List-compatible search batch carrying code-owned filter evidence."""

    def __init__(
        self,
        values: Sequence[SearchResult] = (),
        *,
        applied_search_filters: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(values)
        setattr(
            self,
            SEARCH_FILTER_ATTESTATION_KEY,
            dict(applied_search_filters or {}),
        )


def attested_search_results(
    values: Sequence[SearchResult],
    *,
    freshness: str = "",
) -> AttestedSearchResults:
    """Build a list-compatible result with an exact freshness attestation."""

    freshness = normalize_search_freshness(freshness)
    applied = {"freshness": freshness} if freshness else {}
    return AttestedSearchResults(values, applied_search_filters=applied)


@dataclass(frozen=True)
class FetchResult:
    url: str
    title: str
    text: str
    text_length: int
    status_code: int | None = None
    error: str = ""
    # Текст неполон по НАШЕЙ причине (сработал срок разбора), а не потому, что
    # документ такой. Признак едет вместе с текстом: без него частичный разбор
    # молча выдавался бы за весь документ — тот же класс, что «длина страницы
    # как факт».
    truncated: bool = False

    def to_dict(self, *, preview_chars: int = 12_000, query: str = "") -> dict[str, Any]:
        """Страница для модели: кусок ВОКРУГ совпадения, а не начало документа.

        Раньше здесь стоял `self.text[:preview_chars]`, то есть модель получала шапку
        страницы — меню, баннер согласия на cookie, хлебные крошки, — а искомое место
        оставалось за границей. Ровно этот дефект проект уже чинил дважды: сперва в
        контекстной выдержке («Quote the passage that matched, not the top of the
        document»), потом в инструменте памяти, где до модели доходил титульный лист.
        До веба починка не доехала.

        Без запроса поведение прежнее — брать голову: без него «совпадение» не
        определено, и придумывать его было бы хуже честного начала.
        """
        if query.strip():
            text = best_snippet(query, self.text, max_chars=preview_chars)
        else:
            text = self.text[:preview_chars]
        return {
            "url": self.url,
            "title": self.title,
            "text": text,
            "text_length": self.text_length,
            "status_code": self.status_code,
            "error": self.error,
            # Обрезали ли МЫ показ (кусок вокруг совпадения) ИЛИ разбор не дошёл
            # до конца документа по сроку. Оба случая означают одно для читателя:
            # это не весь текст.
            "truncated": len(self.text) > len(text) or self.truncated,
        }


def _is_forbidden_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Allow-list by reachability, not a list of the private ranges we remembered.

    The check used to enumerate flags — private, loopback, link-local, multicast,
    reserved, unspecified — and every range outside that list was «public». The
    shared address space `100.64.0.0/10` (RFC 6598) is none of those in Python:
    `is_private` is False and `is_global` is False. That range carries CGNAT
    networks, Tailscale addresses and one cloud's metadata service, so «fetch
    this URL» could reach the owner's own tailnet.
    """
    return not address.is_global or address.is_multicast or address.is_reserved


def _resolve_addresses(hostname: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(hostname.strip("[]"))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"Hostname resolution failed: {hostname}") from exc
        addresses = sorted(
            {ipaddress.ip_address(item[4][0]) for item in infos},
            key=lambda item: (item.version, int(item)),
        )
        if not addresses:
            raise UnsafeURLError("Hostname did not resolve") from None
        return addresses


class _PinnedPublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve, validate, and pin the exact IP used for each TCP connection."""

    def __init__(self, *, allow_private_networks: bool) -> None:
        from httpcore._backends.auto import AutoBackend

        self._delegate = AutoBackend()
        self._allow_private_networks = allow_private_networks

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await asyncio.to_thread(_resolve_addresses, host, port)
        if not self._allow_private_networks and any(_is_forbidden_ip(address) for address in addresses):
            # Same reasoning as `validate_public_url`: the addresses reach the log only.
            LOGGER.debug(
                "Blocked non-public destination at connect time for %s: %s",
                host,
                [str(address) for address in addresses],
            )
            raise UnsafeURLError("Non-public destination is blocked at connect time")

        last_error: Exception | None = None
        for address in addresses:
            try:
                # Passing the validated literal IP prevents a second DNS lookup.
                # httpcore still owns the original URL host and therefore uses it
                # for the Host header and TLS SNI/certificate verification.
                return await self._delegate.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # pragma: no cover - alternate address fallback
                last_error = exc
        if last_error is not None:
            raise last_error
        raise UnsafeURLError("Hostname did not resolve")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeURLError("Unix sockets are not allowed for web fetching")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _CoreResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, allow_private_networks: bool, limits: httpx.Limits) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=_PinnedPublicNetworkBackend(allow_private_networks=allow_private_networks),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            response = await self._pool.handle_async_request(core_request)
        except httpcore.TimeoutException as exc:
            raise httpx.TimeoutException(str(exc), request=request) from exc
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_CoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def validate_public_url(url: str, *, allow_private_networks: bool = False) -> str:
    """Validate and normalize an HTTP(S) URL, including all resolved addresses."""
    value = str(url or "").strip()
    if not value:
        raise UnsafeURLError("URL is empty")
    if len(value) > 4096:
        raise UnsafeURLError("URL is too long")

    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise UnsafeURLError("Only http:// and https:// URLs are allowed")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("Credentials in URLs are not allowed")

    hostname = parsed.hostname.rstrip(".").casefold()
    if not allow_private_networks and (
        not hostname or hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local")
    ):
        raise UnsafeURLError("Local hostnames are blocked")

    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    addresses = _resolve_addresses(hostname, port)
    if not allow_private_networks and any(_is_forbidden_ip(address) for address in addresses):
        # The addresses go to the debug log, not into the error. `FetchResult.error`
        # is returned verbatim by the tool and by `POST /api/ingest/url`'s 422, which
        # turned a blocked fetch into an oracle: guess a name, read back the internal
        # address it resolves to.
        LOGGER.debug(
            "Blocked non-public destination for %s: %s",
            hostname,
            [str(address) for address in addresses],
        )
        raise UnsafeURLError("Non-public destination is blocked")

    normalized_path = parsed.path or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc, normalized_path, parsed.query, "")
    )


def _duckduckgo_target(href: str) -> str:
    absolute = urllib.parse.urljoin("https://html.duckduckgo.com/", href)
    parsed = urllib.parse.urlsplit(absolute)
    params = urllib.parse.parse_qs(parsed.query)
    target = params.get("uddg", [""])[0]
    return urllib.parse.unquote(target) if target else absolute


#: Сколько мест в выдаче может занять ОДИН сайт.
#:
#: Замерено 2026-08-05 на живом провайдере, десять настоящих запросов по восемь
#: результатов: в девяти из десяти один домен занимал два места и больше, всего
#: 21 «лишний» результат из 80 — четверть выдачи. «Рецепт борща» дал четыре
#: страницы одного кулинарного сайта из восьми, «курс доллара» — три cbr.ru.
#:
#: Два, а не один: у запроса «курс доллара цб рф» две страницы cbr.ru — это
#: нормальный ответ, а не шум. Потолок отсекает третью и дальше.
_PER_HOST_LIMIT = 2


def _canonical_url(url: str) -> str:
    """Ключ, по которому два адреса — один документ.

    Схема, `www.`, метки перехода (`utm_*`, `yclid`, `from`), якорь и хвостовой
    слэш к содержимому страницы отношения не имеют. Замер на той же выдаче: сама
    по себе канонизация ловит всего 2 совпадения из 80 — зеркала оказались
    РАЗНЫМИ страницами одного сайта, а не вариантами одного адреса. Поэтому она
    здесь не главный механизм, а дешёвая страховка.
    """

    text = str(url or "").strip()
    if not text:
        return ""
    without_scheme = re.sub(r"(?i)^https?://", "", text)
    without_scheme = re.sub(r"(?i)^www\.", "", without_scheme)
    without_anchor = without_scheme.split("#", 1)[0]
    head, _, query = without_anchor.partition("?")
    if query:
        kept = [
            part
            for part in query.split("&")
            if part and not re.match(r"(?i)^(utm_[a-z]+|yclid|gclid|fbclid|from|ref|referrer)=", part)
        ]
        head = head + ("?" + "&".join(sorted(kept)) if kept else "")
    return head.rstrip("/").lower()


def _result_host(url: str) -> str:
    host = re.sub(r"(?i)^https?://", "", str(url or "").strip()).split("/", 1)[0]
    host = re.sub(r"(?i)^(www|m|amp)\.", "", host)
    return host.split(":", 1)[0].lower()


def _diversify(results: Sequence[SearchResult], limit: int) -> list[SearchResult]:
    """Убрать повторы адресов и не дать одному сайту занять всю выдачу.

    Отброшенные по потолку НЕ теряются: если после отбора мест осталось меньше,
    чем просили, они возвращаются в конец в исходном порядке. Иначе «не больше
    двух с сайта» превращалось бы в «меньше результатов», а это уже не
    разнообразие, а потеря — на узком запросе, где отвечает один сайт, выдача
    схлопнулась бы до двух строк.
    """

    picked: list[SearchResult] = []
    overflow: list[SearchResult] = []
    seen: set[str] = set()
    per_host: dict[str, int] = {}
    for item in results:
        key = _canonical_url(item.url)
        if not key or key in seen:
            continue
        seen.add(key)
        host = _result_host(item.url)
        if host and per_host.get(host, 0) >= _PER_HOST_LIMIT:
            overflow.append(item)
            continue
        per_host[host] = per_host.get(host, 0) + 1
        picked.append(item)
    if len(picked) < limit:
        picked.extend(overflow[: limit - len(picked)])
    return picked[:limit]


class WebSurfer:
    """Search and fetch public sources under strict byte and time budgets."""

    def __init__(self, settings: FridaySettings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        # Когда мы в последний раз стучались на каждый сайт. Вежливость к чужому
        # серверу: три страницы одного домена подряд, взятые в одну сотую
        # секунды, выглядят молотилкой, и блокируют за это адрес целиком.
        self._last_touch: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        # Правила сайта читаются один раз на процесс: спрашивать `robots.txt`
        # перед каждой страницей — удвоить нагрузку на чужой сервер ради
        # вежливости к нему же.
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        # Темп, названный самим сайтом (`Crawl-delay`), — он главнее умолчания.
        self._host_pause: dict[str, float] = {}

    def _default_pause(self) -> float:
        return float(getattr(self.settings, "web_host_pause_sec", 0.0) or 0.0)

    def _pause_for(self, host: str) -> float:
        """Сколько ждать перед этим сайтом: его просьба или наше умолчание.

        Названный сайтом темп берётся ТОЛЬКО если он больше нашего: `Crawl-delay`
        меньше нашей паузы — разрешение спешить, а не обязанность. Заодно
        замечено при проверке: стандартный разборщик понимает лишь ЦЕЛУЮ
        задержку, дробную (`0.1`) молча отбрасывает — так что до этого сравнения
        доходят только целые секунды.
        """

        return self._host_pause.get(host, self._default_pause())

    async def _be_polite_to(self, host: str) -> None:
        """Выдержать паузу перед повторным обращением к тому же сайту.

        Замок НА ХОСТ, а не общий: пауза для `ria.ru` не должна задерживать
        соседний `tass.ru` — иначе исследование по трём источникам, которое
        сейчас идёт параллельно, станет последовательным и втрое дольше.
        """

        pause = self._pause_for(host)
        if pause <= 0 or not host:
            return
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            waited = asyncio.get_running_loop().time() - self._last_touch.get(host, 0.0)
            if waited < pause:
                await asyncio.sleep(pause - waited)
            self._last_touch[host] = asyncio.get_running_loop().time()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(_FETCH_TIMEOUT, connect=10.0),
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html,text/plain,application/json;q=0.8,*/*;q=0.2",
                },
                follow_redirects=False,
                trust_env=False,
                limits=limits,
                transport=_PinnedAsyncHTTPTransport(
                    allow_private_networks=self.settings.web_allow_private_networks,
                    limits=limits,
                ),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @declares_search_filter_support("freshness")
    async def search(
        self,
        query: str,
        *,
        max_results: int = _DEFAULT_MAX_RESULTS,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
        source_class: str = "",
    ) -> list[SearchResult]:
        site, include_domains, exclude_domains, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        query = " ".join(str(query or "").split()).strip()
        source_class = normalize_search_source_class(source_class)
        if not query:
            return attested_search_results([], freshness=freshness)
        limit = max(1, min(int(max_results), 20))
        # Filtering after the provider is the enforcement boundary.  Ask the
        # same provider for bounded headroom so a few forbidden rows do not
        # needlessly fan the private query out to the next company.  Never relax
        # the filter merely to fill the requested number of slots.
        provider_limit = min(20, limit * 2) if include_domains or exclude_domains or source_class else limit
        results: list[SearchResult] = []

        refused: list[str] = []
        unsupported: dict[str, list[str]] = {}
        answered = False
        chain_options: dict[str, Any] = {"site": site, "freshness": freshness}
        # Preserve the exact legacy internal call when new filters are absent;
        # deployments can carry small local provider-chain wrappers.
        if include_domains:
            chain_options["include_domains"] = include_domains
        if exclude_domains:
            chain_options["exclude_domains"] = exclude_domains
        if lang:
            chain_options["lang"] = lang
        if region:
            chain_options["region"] = region
        if source_class:
            chain_options["source_class"] = source_class
        for name, provider in self._provider_chain(query, provider_limit, **chain_options):
            try:
                batch = await provider()
            except UnsupportedSearchFilterError as capability_miss:
                # A local capability miss is different from a network refusal.
                # Keep it structural so an all-unsupported chain cannot become
                # either an empty result or an unfiltered request.
                unsupported.setdefault(capability_miss.filter_name, []).append(name)
                LOGGER.info("Search provider %s lacks %s support", name, capability_miss.filter_name)
                continue
            except ProviderRefusedError as exc:
                LOGGER.warning("Search provider %s refused (%s)", name, type(exc).__name__)
                refused.append(name)
                continue
            except Exception as exc:  # noqa: BLE001 — падение провайдера тоже отказ
                LOGGER.warning("Search provider %s failed: %s", name, type(exc).__name__)
                refused.append(name)
                continue
            if site or include_domains or exclude_domains:
                # Even native provider parameters and query operators are hints
                # from an external service.  A non-matching URL never crosses
                # this API boundary; deny-list values themselves are never sent
                # to a provider at all.
                batch = [
                    item
                    for item in batch
                    if _result_matches_domains(
                        item,
                        site=site,
                        include_domains=include_domains,
                        exclude_domains=exclude_domains,
                    )
                ]
            if source_class:
                unfiltered_count = len(batch)
                batch = [item for item in batch if web_source_matches_class(item.url, source_class)]
                if unfiltered_count and not batch:
                    # A RU-only batch is not an answer to a foreign-source
                    # request.  Keep walking the provider chain; never relax the
                    # source class merely to return something.
                    LOGGER.info("Search provider %s returned no %s sources", name, source_class)
                    continue
            wikipedia_targets = (site,) if site else include_domains
            wikipedia_only = bool(wikipedia_targets) and all(
                domain == "wikipedia.org" or domain in _WIKIPEDIA_LANGUAGE_HOST.values()
                for domain in wikipedia_targets
            )
            if name == "wikipedia" and not batch and not wikipedia_only:
                # An empty encyclopaedia index is not evidence that the open web
                # has no answer.  It is honest emptiness only when the caller
                # explicitly limited the corpus to Wikipedia.
                refused.append(name)
                LOGGER.info("Wikipedia fallback had no web-wide answer")
                continue
            answered = True
            results.extend(batch)
            if results:
                break
            # Провайдер ответил честным нулём. Индексы у провайдеров разные,
            # поэтому спрашиваем следующего — но это уже не отказ.

        if not answered and unsupported:
            requested_order = (
                ("freshness", bool(freshness)),
                ("lang", bool(lang)),
                ("region", bool(region)),
                ("site", bool(site)),
                ("include_domains", bool(include_domains)),
                ("exclude_domains", bool(exclude_domains)),
            )
            filter_names = tuple(
                key for key, requested in requested_order if requested and key in unsupported
            )
            if not filter_names:
                filter_names = tuple(unsupported)
            unsupported_providers = tuple(
                dict.fromkeys(
                    provider for filter_name in filter_names for provider in unsupported[filter_name]
                )
            )
            if filter_names == ("freshness",):
                raise FreshnessUnavailableError(
                    unsupported_providers=unsupported_providers,
                    refused_providers=refused,
                )
            raise SearchFilterUnavailableError(
                filter_names=filter_names,
                unsupported_providers=unsupported_providers,
                refused_providers=refused,
            )
        if not answered and refused:
            # Ни один не ответил по существу. Сказать «ничего не найдено» здесь
            # значило бы выдать чужой отказ за факт об интернете.
            raise AllProvidersRefusedError("поисковые провайдеры не ответили: " + ", ".join(refused))

        return attested_search_results(_diversify(results, limit), freshness=freshness)

    async def _search_duckduckgo_html(
        self,
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
    ) -> list[SearchResult]:
        site, include_domains, _, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        _reject_unsupported_freshness("duckduckgo", freshness)
        _reject_unsupported_locales("duckduckgo", lang=lang, region=region)
        provider_query = _query_with_domain_filter(
            query,
            site=site,
            include_domains=include_domains,
        )
        client = await self._get_client()
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": provider_query},
            timeout=_SEARCH_TIMEOUT,
        )
        # 202 — анти-бот заглушка, и она приходит чаще, чем выдача: 19 ответов
        # из 20 в замере. Раньше `raise_for_status` пропускал её как успех.
        if response.status_code in _REFUSAL_STATUS or response.status_code >= 500:
            raise ProviderRefusedError(f"duckduckgo answered {response.status_code}")
        response.raise_for_status()
        try:
            soup = BeautifulSoup(response.text, "lxml")
            results: list[SearchResult] = []
            for node in soup.select(".result"):
                anchor = node.select_one(".result__title a, .result__a")
                snippet_node = node.select_one(".result__snippet")
                if not anchor:
                    continue
                title = anchor.get_text(" ", strip=True)
                href = _duckduckgo_target(str(anchor.get("href", "")))
                if title and href:
                    results.append(
                        SearchResult(
                            title=title,
                            url=href,
                            snippet=snippet_node.get_text(" ", strip=True) if snippet_node else "",
                            source="duckduckgo",
                        )
                    )
                if len(results) >= limit:
                    break
            if not results:
                # 200 без разметки результатов — тоже заглушка: на заведомо
                # бессмысленный запрос DuckDuckGo всё равно отдаёт десять ссылок.
                raise ProviderRefusedError("duckduckgo answered without result markup")
            return results
        except ProviderRefusedError:
            raise
        except Exception as exc:
            raise ProviderRefusedError(f"duckduckgo failed: {type(exc).__name__}") from exc

    async def _search_wikipedia(
        self,
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
        source_class: str = "",
    ) -> list[SearchResult]:
        """Последнее звено цепочки: энциклопедия вместо пустоты.

        Замерено 2026-08-02, тем же набором из двадцати запросов и тем же
        критерием (доля непустых выдач):

            бесплатные HTML-провайдеры при СЕРИИ запросов разваливаются —
            brave-html упирается в 429 и даёт 1/20, lite-версия DuckDuckGo
            2/20 (18 ответов — 202). Поодиночке они отвечают (9/10 и 10/10),
            и первый замер это скрыл: провайдер надо мерить нагрузкой, а не
            одним запросом.

            Wikipedia API — 10/10, стабильно, без ключа и без анти-бота.

        Она отвечает не на всё: «курс доллара сегодня» и «погода завтра» —
        не её вопросы. Но «кто такой», «что такое», «высота», «население» —
        именно её, и на них она отвечает всегда. Когда все поисковики отказали,
        энциклопедическая статья лучше, чем «до интернета не дотянуться».

        Правило Wikimedia про User-Agent соблюдается: осмысленное имя и адрес
        проекта, а не подделка под браузер.
        """
        # CirrusSearch can filter by page creation/edit time, but that is not the
        # publication freshness promised by a public-web search.  Treating an old
        # article edited today as fresh would be the same stale leak as ignoring
        # the window altogether.
        site, include_domains, exclude_domains, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        _reject_unsupported_freshness("wikipedia", freshness)
        _reject_unsupported_locales(
            "wikipedia",
            lang=lang,
            region=region,
            languages=frozenset(_WIKIPEDIA_LANGUAGE_HOST),
        )
        source_class = normalize_search_source_class(source_class)
        effective_includes = (site,) if site else include_domains
        languages = []
        default_languages = ("en",) if source_class == "foreign" else ("ru", "en")
        for language in (lang,) if lang else default_languages:
            host = _WIKIPEDIA_LANGUAGE_HOST[language]
            if effective_includes and not any(
                _host_matches_domain(host, domain) for domain in effective_includes
            ):
                continue
            if any(_host_matches_domain(host, domain) for domain in exclude_domains):
                continue
            languages.append(language)
        if not languages:
            # CirrusSearch indexes Wikipedia, not an arbitrary requested domain.
            # A local capability miss is not an honest empty result about the web.
            filter_name = "site" if site else "include_domains" if include_domains else "exclude_domains"
            raise UnsupportedSearchFilterError(provider="wikipedia", filter_name=filter_name)
        client = await self._get_client()
        for language in languages:
            try:
                response = await client.get(
                    f"https://{language}.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "srlimit": max(1, min(limit, 10)),
                        "format": "json",
                    },
                    headers={"User-Agent": _WIKIPEDIA_USER_AGENT},
                    timeout=_SEARCH_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001 — сетевой сбой это отказ, не пустота
                raise ProviderRefusedError(f"wikipedia failed: {type(exc).__name__}") from exc
            if response.status_code in _REFUSAL_STATUS or response.status_code >= 500:
                raise ProviderRefusedError(f"wikipedia answered {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderRefusedError("wikipedia answered with non-JSON") from exc
            hits = ((payload.get("query") or {}).get("search")) or []
            results: list[SearchResult] = []
            for hit in hits:
                title = str(hit.get("title") or "").strip()
                if not title:
                    continue
                # Сниппет приходит с разметкой подсветки — человеку и модели она
                # не нужна, а в поисковую выдачу попадёт как мусор.
                snippet = re.sub(r"<[^>]+>", "", str(hit.get("snippet") or "")).strip()
                results.append(
                    SearchResult(
                        title=title,
                        url=f"https://{language}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                        snippet=snippet,
                        source=f"wikipedia-{language}",
                    )
                )
                if len(results) >= limit:
                    break
            if results:
                return results
        # Обе языковые версии ответили честным нулём — это не отказ.
        return []

    def _provider_chain(
        self,
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
        source_class: str = "",
    ) -> list[tuple[str, Callable[[], Awaitable[list[SearchResult]]]]]:
        """Провайдеры в порядке замеренной надёжности.

        Числа сняты 2026-08-01 на одном наборе из 20 разнообразных запросов,
        критерий объявлен до замера — доля запросов с непустой выдачей:

            Яндекс (ключ владельца)   20/20
            Brave по ключу              — ключа нет, но ветка сохранена
            Brave по HTML             1/20 при серии (9/10 поодиночке — 429)
            DuckDuckGo                1/20  (19 ответов — HTTP 202)
            DuckDuckGo lite           2/20  (18 ответов — HTTP 202)
            Wikipedia API            10/10  — без ключа, без анти-бота
            Mojeek / Bing / Startpage 0/20  — в цепочку не берутся

        Главный урок замера 2026-08-02: провайдера надо мерить СЕРИЕЙ, а не
        одним запросом. Поодиночке brave-html давал 9 из 10 и выглядел рабочим
        резервом; на двадцати подряд он упирается в 429, и вся цепочка отдаёт
        1/20. То есть настоящего резерва у бесплатных HTML-провайдеров нет.

        Слабые провайдеры оставлены намеренно: они отвечают на ПЕРВЫЕ запросы,
        а этого хватает, когда Яндекс отказал ненадолго. Настоящий резерв —
        ключ Brave/Tavily/Serper: ветки готовы, нужен только ключ.
        """
        source_class = normalize_search_source_class(source_class)
        chain: list[tuple[str, Callable[[], Awaitable[list[SearchResult]]]]] = []
        provider_options: dict[str, Any] = {"site": site, "freshness": freshness}
        if include_domains:
            provider_options["include_domains"] = include_domains
        if exclude_domains:
            provider_options["exclude_domains"] = exclude_domains
        if lang:
            provider_options["lang"] = lang
        if region:
            provider_options["region"] = region

        def call(
            provider: Callable[..., Awaitable[list[SearchResult]]],
            *,
            source_aware: bool = False,
        ) -> Callable[[], Awaitable[list[SearchResult]]]:
            options = dict(provider_options)
            if source_aware and source_class:
                options["source_class"] = source_class
            return lambda: provider(query, limit, **options)

        if self.settings.yandex_search_api_key:
            chain.append(("yandex", call(self._search_yandex, source_aware=True)))
        if self.settings.brave_search_api_key:
            chain.append(("brave", call(self._search_brave)))
        if self.settings.tavily_api_key:
            chain.append(("tavily", call(self._search_tavily)))
        if self.settings.serper_api_key:
            chain.append(("serper", call(self._search_serper)))
        chain.append(("brave-html", call(self._search_brave_html)))
        chain.append(("duckduckgo", call(self._search_duckduckgo_html)))
        # Последним — энциклопедия: она отвечает не на всё, но отвечает ВСЕГДА,
        # тогда как бесплатные HTML-провайдеры при серии запросов дают 1-2 из 20.
        chain.append(("wikipedia", call(self._search_wikipedia, source_aware=True)))
        return chain

    async def _search_brave_html(
        self,
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
    ) -> list[SearchResult]:
        """Brave без ключа. Отвечает не всегда, но вчетверо чаще DuckDuckGo."""
        # The documented freshness parameter belongs to Brave's API endpoint.
        # Do not assume that the consumer HTML form accepts the same contract.
        site, include_domains, _, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        _reject_unsupported_freshness("brave-html", freshness)
        _reject_unsupported_locales("brave-html", lang=lang, region=region)
        provider_query = _query_with_domain_filter(
            query,
            site=site,
            include_domains=include_domains,
        )
        client = await self._get_client()
        response = await client.get(
            "https://search.brave.com/search",
            params={"q": provider_query},
            timeout=_SEARCH_TIMEOUT,
        )
        if response.status_code in _REFUSAL_STATUS or response.status_code >= 500:
            raise ProviderRefusedError(f"brave-html answered {response.status_code}")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        results: list[SearchResult] = []
        for node in soup.select("div.snippet[data-type='web'], #results .snippet"):
            anchor = node.select_one("a[href^='http']")
            if anchor is None:
                continue
            url = str(anchor.get("href") or "")
            if not url:
                continue
            snippet_node = node.select_one(".snippet-description, .snippet-content")
            results.append(
                SearchResult(
                    title=anchor.get_text(" ", strip=True)[:300] or url,
                    url=url,
                    snippet=snippet_node.get_text(" ", strip=True) if snippet_node else "",
                    source="brave-html",
                )
            )
            if len(results) >= limit:
                break
        if not results:
            # Разметка Brave без результатов — это его анти-бот страница, а не
            # утверждение, что в интернете ничего нет.
            raise ProviderRefusedError("brave-html answered without result markup")
        return results

    def _yandex_segment(self, query: str, *, source_class: str = "") -> str:
        """Какой сегмент спрашивать: русский или международный.

        Жёсткий `SEARCH_TYPE_RU` был не про язык индекса — русский сегмент
        находит и raft.github.io, и autohome.com.cn, — а про региональное
        ранжирование. Но оно решает: замерено на живом вопросе владельца «какие
        новости не из ру сегмента есть за сегодня и вчера?» — выдача пришла из
        lenta.ru и rbc.ru, то есть ровно то, о чём просили НЕ давать.

        Правило простое и проверяемое: явная просьба о зарубежных источниках
        относится к текущему запросу и потому сильнее общего регионального
        default. Для остальных запросов сохраняется настроенный сегмент.
        """
        source_class = normalize_search_source_class(source_class)
        if source_class == "foreign" or _ASKS_FOR_FOREIGN_SOURCES.search(query):
            return "SEARCH_TYPE_COM"
        configured = str(self.settings.yandex_search_type or "").strip()
        if configured:
            return configured
        letters = [character for character in query if character.isalpha()]
        if letters and not any(
            "а" <= character.casefold() <= "я" or character == "ё" for character in letters
        ):
            # Ни одной кириллической буквы — запрос набран на другом языке.
            return "SEARCH_TYPE_COM"
        return "SEARCH_TYPE_RU"

    async def _search_yandex(
        self,
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
        source_class: str = "",
    ) -> list[SearchResult]:
        """Яндекс Search API v2 — синхронный поиск, ответ приходит XML-ом в base64.

        Ключ Yandex Cloud (`AQVN…`) сам несёт привязку к каталогу, поэтому
        `folderId` в теле не нужен — проверено на живом ключе владельца.
        """
        site, include_domains, _, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        if region and region not in _YANDEX_REGION_TO_SEARCH_TYPE:
            raise UnsupportedSearchFilterError(provider="yandex", filter_name="region")
        provider_query = _query_with_yandex_filters(
            query,
            site=site,
            include_domains=include_domains,
            freshness=freshness,
            lang=lang,
        )
        search_type = _YANDEX_REGION_TO_SEARCH_TYPE.get(region) or self._yandex_segment(
            query, source_class=source_class
        )
        client = await self._get_client()
        response = await client.post(
            "https://searchapi.api.cloud.yandex.net/v2/web/search",
            json={
                "query": {
                    "searchType": search_type,
                    "queryText": provider_query,
                },
                "groupSpec": {
                    "groupMode": "GROUP_MODE_FLAT",
                    "groupsOnPage": limit,
                    "docsInGroup": 1,
                },
            },
            headers={"Authorization": f"Api-Key {self.settings.yandex_search_api_key}"},
            timeout=_SEARCH_TIMEOUT,
        )
        if response.status_code in _REFUSAL_STATUS or response.status_code >= 500:
            raise ProviderRefusedError(f"yandex answered {response.status_code}")
        response.raise_for_status()
        try:
            raw = str(response.json().get("rawData") or "")
        except ValueError as exc:
            raise ProviderRefusedError("yandex answered with non-JSON") from exc
        if not raw:
            raise ProviderRefusedError("yandex answered without rawData")
        try:
            xml = base64.b64decode(raw).decode("utf-8", "replace")
        except (ValueError, binascii.Error) as exc:
            raise ProviderRefusedError("yandex rawData is not base64") from exc
        soup = BeautifulSoup(xml, "xml")
        error = soup.find("error")
        if error is not None:
            # Исчерпанная квота и неверный ключ приходят ВНУТРИ 200-го ответа.
            raise ProviderRefusedError(f"yandex error: {error.get_text(' ', strip=True)[:120]}")
        results: list[SearchResult] = []
        for doc in soup.find_all("doc"):
            url_node = doc.find("url")
            if url_node is None:
                continue
            url = url_node.get_text(strip=True)
            title_node = doc.find("title")
            passage = doc.find("passage")
            headline = doc.find("headline")
            snippet_node = passage if passage is not None else headline
            if not url:
                continue
            results.append(
                SearchResult(
                    # `<hlword>` вокруг совпавших слов — разметка подсветки;
                    # get_text склеивает её обратно в обычную строку.
                    title=title_node.get_text(" ", strip=True) if title_node else url,
                    url=url,
                    snippet=snippet_node.get_text(" ", strip=True) if snippet_node else "",
                    source="yandex",
                )
            )
            if len(results) >= limit:
                break
        return results

    async def _search_brave(
        self,
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
    ) -> list[SearchResult]:
        site, include_domains, _, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        _reject_unsupported_locales(
            "brave",
            lang=lang,
            region=region,
            languages=_BRAVE_SEARCH_LANGUAGES,
            regions=_BRAVE_COUNTRIES,
        )
        provider_query = _query_with_domain_filter(
            query,
            site=site,
            include_domains=include_domains,
        )
        if include_domains and (len(provider_query) > 400 or len(provider_query.split()) > 50):
            raise UnsupportedSearchFilterError(provider="brave", filter_name="include_domains")
        params: dict[str, str | int] = {"q": provider_query, "count": limit}
        if freshness:
            params["freshness"] = _BRAVE_FRESHNESS[freshness]
        if lang:
            params["search_lang"] = lang
        if region:
            params["country"] = region.upper()
        client = await self._get_client()
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.settings.brave_search_api_key,
            },
            timeout=_SEARCH_TIMEOUT,
        )
        # Неверный ключ и исчерпанный лимит — отказ, а не «ничего не найдено».
        if response.status_code in _REFUSAL_STATUS or response.status_code >= 500:
            raise ProviderRefusedError(f"brave answered {response.status_code}")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRefusedError("brave answered with non-JSON") from exc
        web_payload = payload.get("web") if isinstance(payload, dict) else None
        items = web_payload.get("results") if isinstance(web_payload, dict) else None
        if not isinstance(items, list):
            raise ProviderRefusedError("brave answered without a result container")
        return [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("description", "")),
                source="brave",
            )
            for item in items[:limit]
            if isinstance(item, dict) and item.get("url")
        ]

    async def _search_tavily(
        self,
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
    ) -> list[SearchResult]:
        site, include_domains, _, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        _reject_unsupported_locales(
            "tavily",
            lang=lang,
            region=region,
            regions=frozenset(_TAVILY_COUNTRY_BY_ISO),
        )
        payload: dict[str, Any] = {
            "api_key": self.settings.tavily_api_key,
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
        }
        effective_includes = (site,) if site else include_domains
        if effective_includes:
            # Tavily has a native domain allow-list; unlike a textual operator it
            # does not depend on query parsing.
            payload["include_domains"] = list(effective_includes)
        if freshness:
            payload["time_range"] = freshness
        if region:
            # Officially a country boost for general web search, not a strict
            # geographic source filter; the public tool describes the same.
            payload["topic"] = "general"
            payload["country"] = _TAVILY_COUNTRY_BY_ISO[region]
        client = await self._get_client()
        response = await client.post(
            "https://api.tavily.com/search",
            json=payload,
            timeout=_SEARCH_TIMEOUT,
        )
        if response.status_code in _REFUSAL_STATUS or response.status_code >= 500:
            raise ProviderRefusedError(f"tavily answered {response.status_code}")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRefusedError("tavily answered with non-JSON") from exc
        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ProviderRefusedError("tavily answered without a result container")
        return [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", "")),
                source="tavily",
            )
            for item in items[:limit]
            if isinstance(item, dict) and item.get("url")
        ]

    async def _search_serper(
        self,
        query: str,
        limit: int,
        *,
        site: str = "",
        freshness: str = "",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        lang: str = "",
        region: str = "",
    ) -> list[SearchResult]:
        site, include_domains, _, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        _reject_unsupported_locales(
            "serper",
            lang=lang,
            region=region,
            languages=_ISO_639_1,
            regions=_ISO_3166_1_ALPHA2,
        )
        provider_query = _query_with_domain_filter(
            query,
            site=site,
            include_domains=include_domains,
        )
        payload: dict[str, Any] = {"q": provider_query, "num": limit}
        if freshness:
            payload["tbs"] = _SERPER_FRESHNESS[freshness]
        if lang:
            # Serper documents this as search localisation.  It is deliberately
            # not described as a hard document-language guarantee.
            payload["hl"] = lang
        if region:
            payload["gl"] = region
        client = await self._get_client()
        response = await client.post(
            "https://google.serper.dev/search",
            json=payload,
            headers={"X-API-KEY": self.settings.serper_api_key},
            timeout=_SEARCH_TIMEOUT,
        )
        if response.status_code in _REFUSAL_STATUS or response.status_code >= 500:
            raise ProviderRefusedError(f"serper answered {response.status_code}")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRefusedError("serper answered with non-JSON") from exc
        recognised_sections = {
            "answerBox",
            "images",
            "knowledgeGraph",
            "news",
            "organic",
            "peopleAlsoAsk",
            "places",
            "relatedSearches",
            "searchParameters",
            "shopping",
            "videos",
        }
        if not isinstance(payload, dict) or not recognised_sections.intersection(payload):
            raise ProviderRefusedError("serper answered without a recognised result container")
        items = payload.get("organic", [])
        if not isinstance(items, list):
            raise ProviderRefusedError("serper answered with an invalid organic result container")
        return [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("link", "")),
                snippet=str(item.get("snippet", "")),
                source="serper",
            )
            for item in items[:limit]
            if isinstance(item, dict) and item.get("link")
        ]

    async def _validate_url(self, url: str) -> str:
        return await asyncio.to_thread(
            validate_public_url,
            url,
            allow_private_networks=self.settings.web_allow_private_networks,
        )

    async def _request_bytes(self, url: str) -> tuple[bytes, httpx.Response, str]:
        client = await self._get_client()
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            current = await self._validate_url(current)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise httpx.HTTPStatusError(
                            "Redirect without Location header",
                            request=response.request,
                            response=response,
                        )
                    current = urllib.parse.urljoin(current, location)
                    continue

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.settings.web_max_response_bytes:
                        raise ValueError(
                            f"Response exceeds {self.settings.web_max_response_bytes} byte limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), response, current
        raise ValueError(f"Too many redirects (>{_MAX_REDIRECTS})")

    async def _robots_verdict(self, url: str) -> str:
        """Разрешает ли сайт брать эту страницу. Пустая строка — можно.

        Читается ОДИН раз на сайт и держится в памяти процесса: спрашивать
        `robots.txt` перед каждой страницей значило бы удвоить число обращений к
        чужому серверу ради вежливости к нему же.

        Недоступный или битый `robots.txt` означает РАЗРЕШЕНО. Обратное правило
        («не смогли прочитать — не пойдём») делало бы любой сбой сети запретом на
        весь интернет, и человек получал бы отказ вместо страницы.
        """

        host = _host_of(url)
        if not host:
            return ""
        cached = self._robots.get(host)
        if cached is None:
            parser = urllib.robotparser.RobotFileParser()
            try:
                scheme = "https" if str(url or "").lower().startswith("https") else "http"
                # СВОЙ срок, и короткий. Без него сервер, отдающий по байту в
                # пять секунд, держал бы нас на `robots.txt` — то есть вежливость
                # к чужому серверу открыла бы дыру, которую страничный бюджет уже
                # закрыл. Поймано сторожем `test_a_drip_feeding_server…`: загрузка
                # растянулась с секунд до тридцати. Файл правил крошечный,
                # десяти секунд ему более чем достаточно.
                # Срок берётся ДОЛЕЙ от страничного бюджета, а не константой:
                # иначе чтение правил переживает бюджет самой страницы и правило
                # «одна загрузка не длится дольше X» перестаёт быть правдой.
                # Ровно это и поймал сторож: с фиксированными десятью секундами
                # загрузка при урезанном бюджете растянулась на тридцать.
                async with asyncio.timeout(min(_ROBOTS_TIMEOUT, _FETCH_TOTAL_BUDGET / 4)):
                    body, response, _ = await self._request_bytes(f"{scheme}://{host}/robots.txt")
                if 200 <= response.status_code < 300:
                    parser.parse(body.decode("utf-8", errors="replace").splitlines())
                else:
                    # 404 — «правил нет», и это разрешение, а не молчание.
                    parser.parse([])
            except Exception as exc:  # noqa: BLE001 — сбой чтения правил не запрет
                LOGGER.debug("robots.txt недоступен для %s: %s", host, type(exc).__name__)
                parser.parse([])
            self._robots[host] = parser
            cached = parser
        # Заголовок мы шлём браузерный, поэтому и правило спрашиваем для «*»:
        # представляться одним, а правила читать для другого — обман.
        if not cached.can_fetch("*", url):
            return "Сайт запрещает автоматический доступ к этой странице (robots.txt)"
        delay = cached.crawl_delay("*")
        if delay:
            # Хозяин сайта назвал свой темп — он главнее нашего умолчания.
            self._host_pause[host] = max(float(delay), self._default_pause())
        return ""

    async def fetch(self, url: str, *, max_length: int = 50_000) -> FetchResult:
        requested = str(url or "").strip()
        forbidden = await self._robots_verdict(requested)
        if forbidden:
            # Отказ НАЗЫВАЕТСЯ. Пустая страница без причины читалась бы как
            # «там ничего нет», и модель пересказала бы это человеку как факт.
            return FetchResult(url=_failure_url(requested), title="", text="", text_length=0, error=forbidden)
        await self._be_polite_to(_host_of(requested))
        try:
            # httpx timeouts are PER OPERATION: `read=20` means «no more than twenty
            # seconds between chunks», not «no more than twenty seconds». A server
            # dripping one byte every five seconds held the connection indefinitely —
            # measured at 65 s and still open — and the 5 MiB ceiling does not help,
            # because at that rate it would take years to reach. Agent calls are cut
            # by the kernel's own 30-second timeout, but `POST /api/ingest/url` calls
            # this directly and had no ceiling at all.
            async with asyncio.timeout(_FETCH_TOTAL_BUDGET):
                body, response, final_url = await self._request_bytes(requested)
            status = response.status_code
            if status < 200 or status >= 300:
                return FetchResult(
                    url=_failure_url(final_url),
                    title="",
                    text="",
                    text_length=0,
                    status_code=status,
                    error=f"HTTP {status}",
                )
            # `.strip()` because "text/html ; charset=utf-8" is legal and used to be
            # rejected as `"text/html "`. And no `content_type and`: an allowlist that
            # only applies when the server chose to declare a type is not an allowlist.
            # A response with no Content-Type at all skipped the check entirely, so
            # arbitrary bytes were decoded with errors="replace" and stored as the text
            # of a Raw Object — the same bytes are refused the moment the server labels
            # them application/octet-stream.
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            if content_type not in _ALLOWED_CONTENT_TYPES:
                return FetchResult(
                    url=_failure_url(final_url),
                    title="",
                    text="",
                    text_length=0,
                    status_code=status,
                    # A remote Content-Type is attacker-controlled.  Returning it
                    # through the tool/API would turn an error header into model
                    # context and durable evidence.
                    error="unsupported_content_type",
                )
            text_budget = max(1_000, min(int(max_length), self.settings.max_extracted_text_chars))
            if content_type == "application/pdf":
                # Parse after the download budget: a slow PDF must not borrow from
                # the byte-stream ceiling, and parse_timeout must not look like a
                # network Timeout (ingest/url would report «empty content»).
                try:
                    # Два предохранителя, и оба нужны. Внутренний срок
                    # (`parse_budget_sec`) реально ОСТАНАВЛИВАЕТ разбор между
                    # страницами; внешний таймаут остаётся страховкой на случай,
                    # когда одна страница молотит дольше всего бюджета — прервать
                    # pypdf внутри страницы нечем. Запас внешнему даётся намеренно,
                    # чтобы обычный случай завершался изнутри, с частичным текстом,
                    # а не выглядел отказом.
                    parse_budget = getattr(self.settings, "pdf_parse_budget_sec", _PDF_PARSE_BUDGET)
                    async with asyncio.timeout(parse_budget * 2):
                        text, title, parse_error = await asyncio.to_thread(
                            self._extract_pdf_text,
                            body,
                            max_text_chars=text_budget,
                            parse_budget_sec=parse_budget,
                        )
                except TimeoutError:
                    return FetchResult(
                        url=_failure_url(final_url),
                        title="",
                        text="",
                        text_length=0,
                        status_code=status,
                        error="parse_timeout",
                    )
                if parse_error == "parse_truncated":
                    # Текст ЕСТЬ, но он неполон — и об этом говорится, а не
                    # умалчивается. Отдавать его как целый значило бы принять
                    # первые страницы за весь документ; отдавать пустоту —
                    # выбросить прочитанное. Признак едет вместе с текстом.
                    return FetchResult(
                        url=final_url,
                        title=title,
                        text=text,
                        text_length=len(text),
                        status_code=status,
                        error="",
                        truncated=True,
                    )
                if parse_error:
                    return FetchResult(
                        url=_failure_url(final_url),
                        title="",
                        text="",
                        text_length=0,
                        status_code=status,
                        error=parse_error,
                    )
            else:
                encoding = response.encoding or "utf-8"
                raw_text = body.decode(encoding, errors="replace")
                if "html" in content_type or "<html" in raw_text[:500].casefold():
                    text, title = self._extract_text_from_html(raw_text)
                elif "json" in content_type:
                    try:
                        text = json.dumps(json.loads(raw_text), ensure_ascii=False, indent=2)
                    except json.JSONDecodeError:
                        text = raw_text
                    title = ""
                else:
                    text, title = raw_text, ""
            text_truncated = len(text) > text_budget
            text = text[:text_budget]
            return FetchResult(
                url=final_url,
                title=title,
                text=text,
                text_length=len(text),
                status_code=status,
                truncated=text_truncated,
            )
        except (httpx.TimeoutException, TimeoutError):
            # TimeoutError explicitly: it is NOT a subclass of httpx.TimeoutException,
            # and `str(TimeoutError())` is the empty string — falling through to the
            # generic handler would return a blank error, which `POST /api/ingest/url`
            # then reports as «empty content» instead of a timeout.
            return FetchResult(_failure_url(requested), "", "", 0, error="Timeout")
        except UnsafeURLError:
            # UnsafeURLError may contain the hostname.  More importantly, future
            # validation errors may quote the full requested URL.  The caller needs
            # a stable reason, never the exception text.
            return FetchResult(_failure_url(requested), "", "", 0, error="blocked_url")
        except Exception as exc:
            LOGGER.info("Web fetch failed for %s: %s", _log_safe_host(requested), type(exc).__name__)
            # httpx exception strings include request URLs, including query tokens.
            # This field reaches the model, API responses and evidence; keep only
            # the exception class as an allowlisted diagnostic signal.
            return FetchResult(
                _failure_url(requested),
                "",
                "",
                0,
                error=f"fetch_failed:{type(exc).__name__}",
            )

    @declares_search_filter_support("freshness")
    async def research(
        self,
        query: str,
        *,
        max_sources: int = _DEFAULT_MAX_SOURCES,
        freshness: str = "",
        source_class: str = "",
    ) -> dict[str, Any]:
        freshness = normalize_search_freshness(freshness)
        source_class = normalize_search_source_class(source_class)

        def attested_report(report: dict[str, Any]) -> dict[str, Any]:
            if freshness:
                report["freshness"] = freshness
                report[SEARCH_FILTER_ATTESTATION_KEY] = {"freshness": freshness}
            return report

        source_limit = max(1, min(int(max_sources), 8))
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        try:
            async with asyncio.timeout(_RESEARCH_TOTAL_BUDGET):
                search_options: dict[str, Any] = {"max_results": source_limit * 2}
                if freshness:
                    search_options["freshness"] = freshness
                if source_class:
                    search_options["source_class"] = source_class
                if freshness and not search_callable_supports_filter(self.search, "freshness"):
                    # ``**kwargs`` is intentionally insufficient here: old
                    # wrappers accept it while silently dropping new values.
                    raise FreshnessUnavailableError(
                        unsupported_providers=("research-search-adapter",),
                    )
                # Absent filters remain absent for the legacy override seam.
                results = await self.search(query, **search_options)
                if freshness and not search_filter_is_attested(results, "freshness", freshness):
                    # The adapter claimed capability but did not prove that the
                    # returned batch actually belongs to the requested window.
                    raise FreshnessUnavailableError(
                        unsupported_providers=("research-search-adapter-attestation",),
                        refused_providers=("research-search-adapter",),
                    )
        except SearchFilterUnavailableError:
            # Keep the provider capability boundary structural for the kernel;
            # it must not turn an unsupported window into ordinary emptiness.
            raise
        except TimeoutError:
            return attested_report(
                {
                    "query": query,
                    "sources": [],
                    "target_sources": 0,
                    "requested_sources": 0,
                    "completed_sources": 0,
                    "timed_out_sources": 0,
                    "failed_sources": 0,
                    "search_timed_out": True,
                    "summary": "Public-source search timed out before pages could be fetched.",
                }
            )
        except AllProvidersRefusedError as exc:
            # «Ничего не найдено» здесь было бы утверждением о мире, которого
            # никто не проверял: искать не удалось вовсе.
            # Без текста запроса: `docs/SECURITY.md` обещает, что здесь в журнал
            # идут только имя хоста и класс исключения. Поисковая строка — это
            # персональные данные, а журнал не чистится ни удалением знания, ни
            # `purge`.
            LOGGER.warning("Research search refused (%d chars): %s", len(query), type(exc).__name__)
            return attested_report(
                {
                    "query": query,
                    "sources": [],
                    "target_sources": 0,
                    "requested_sources": 0,
                    "completed_sources": 0,
                    "timed_out_sources": 0,
                    "failed_sources": 0,
                    "search_timed_out": False,
                    "search_failed": True,
                    "summary": (
                        "Поисковые системы не ответили — доступ к интернету сейчас не работает. "
                        "Это НЕ значит, что по запросу ничего нет."
                    ),
                }
            )
        if not results:
            return attested_report(
                {
                    "query": query,
                    "sources": [],
                    "target_sources": 0,
                    "requested_sources": 0,
                    "completed_sources": 0,
                    "timed_out_sources": 0,
                    "failed_sources": 0,
                    "search_timed_out": False,
                    "summary": "No search results found.",
                }
            )

        # Прямые источники — впереди страниц. Котировка и прогноз на обычных
        # сайтах рисуются скриптом, и в тексте их нет: замерено на «сколько
        # стоит нефть Brent» и «какой курс биткоина» — одиннадцать из двенадцати
        # вопросов дали конкретное значение, а эти два вернули «цифра в тексте
        # не раскрылась». Здесь ЧИСЛА приходят числами, из официальных и
        # общепризнанных источников, со ссылкой для человека.
        direct: list[dict[str, Any]] = []
        if not freshness:
            try:
                remaining_total = max(0.0, _RESEARCH_TOTAL_BUDGET - (loop.time() - started_at))
                fetch_reserve = min(_RESEARCH_FETCH_BUDGET, remaining_total)
                direct_budget = min(
                    _RESEARCH_DIRECT_BUDGET,
                    max(0.0, remaining_total - fetch_reserve),
                )
                if direct_budget > 0:
                    async with asyncio.timeout(direct_budget):
                        direct = await direct_answers(query, await self._get_client())
            except TimeoutError:
                direct = []
            except Exception as exc:  # noqa: BLE001 — прямой источник не должен ронять исследование
                LOGGER.warning("Прямые источники данных не ответили (%s)", type(exc).__name__)
        # Direct adapters have no publication-window contract.  A current quote
        # may look harmless, but mixing an unattested row into an explicitly
        # filtered report would make the report-wide freshness proof false.
        if source_class:
            # Direct adapters bypass ``search``.  Apply the same DNS contract
            # before their fact rows join the report (for example a rewritten
            # query must not add a CBR .ru quote to a foreign-news turn).
            direct = [
                item
                for item in direct
                if isinstance(item, Mapping)
                and web_source_matches_class(str(item.get("url") or ""), source_class)
            ]

        target_sources = min(source_limit, len(results))
        selected = list(results[:target_sources])
        spare = list(results[target_sources : source_limit * 2])
        complete: list[dict[str, Any]] = []
        partial: list[tuple[dict[str, Any], bool]] = []
        complete_keys: set[str] = set()
        partial_keys: set[str] = set()
        failed_hosts: set[str] = set()
        attempted_hosts: set[str] = set()
        requested_sources = 0
        failed_sources = 0
        timed_out_sources = 0

        def record_item(item: Mapping[str, Any], *, attempted: bool) -> None:
            """Admit complete evidence and hold useful partial evidence for fallback."""

            nonlocal failed_sources
            url = str(item.get("url") or "")
            key = canonical_public_web_url_key(url)
            text = str(item.get("text") or "").strip()
            status_code = item.get("status_code")
            error = item.get("error")
            truncated = item.get("truncated")
            text_length = item.get("text_length")
            valid = bool(
                key
                and text
                and isinstance(status_code, int)
                and not isinstance(status_code, bool)
                and 200 <= status_code < 300
                and isinstance(error, str)
                and not error.strip()
                and isinstance(truncated, bool)
                and isinstance(text_length, int)
                and not isinstance(text_length, bool)
                and text_length >= len(text)
            )
            if not valid:
                if attempted:
                    failed_sources += 1
                    host = _host_of(url)
                    if host:
                        failed_hosts.add(host)
                return
            incomplete = bool(
                truncated is True
                or isinstance(text_length, int)
                and not isinstance(text_length, bool)
                and text_length > len(text)
            )
            if incomplete:
                if key in complete_keys or key in partial_keys:
                    if attempted:
                        failed_sources += 1
                    return
                partial_keys.add(key)
                partial.append((dict(item), attempted))
                return
            if key in complete_keys:
                if attempted:
                    failed_sources += 1
                return
            if key in partial_keys:
                for index, (old_item, old_attempted) in enumerate(partial):
                    if canonical_public_web_url_key(str(old_item.get("url") or "")) != key:
                        continue
                    partial.pop(index)
                    partial_keys.remove(key)
                    if old_attempted:
                        failed_sources += 1
                    break
            complete_keys.add(key)
            complete.append(dict(item))

        for item in direct:
            if isinstance(item, Mapping):
                record_item(item, attempted=False)

        async def fetch_batch(batch: Sequence[SearchResult], *, timeout: float) -> None:
            """Fetch one deficit-sized wave and always reap every child task."""

            nonlocal failed_sources, requested_sources, timed_out_sources
            if not batch:
                return
            requested_sources += len(batch)
            attempted_hosts.update(host for result in batch if (host := _host_of(result.url)))
            tasks = [asyncio.create_task(self.fetch(result.url, max_length=20_000)) for result in batch]
            done: set[asyncio.Task[FetchResult]] = set()
            pending: set[asyncio.Task[FetchResult]] = set(tasks)
            try:
                done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
            finally:
                unfinished = [task for task in tasks if not task.done()]
                for task in unfinished:
                    task.cancel()
                if unfinished:
                    await asyncio.gather(*unfinished, return_exceptions=True)
            timed_out_sources += len(pending)
            for search_result, task in zip(batch, tasks, strict=False):
                if task not in done:
                    continue
                try:
                    fetch_result = task.result()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed_sources += 1
                    host = _host_of(search_result.url)
                    if host:
                        failed_hosts.add(host)
                    continue
                item = fetch_result.to_dict(preview_chars=20_000, query=query)
                item["search_title"] = search_result.title
                item["snippet"] = search_result.snippet
                item["source"] = search_result.source
                if source_class and not web_source_matches_class(str(item.get("url") or ""), source_class):
                    failed_sources += 1
                    host = _host_of(search_result.url)
                    if host:
                        failed_hosts.add(host)
                    continue
                record_item(item, attempted=True)

        remaining_total = max(0.0, _RESEARCH_TOTAL_BUDGET - (loop.time() - started_at))
        await fetch_batch(selected, timeout=min(_RESEARCH_FETCH_BUDGET, remaining_total))

        # Refill every missing COMPLETE slot, one deficit-sized wave at a time.
        # Failed spares do not hide later candidates, while the search-stage 2x
        # ceiling and the original total/fetch deadlines remain authoritative.
        unused = list(spare)
        while len(complete) < target_sources and unused:
            remaining_total = max(0.0, _RESEARCH_TOTAL_BUDGET - (loop.time() - started_at))
            if remaining_total <= 0:
                break
            elsewhere = [item for item in unused if _host_of(item.url) not in attempted_hosts]
            same_place = [item for item in unused if _host_of(item.url) in attempted_hosts]
            deficit = target_sources - len(complete)
            extra = (elsewhere + same_place)[:deficit]
            chosen = {id(item) for item in extra}
            unused = [item for item in unused if id(item) not in chosen]
            await fetch_batch(extra, timeout=min(_RESEARCH_FETCH_BUDGET, remaining_total))

        missing = max(0, target_sources - len(complete))
        retained_partial = partial[:missing]
        for _item, attempted in partial[missing:]:
            if attempted:
                failed_sources += 1
        sources = [*complete, *(item for item, _attempted in retained_partial)]
        if sources and len(complete) < target_sources:
            summary = (
                f"Collected {len(complete)} complete public sources; "
                f"the target of {target_sources} could not be filled."
            )
        elif sources:
            summary = f"Collected {len(complete)} complete public sources."
        elif failed_hosts:
            where = ", ".join(sorted(failed_hosts)[:3])
            summary = f"No page could be read completely: {where} did not provide usable public text."
        else:
            summary = "Search results were found, but no source could be fetched safely."
        if timed_out_sources:
            summary += (
                f" {timed_out_sources} source fetch"
                f"{'es' if timed_out_sources != 1 else ''} did not finish before the research deadline."
            )

        return attested_report(
            {
                "query": query,
                "sources": sources,
                "target_sources": target_sources,
                "requested_sources": requested_sources,
                "completed_sources": len(sources),
                "timed_out_sources": timed_out_sources,
                "failed_sources": failed_sources,
                "search_timed_out": False,
                "summary": summary,
            }
        )

    @staticmethod
    def _extract_pdf_text(
        content: bytes, *, max_text_chars: int, parse_budget_sec: float | None = None
    ) -> tuple[str, str, str]:
        """In-memory PDF → text via the existing DocumentExtractor.

        Returns ``(text, title, error)``. Scan-only and encrypted PDFs fail with a
        clear error rather than a silent empty body — same contract as office
        extractors used by the ingestion path.

        `parse_budget_sec` едет ВНУТРЬ разбора, а не остаётся снаружи: таймаут
        вокруг `to_thread` освобождает вызывающего, но не поток. Один PDF с
        патологической страницей занимал поток общего пула до конца процесса, и
        каждая повторная попытка модели добавляла ещё один.
        """
        from friday.documents import DocumentExtractor

        result = DocumentExtractor(
            max_text_chars=max(1_000, int(max_text_chars)),
            parse_budget_sec=parse_budget_sec,
        ).extract(
            content,
            "document.pdf",
            "application/pdf",
        )
        if result.error and not (result.text or "").strip():
            return "", "", result.error
        text = (result.text or "").strip()
        if not text:
            # Срок разбора мог оборваться ДО первой прочитанной страницы. Пустой
            # текст без объяснения читался бы как «в этом PDF нет текста» — то
            # есть свойство документа вместо нашего ограничения.
            if result.metadata.get("parse_deadline_reached"):
                return "", "", "parse_timeout"
            return "", "", "No extractable text (empty or scanned PDF)"
        if result.metadata.get("parse_deadline_reached") or result.metadata.get("extraction_truncated"):
            # Частичный текст — это ЧАСТИЧНЫЙ текст, и вызывающий обязан узнать
            # об этом. До проведения пометки через границу срок разбора превращал
            # честный отказ (`parse_timeout`) в тихую обрезку: `/api/ingest/url`
            # принимал первые страницы как весь документ.
            #
            # Причин обрыва три, и все три теряются на этой границе одинаково: срок
            # разбора, потолок знаков (на боевом маршруте `max_length=50 000` — то
            # есть ЛЮБОЙ PDF длиннее полусотни тысяч знаков, случай куда более
            # частый, чем срок) и потолок в 250 страниц. Чинить только редкий из
            # трёх — значит оставить частый.
            return text, "", "parse_truncated"
        return text, "", ""

    @staticmethod
    def _extract_text_from_html(html: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for tag in soup.find_all(_DROP_TAGS):
            tag.decompose()
        # Collect first, remove second. `decompose()` empties the tag AND every
        # descendant, so removing while iterating a list taken beforehand meant
        # the next descendant in that list had `attrs is None` and `tag.get(...)`
        # raised AttributeError. Any element matching the drop list with a child
        # inside it triggered that — which is every real page with a `<div
        # class="sidebar">`. The exception surfaced as "the page could not be
        # read", so a whole class of ordinary sites was simply unfetchable.
        doomed = []
        for tag in soup.find_all(True):
            class_value = tag.get("class")
            if isinstance(class_value, list):
                classes = " ".join(str(value) for value in class_value)
            else:
                classes = str(class_value or "")
            identifier = str(tag.get("id") or "")
            if _DROP_CLASS_RE.search(f"{classes} {identifier}"):
                doomed.append(tag)
        for tag in doomed:
            # A nested match may already have gone with its parent.
            if tag.parent is not None or tag is soup:
                tag.decompose()
        root = soup.find("main") or soup.find("article") or soup.body or soup
        lines = [" ".join(line.split()) for line in root.get_text("\n").splitlines()]
        text = "\n".join(line for line in lines if line)
        return text, title
