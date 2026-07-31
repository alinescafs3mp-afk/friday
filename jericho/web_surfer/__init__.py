"""Budgeted public-web search and fetching with SSRF protection.

Only public HTTP(S) destinations are reachable by default. DNS answers are
checked before every request and every redirect, reducing exposure to DNS
rebinding and blocking redirects into the local network. Private-network
fetching can be enabled explicitly for trusted, single-user installations.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpcore
import httpx
from bs4 import BeautifulSoup

from jericho.config import JerichoSettings
from jericho.retrieval import best_snippet

LOGGER = logging.getLogger(__name__)


def _log_safe_host(url: str) -> str:
    """Hostname-only form of a URL for log lines.

    Full URLs (and httpx exception strings, which embed them) can carry the
    user's query in path or parameters — logs get the host and nothing else.
    """
    try:
        return urllib.parse.urlsplit(str(url)).netloc or "<invalid-url>"
    except ValueError:
        return "<invalid-url>"


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
_DEFAULT_MAX_RESULTS = 5
_DEFAULT_MAX_SOURCES = 3
_MAX_REDIRECTS = 5
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
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
)


class UnsafeURLError(ValueError):
    """The requested URL can reach a non-public or otherwise unsafe target."""


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


@dataclass(frozen=True)
class FetchResult:
    url: str
    title: str
    text: str
    text_length: int
    status_code: int | None = None
    error: str = ""

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
            "truncated": len(self.text) > len(text),
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


class WebSurfer:
    """Search and fetch public sources under strict byte and time budgets."""

    def __init__(self, settings: JerichoSettings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

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

    async def search(self, query: str, *, max_results: int = _DEFAULT_MAX_RESULTS) -> list[SearchResult]:
        query = " ".join(str(query or "").split()).strip()
        if not query:
            return []
        limit = max(1, min(int(max_results), 20))
        results: list[SearchResult] = []

        providers = []
        if self.settings.brave_search_api_key:
            providers.append(self._search_brave(query, limit))
        if self.settings.tavily_api_key:
            providers.append(self._search_tavily(query, limit))
        if self.settings.serper_api_key:
            providers.append(self._search_serper(query, limit))
        if providers:
            for batch in await asyncio.gather(*providers, return_exceptions=True):
                if isinstance(batch, list):
                    results.extend(batch)

        if not results:
            results.extend(await self._search_duckduckgo_html(query, limit))

        deduped: list[SearchResult] = []
        seen: set[str] = set()
        for item in results:
            normalized = item.url.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(item)
        return deduped[:limit]

    async def _search_duckduckgo_html(self, query: str, limit: int) -> list[SearchResult]:
        try:
            client = await self._get_client()
            response = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                timeout=_SEARCH_TIMEOUT,
            )
            response.raise_for_status()
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
            return results
        except Exception as exc:
            LOGGER.warning("DuckDuckGo search failed: %s", type(exc).__name__)
            return []

    async def _search_brave(self, query: str, limit: int) -> list[SearchResult]:
        try:
            client = await self._get_client()
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.settings.brave_search_api_key,
                },
                timeout=_SEARCH_TIMEOUT,
            )
            response.raise_for_status()
            return [
                SearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    snippet=str(item.get("description", "")),
                    source="brave",
                )
                for item in response.json().get("web", {}).get("results", [])[:limit]
                if item.get("url")
            ]
        except Exception as exc:
            LOGGER.warning("Brave search failed: %s", type(exc).__name__)
            return []

    async def _search_tavily(self, query: str, limit: int) -> list[SearchResult]:
        try:
            client = await self._get_client()
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.settings.tavily_api_key,
                    "query": query,
                    "max_results": limit,
                    "search_depth": "basic",
                },
                timeout=_SEARCH_TIMEOUT,
            )
            response.raise_for_status()
            return [
                SearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    snippet=str(item.get("content", "")),
                    source="tavily",
                )
                for item in response.json().get("results", [])[:limit]
                if item.get("url")
            ]
        except Exception as exc:
            LOGGER.warning("Tavily search failed: %s", type(exc).__name__)
            return []

    async def _search_serper(self, query: str, limit: int) -> list[SearchResult]:
        try:
            client = await self._get_client()
            response = await client.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": limit},
                headers={"X-API-KEY": self.settings.serper_api_key},
                timeout=_SEARCH_TIMEOUT,
            )
            response.raise_for_status()
            return [
                SearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("link", "")),
                    snippet=str(item.get("snippet", "")),
                    source="serper",
                )
                for item in response.json().get("organic", [])[:limit]
                if item.get("link")
            ]
        except Exception as exc:
            LOGGER.warning("Serper search failed: %s", type(exc).__name__)
            return []

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

    async def fetch(self, url: str, *, max_length: int = 50_000) -> FetchResult:
        requested = str(url or "").strip()
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
                    url=final_url,
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
                    url=final_url,
                    title="",
                    text="",
                    text_length=0,
                    status_code=status,
                    error=f"Unsupported content type: {content_type or 'missing'}",
                )
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
            text = text[: max(1_000, min(int(max_length), self.settings.max_extracted_text_chars))]
            return FetchResult(
                url=final_url,
                title=title,
                text=text,
                text_length=len(text),
                status_code=status,
            )
        except (httpx.TimeoutException, TimeoutError):
            # TimeoutError explicitly: it is NOT a subclass of httpx.TimeoutException,
            # and `str(TimeoutError())` is the empty string — falling through to the
            # generic handler would return a blank error, which `POST /api/ingest/url`
            # then reports as «empty content» instead of a timeout.
            return FetchResult(requested, "", "", 0, error="Timeout")
        except UnsafeURLError as exc:
            return FetchResult(requested, "", "", 0, error=f"Blocked URL: {exc}")
        except Exception as exc:
            LOGGER.info("Web fetch failed for %s: %s", _log_safe_host(requested), type(exc).__name__)
            return FetchResult(requested, "", "", 0, error=str(exc))

    async def research(self, query: str, *, max_sources: int = _DEFAULT_MAX_SOURCES) -> dict[str, Any]:
        source_limit = max(1, min(int(max_sources), 8))
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        try:
            async with asyncio.timeout(_RESEARCH_TOTAL_BUDGET):
                results = await self.search(query, max_results=source_limit * 2)
        except TimeoutError:
            return {
                "query": query,
                "sources": [],
                "requested_sources": 0,
                "completed_sources": 0,
                "timed_out_sources": 0,
                "failed_sources": 0,
                "search_timed_out": True,
                "summary": "Public-source search timed out before pages could be fetched.",
            }
        if not results:
            return {
                "query": query,
                "sources": [],
                "requested_sources": 0,
                "completed_sources": 0,
                "timed_out_sources": 0,
                "failed_sources": 0,
                "search_timed_out": False,
                "summary": "No search results found.",
            }

        selected = results[:source_limit]
        tasks = [asyncio.create_task(self.fetch(result.url, max_length=20_000)) for result in selected]
        done: set[asyncio.Task[FetchResult]] = set()
        pending: set[asyncio.Task[FetchResult]] = set(tasks)
        remaining_total = max(0.0, _RESEARCH_TOTAL_BUDGET - (loop.time() - started_at))
        fetch_budget = min(_RESEARCH_FETCH_BUDGET, remaining_total)
        try:
            done, pending = await asyncio.wait(tasks, timeout=fetch_budget)
        finally:
            unfinished = [task for task in tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

        sources: list[dict[str, Any]] = []
        failed_sources = 0
        for search_result, task in zip(selected, tasks, strict=False):
            if task not in done:
                continue
            try:
                fetch_result = task.result()
            except BaseException:
                failed_sources += 1
                continue
            # Выдержка по ИСХОДНОМУ запросу исследования, а не по адресу страницы.
            item = fetch_result.to_dict(preview_chars=20_000, query=query)
            item["search_title"] = search_result.title
            item["snippet"] = search_result.snippet
            item["source"] = search_result.source
            sources.append(item)

        readable_sources = sum(1 for item in sources if not item["error"])
        timed_out_sources = len(pending)
        if sources:
            summary = f"Collected {readable_sources} readable public sources."
        else:
            summary = "Search results were found, but no source could be fetched safely."
        if timed_out_sources:
            summary += (
                f" {timed_out_sources} source fetch"
                f"{'es' if timed_out_sources != 1 else ''} did not finish before the research deadline."
            )

        return {
            "query": query,
            "sources": sources,
            "requested_sources": len(selected),
            "completed_sources": len(sources),
            "timed_out_sources": timed_out_sources,
            "failed_sources": failed_sources,
            "search_timed_out": False,
            "summary": summary,
        }

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
