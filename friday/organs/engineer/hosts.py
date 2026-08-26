"""Bounded single-target host assessment for engineer mode."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from friday.host_control.contracts import ContractError
from friday.host_control.policy import NetworkPolicy, NetworkTargetSnapshot, normalize_network_targets

from . import local_binaries
from .redaction import redact_header, redact_text
from .targets import (
    PinnedTarget,
    extract_single_target,
    is_forbidden_address,
    normalize_ip_address,
    parse_host_token,
    requests_active_assessment,
    target_source_sha256,
)

DEFAULT_PORTS = (
    21,
    22,
    23,
    25,
    53,
    80,
    81,
    88,
    110,
    111,
    135,
    139,
    143,
    389,
    443,
    445,
    465,
    502,
    587,
    623,
    636,
    873,
    993,
    995,
    1080,
    1433,
    1521,
    1723,
    1883,
    2049,
    2082,
    2083,
    2222,
    2375,
    2376,
    3000,
    3128,
    3306,
    3389,
    4443,
    5000,
    5432,
    5555,
    5601,
    5672,
    5900,
    5985,
    5986,
    6379,
    6443,
    7001,
    8000,
    8008,
    8080,
    8081,
    8443,
    8888,
    9000,
    9090,
    9200,
    9443,
    10000,
    11211,
    27017,
)
MAX_PORTS = 64
MAX_TARGET_ADDRESSES = 16
CONNECT_TIMEOUT_SEC = 1.2
DNS_RESOLVE_TIMEOUT_SEC = 5.0
MAX_AUDIT_SECONDS = 115.0
BANNER_BYTES = 384
SCAN_WORKERS = 24
TLS_PORTS = frozenset({443, 465, 636, 993, 995, 2083, 2376, 4443, 5986, 6443, 8443, 9443})
HTTP_PORTS = frozenset(
    {80, 81, 443, 2082, 2083, 3000, 5000, 8000, 8008, 8080, 8081, 8443, 8888, 9000, 9090, 9443}
)
HUNT_PATHS = (
    "/",
    "/login",
    "/admin",
    "/administrator",
    "/api",
    "/api/v1",
    "/graphql",
    "/robots.txt",
    "/sitemap.xml",
    "/.git/HEAD",
    "/.env",
    "/.svn/entries",
    "/server-status",
    "/phpinfo.php",
    "/actuator",
    "/actuator/health",
    "/actuator/env",
    "/console",
    "/manager/html",
    "/wp-login.php",
    "/wp-admin/",
    "/debug",
    "/status",
    "/health",
    "/metrics",
    "/swagger",
    "/openapi.json",
    "/backup.zip",
    "/dump.sql",
    "/webdav",
    "/rdweb",
    "/owa",
)
MAX_HTTP_HITS = 32


class EngineerTargetPolicyError(ValueError):
    """Stable fail-closed reason for an exact Engineer network target."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _remaining(deadline: float | None, ceiling: float) -> float:
    return local_binaries.remaining_timeout(deadline, ceiling)


def _address_sort_key(value: str) -> tuple[int, int]:
    parsed = ipaddress.ip_address(value)
    return parsed.version, int(parsed)


def _bounded_getaddrinfo(host: str, *, deadline: float | None) -> list[Any]:
    """Bound the resolver wait; a stuck libc resolver cannot age the turn."""

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="friday-engineer-dns")
    future = pool.submit(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    try:
        done, _pending = wait(
            {future},
            timeout=_remaining(deadline, DNS_RESOLVE_TIMEOUT_SEC),
        )
        if future not in done:
            future.cancel()
            raise TimeoutError("engineer DNS resolution timed out")
        return list(future.result())
    except OSError as exc:
        raise ValueError(f"host {host!r} did not resolve") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def authorize_target(
    host: str,
    *,
    source_token: str = "",
    source_sha256: str = "",
    deadline: float | None = None,
) -> PinnedTarget:
    """Resolve once, authorize every answer, and pin deterministic connect IPs."""

    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("engineer deadline expired before DNS resolution")
    token, implied_port = parse_host_token(host)
    try:
        literal = normalize_ip_address(ipaddress.ip_address(token))
        answers = [literal]
    except ValueError:
        infos = _bounded_getaddrinfo(token, deadline=deadline)
        answers = []
        for info in infos:
            try:
                answers.append(normalize_ip_address(ipaddress.ip_address(info[4][0])))
            except ValueError:
                continue
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("engineer deadline expired during DNS resolution")
    if not answers:
        raise ValueError(f"host {token!r} did not resolve to an IP")
    unique: set[str] = set()
    for address in answers:
        if is_forbidden_address(address):
            raise ValueError(f"address {address} is not a usable engineer target")
        unique.add(str(address))
    ordered = tuple(sorted(unique, key=_address_sort_key)[:MAX_TARGET_ADDRESSES])
    if not ordered:
        raise ValueError(f"host {token!r} did not resolve to an authorized IP")
    return PinnedTarget(
        host=token,
        addresses=ordered,
        implied_port=implied_port,
        source_token=source_token[:253],
        source_sha256=source_sha256 or hashlib.sha256(source_token.encode("utf-8")).hexdigest(),
    )


def _configured_network_policy(
    allowed_cidrs: Sequence[str],
    *,
    allow_public: bool,
) -> NetworkPolicy:
    canonical: list[str] = []
    for raw in allowed_cidrs:
        try:
            network = ipaddress.ip_network(str(raw), strict=True)
        except ValueError as exc:
            raise EngineerTargetPolicyError("target_policy_invalid") from exc
        if str(network) != str(raw) or network.prefixlen == 0:
            raise EngineerTargetPolicyError("target_policy_invalid")
        canonical.append(str(network))
    try:
        return NetworkPolicy(
            # The current release does not synthesize trust from RFC1918 alone.
            # A private destination must be covered by an exact operator CIDR;
            # loopback remains the policy module's built-in exception.
            connected_cidrs=(),
            allowed_cidrs=tuple(canonical),
            allow_public=bool(allow_public),
            max_targets=MAX_TARGET_ADDRESSES,
            max_target_tokens=MAX_TARGET_ADDRESSES,
        )
    except ContractError as exc:
        raise EngineerTargetPolicyError("target_policy_invalid") from exc


def admit_pinned_target_policy(
    target: PinnedTarget,
    *,
    allowed_cidrs: Sequence[str],
    allow_public: bool,
    public_action_approved: bool = False,
) -> NetworkTargetSnapshot:
    """Revalidate pinned addresses against the shared exact network policy.

    Public scope requires both the operator feature flag and a separate
    per-action approval.  Engineer v1 has no approval carrier, so its runtime
    always calls this with ``public_action_approved=False`` and refuses before
    any probe.  The explicit argument keeps the shared boundary testable for a
    future reviewed HITL integration without treating prose as approval.
    """

    addresses: list[str] = []
    any_public = False
    for raw in target.addresses:
        try:
            address = normalize_ip_address(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise EngineerTargetPolicyError("target_policy_invalid") from exc
        addresses.append(str(address))
        any_public = bool(any_public or address.is_global)
    if any_public and not allow_public:
        raise EngineerTargetPolicyError("public_target_requires_operator_flag")
    policy = _configured_network_policy(allowed_cidrs, allow_public=allow_public)
    try:
        snapshot = normalize_network_targets(addresses, policy)
    except ContractError as exc:
        raise EngineerTargetPolicyError("target_outside_operator_policy") from exc
    if snapshot.approval_required and public_action_approved is not True:
        raise EngineerTargetPolicyError("public_target_requires_per_action_approval")
    return snapshot


def pin_target_from_speech(
    speech: str,
    *,
    deadline: float | None = None,
    allowed_cidrs: Sequence[str] | None = None,
    allow_public: bool = False,
    public_action_approved: bool = False,
) -> PinnedTarget | None:
    """Mint authority from one current-speech token and an exact policy.

    ``allowed_cidrs=None`` retains the low-level compatibility entry used by
    isolated parser tests.  Production runtime always supplies the configured
    tuple, including an empty tuple, so an unconfigured private/public address
    cannot be admitted accidentally.
    """

    selected = extract_single_target(speech)
    if selected is None:
        return None
    token = str(selected.get("token") or "")
    target = authorize_target(
        token,
        source_token=token,
        source_sha256=target_source_sha256(speech, token),
        deadline=deadline,
    )
    if allowed_cidrs is not None:
        admit_pinned_target_policy(
            target,
            allowed_cidrs=allowed_cidrs,
            allow_public=allow_public,
            public_action_approved=public_action_approved,
        )
    return target


def active_assessment_requested(speech: str) -> bool:
    """Expose the current-turn effect-intent gate beside target pinning."""

    return requests_active_assessment(speech)


def resolve_target(host: str) -> dict[str, Any]:
    """Compatibility projection for diagnostics and tests; not model authority."""

    return authorize_target(host, source_token=str(host or "")).public_dict()


def resolve_and_authorize(host: str, extras: Sequence[str] | None = None) -> dict[str, Any]:
    del extras
    return resolve_target(host)


def _banner(sock: socket.socket, *, deadline: float | None) -> str:
    sock.settimeout(_remaining(deadline, CONNECT_TIMEOUT_SEC))
    try:
        chunk = sock.recv(BANNER_BYTES)
    except OSError:
        return ""
    return redact_text(chunk.decode("utf-8", errors="replace").replace("\x00", ""), limit=BANNER_BYTES)


def _host_header(host: str) -> str:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return host
    return f"[{host}]" if parsed.version == 6 else host


def _probe_port(target: PinnedTarget, port: int, deadline: float | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"port": port, "state": "closed", "probes": ["tcp_connect"]}
    connect_host = target.connect_address
    try:
        with socket.create_connection(
            (connect_host, port),
            timeout=_remaining(deadline, CONNECT_TIMEOUT_SEC),
        ) as sock:
            entry["state"] = "open"
            banner = _banner(sock, deadline=deadline)
            if banner:
                entry["banner"] = banner
                entry["probes"].append("banner_read")
    except (OSError, TimeoutError):
        return entry
    if port in TLS_PORTS:
        entry["tls"] = _tls_summary(target, port, deadline=deadline)
        entry["probes"].append("tls_handshake")
    if port in HTTP_PORTS:
        entry["http"] = _http_exchange(target, port, "/", use_tls=_use_tls(port, entry), deadline=deadline)
        entry["probes"].append("http_head")
    if port == 6379:
        entry["redis"] = _line_probe(target, port, b"PING\r\n", deadline=deadline)
        entry["probes"].append("redis_ping")
    if port == 11211:
        entry["memcached"] = _line_probe(target, port, b"stats\r\n", deadline=deadline)
        entry["probes"].append("memcached_stats")
    return entry


def _use_tls(port: int, entry: Mapping[str, Any]) -> bool:
    tls = entry.get("tls") if isinstance(entry.get("tls"), Mapping) else {}
    return port in TLS_PORTS or bool(tls and not tls.get("error"))


def _tls_summary(target: PinnedTarget, port: int, *, deadline: float | None) -> dict[str, Any]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with (
            socket.create_connection(
                (target.connect_address, port),
                timeout=_remaining(deadline, CONNECT_TIMEOUT_SEC),
            ) as raw,
            context.wrap_socket(raw, server_hostname=target.host) as wrapped,
        ):
            wrapped.settimeout(_remaining(deadline, CONNECT_TIMEOUT_SEC))
            certificate = wrapped.getpeercert(binary_form=True) or b""
            return {
                "version": str(wrapped.version() or "")[:32],
                "cipher": str((wrapped.cipher() or ("", "", 0))[0])[:80],
                "certificate_sha256": hashlib.sha256(certificate).hexdigest() if certificate else "",
                "sni": target.host,
                "verified": False,
            }
    except (OSError, TimeoutError):
        return {"error": "tls_handshake_failed", "sni": target.host, "verified": False}


def _http_exchange(
    target: PinnedTarget,
    port: int,
    path: str,
    *,
    use_tls: bool,
    deadline: float | None,
    method: str = "HEAD",
) -> dict[str, Any]:
    safe_path = path if path in HUNT_PATHS else "/"
    request = (
        f"{method} {safe_path} HTTP/1.0\r\nHost: {_host_header(target.host)}\r\n"
        "User-Agent: Friday-engineer-hunt\r\nConnection: close\r\n\r\n"
    ).encode("ascii", errors="replace")
    raw: socket.socket | None = None
    try:
        raw = socket.create_connection(
            (target.connect_address, port),
            timeout=_remaining(deadline, CONNECT_TIMEOUT_SEC),
        )
        sock: socket.socket = raw
        if use_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(raw, server_hostname=target.host)
        with sock:
            sock.settimeout(_remaining(deadline, CONNECT_TIMEOUT_SEC))
            sock.sendall(request)
            body = sock.recv(1024)
    except (OSError, TimeoutError) as exc:
        if raw is not None:
            raw.close()
        return {"path": safe_path, "method": method, "error": type(exc).__name__}
    text = body.decode("iso-8859-1", errors="replace")
    status = redact_text(text.split("\r\n", 1)[0], limit=160)
    headers: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if not line:
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().casefold()
        if normalized in {
            "server",
            "x-powered-by",
            "www-authenticate",
            "location",
            "set-cookie",
            "access-control-allow-origin",
            "content-type",
            "x-frame-options",
            "content-security-policy",
            "strict-transport-security",
        }:
            headers[normalized] = redact_header(normalized, value.strip(), limit=180)
    return {"path": safe_path, "method": method, "status": status, "headers": headers}


def _line_probe(
    target: PinnedTarget,
    port: int,
    payload: bytes,
    *,
    deadline: float | None,
) -> dict[str, Any]:
    try:
        with socket.create_connection(
            (target.connect_address, port),
            timeout=_remaining(deadline, CONNECT_TIMEOUT_SEC),
        ) as sock:
            sock.settimeout(_remaining(deadline, CONNECT_TIMEOUT_SEC))
            sock.sendall(payload)
            reply = sock.recv(256)
    except (OSError, TimeoutError) as exc:
        return {"error": type(exc).__name__}
    return {"reply": redact_text(reply.decode("utf-8", errors="replace").replace("\x00", ""), limit=200)}


def _scan_ports(
    target: PinnedTarget, ports: Sequence[int], *, deadline: float | None
) -> list[dict[str, Any]]:
    ordered = list(ports)
    workers = max(1, min(SCAN_WORKERS, len(ordered)))
    timeout = _remaining(
        deadline,
        CONNECT_TIMEOUT_SEC * (1 + len(ordered) / workers) + 4.0,
    )
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="friday-engineer-scan")
    futures: dict[Future[dict[str, Any]], int] = {
        pool.submit(_probe_port, target, port, deadline): port for port in ordered
    }
    try:
        done, pending = wait(futures, timeout=timeout)
        results: list[dict[str, Any]] = []
        for future in done:
            if future.cancelled():
                continue
            port = futures[future]
            try:
                results.append(future.result())
            except Exception:  # noqa: BLE001 - a failed worker becomes bounded evidence
                results.append({"port": port, "state": "probe_error", "probes": ["tcp_connect"]})
        for future in pending:
            future.cancel()
        seen_ports = {int(item.get("port") or 0) for item in results}
        results.extend(
            {"port": port, "state": "timeout", "probes": ["tcp_connect"]}
            for port in ordered
            if port not in seen_ports
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return sorted(results, key=lambda item: int(item.get("port") or 0))


def http_hunt(
    target: PinnedTarget,
    port: int,
    use_tls: bool,
    *,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    timeout = _remaining(
        deadline,
        CONNECT_TIMEOUT_SEC * (1 + len(HUNT_PATHS) / 8) + 2.0,
    )
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="friday-engineer-http")
    futures: dict[Future[dict[str, Any]], int] = {
        pool.submit(_http_exchange, target, port, path, use_tls=use_tls, deadline=deadline): index
        for index, path in enumerate(HUNT_PATHS)
    }
    try:
        done, pending = wait(futures, timeout=timeout)
        for future in pending:
            future.cancel()
        hits = []
        for future in done:
            try:
                item = future.result()
            except Exception:  # noqa: BLE001 - do not fail the whole bounded path set
                continue
            status = str(item.get("status") or "")
            if not item.get("error") and any(
                code in status for code in (" 200", " 301", " 302", " 401", " 403", " 500")
            ):
                hits.append((futures[future], item))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [item for _index, item in sorted(hits, key=lambda pair: pair[0])[:MAX_HTTP_HITS]]


def _weaknesses(results: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for entry in results:
        if entry.get("state") != "open":
            continue
        port = int(entry.get("port") or 0)
        if port in {21, 23}:
            found.append({"code": "cleartext_admin", "detail": f"port {port}"})
        if port == 445:
            found.append({"code": "smb_open", "detail": "445/tcp"})
        if port == 3389:
            found.append({"code": "rdp_open", "detail": "3389/tcp"})
        if port == 5900:
            found.append({"code": "vnc_open", "detail": "5900/tcp"})
        if port in {2375, 2376}:
            found.append({"code": "docker_api", "detail": f"{port}/tcp"})
        if port in {3306, 5432, 1433, 27017, 6379, 9200, 11211, 5672}:
            found.append({"code": "data_store_exposed", "detail": f"port {port}"})
        redis = entry.get("redis") if isinstance(entry.get("redis"), Mapping) else None
        if redis and "PONG" in str(redis.get("reply") or ""):
            found.append({"code": "redis_unauth_ping", "detail": "6379 answered PING"})
        http = entry.get("http") if isinstance(entry.get("http"), Mapping) else None
        if http and port in {80, 8080, 8000} and " 200" in str(http.get("status") or ""):
            found.append({"code": "http_without_tls", "detail": f"port {port}"})
        headers = (http or {}).get("headers") if isinstance((http or {}).get("headers"), Mapping) else {}
        if headers and "strict-transport-security" not in headers and port in TLS_PORTS:
            found.append({"code": "missing_hsts", "detail": f"port {port}"})
        if headers and "server" in headers:
            banner_hash = hashlib.sha256(str(headers["server"]).encode("utf-8")).hexdigest()[:16]
            found.append({"code": "server_banner", "detail": f"sha256:{banner_hash}"})
        tls = entry.get("tls") if isinstance(entry.get("tls"), Mapping) else None
        if tls and tls.get("error"):
            found.append({"code": "tls_handshake_failed", "detail": f"port {port}"})
        for hit in list(entry.get("http_paths") or []):
            if not isinstance(hit, Mapping):
                continue
            status = str(hit.get("status") or "")
            path = str(hit.get("path") or "")
            if path in {"/.env", "/.git/HEAD", "/phpinfo.php", "/actuator/env", "/dump.sql"} and any(
                code in status for code in (" 200", " 301", " 302")
            ):
                found.append({"code": "sensitive_path", "detail": f"{path} {status[:40]}"})
            if path in {"/login", "/admin", "/wp-login.php", "/manager/html"} and " 200" in status:
                found.append({"code": "auth_surface", "detail": path})
    deduped = {(item["code"], item["detail"]): item for item in found}
    return [deduped[key] for key in sorted(deduped)]


def _clean_ports(ports: Sequence[int] | None, implied_port: int | None) -> list[int]:
    selected = list(ports) if ports is not None else list(DEFAULT_PORTS)
    if implied_port is not None and implied_port not in selected:
        selected.insert(0, implied_port)
    if not selected:
        selected = list(DEFAULT_PORTS)
    if len(selected) > MAX_PORTS:
        raise ValueError(f"at most {MAX_PORTS} ports")
    cleaned: list[int] = []
    for value in selected:
        if isinstance(value, bool):
            raise ValueError("port is not an integer")
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError(f"port {port} is not in 1..65535")
        if port not in cleaned:
            cleaned.append(port)
    return cleaned


def audit_target(
    target: PinnedTarget,
    ports: Sequence[int] | None = None,
    *,
    rehearsal: bool = True,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Assess one already-authorized target without performing target expansion."""

    effective_deadline = deadline if deadline is not None else time.monotonic() + MAX_AUDIT_SECONDS
    selected = _clean_ports(ports, target.implied_port)
    results = _scan_ports(target, selected, deadline=effective_deadline)
    deadline_exhausted = False
    if rehearsal:
        for item in results:
            if item.get("state") != "open" or int(item.get("port") or 0) not in HTTP_PORTS:
                continue
            if time.monotonic() >= effective_deadline:
                deadline_exhausted = True
                break
            port = int(item["port"])
            try:
                item["http_paths"] = http_hunt(
                    target,
                    port,
                    use_tls=_use_tls(port, item),
                    deadline=effective_deadline,
                )
            except TimeoutError:
                item["http_paths"] = []
                deadline_exhausted = True
                break
            item.setdefault("probes", []).append("http_path_head")
    open_ports = [int(item["port"]) for item in results if item.get("state") == "open"]
    try:
        nmap = local_binaries.nmap_connect_scan(
            target.connect_address,
            open_ports or selected[:32],
            deadline=effective_deadline,
        )
    except TimeoutError:
        nmap = {"ok": False, "error": "deadline"}
    try:
        dns = local_binaries.dig_records(target.host, deadline=effective_deadline)
    except TimeoutError:
        dns = {"ok": False, "error": "deadline"}
    probe_names = {
        str(probe) for item in results for probe in list(item.get("probes") or []) if isinstance(probe, str)
    }
    if dns.get("attempted") or dns.get("error") not in {"resolver_missing", "deadline"}:
        probe_names.add("dns_lookup")
    if nmap.get("error") not in {"nmap_missing", "no_ports", "deadline"}:
        probe_names.add("nmap_service_detection")
    return {
        "ok": True,
        "host": target.host,
        "addresses": list(target.addresses),
        "probed_address": target.connect_address,
        "target_source_sha256": target.source_sha256,
        "open_ports": open_ports,
        "ports": results,
        "weaknesses": _weaknesses(results),
        "dns": dns,
        "nmap": (
            nmap if nmap.get("ok") or nmap.get("report") else {"used": False, "reason": nmap.get("error")}
        ),
        "local_tools": {name: bool(path) for name, path in sorted(local_binaries.inventory().items())},
        "rehearsal": bool(rehearsal),
        "deadline_exhausted": deadline_exhausted,
        "active_probes_sent": bool(probe_names),
        "active_probes": sorted(probe_names),
        "exploit_payloads_sent": False,
        "payloads_sent": False,
    }


def audit_host(
    host: str,
    ports: Sequence[int] | None = None,
    *,
    extras: Sequence[str] | None = None,
    rehearsal: bool = True,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Compatibility entry for trusted code; model tools must use a pinned target."""

    del extras
    effective_deadline = deadline if deadline is not None else time.monotonic() + MAX_AUDIT_SECONDS
    target = authorize_target(host, source_token=str(host or ""), deadline=effective_deadline)
    return audit_target(target, ports, rehearsal=rehearsal, deadline=effective_deadline)


def rehearsal_playbook(audit: Mapping[str, Any], artifact: Mapping[str, Any] | None = None) -> dict[str, Any]:
    steps: list[dict[str, str]] = []
    host = str(audit.get("host") or "")
    for weakness in list(audit.get("weaknesses") or []):
        code = str(weakness.get("code") or "")
        detail = redact_text(weakness.get("detail"), limit=120)
        intent = {
            "smb_open": "enumerate SMB signing and shares",
            "rdp_open": "check whether NLA is required",
            "http_without_tls": "review the HTTP paths already fetched",
            "data_store_exposed": "confirm whether the listener greets unauthenticated",
            "redis_unauth_ping": "treat the PING response as unauthenticated exposure",
            "docker_api": "treat the Docker API listener as host-equivalent exposure",
            "sensitive_path": "review the sensitive HTTP path response",
            "auth_surface": "review the reachable login surface",
            "cleartext_admin": "review the cleartext administrative protocol",
        }.get(code, "confirm service identity from bounded evidence")
        steps.append(
            {
                "id": code or "generic",
                "intent": intent,
                "detail": detail,
                "detection": "traffic from the Friday host",
            }
        )
    if artifact and artifact.get("finding_codes"):
        steps.append(
            {
                "id": "artifact-compare",
                "intent": "match static findings to the live binary",
                "detail": ",".join(str(item) for item in list(artifact.get("finding_codes") or [])[:16]),
                "detection": "file-integrity or EDR on the sample hash",
            }
        )
    if not steps:
        steps.append(
            {
                "id": "manual-review",
                "intent": "review current evidence before choosing another explicit target",
                "detail": host,
                "detection": "the bounded connect sweep itself",
            }
        )
    return {
        "ok": True,
        "host": host,
        "active_probes_sent": bool(audit.get("active_probes_sent")),
        "active_probes": list(audit.get("active_probes") or []),
        "exploit_payloads_sent": False,
        "payloads_sent": False,
        "steps": steps[:24],
        "note": "Defensive rehearsal only; Friday does not send exploit payloads.",
    }


def public_host_payload(audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "host": audit.get("host"),
        "addresses": list(audit.get("addresses") or [])[:8],
        "open_ports": list(audit.get("open_ports") or [])[:MAX_PORTS],
        "weaknesses": sorted(
            str(item.get("code"))
            for item in list(audit.get("weaknesses") or [])
            if isinstance(item, Mapping) and item.get("code")
        )[:64],
        "active_probes": list(audit.get("active_probes") or [])[:16],
        "exploit_payloads_sent": False,
    }


def host_markdown(report: Mapping[str, Any]) -> str:
    """Render only code-owned facts; remote free text stays out of system prompts."""

    lines = [
        f"# Target `{redact_text(report.get('host'), limit=253)}`",
        "addresses: " + ", ".join(str(item) for item in list(report.get("addresses") or [])[:8]),
        "open: "
        + ", ".join(str(item) for item in list(report.get("open_ports") or [])[:MAX_PORTS] or ["none"]),
        "active probes: " + ", ".join(str(item) for item in list(report.get("active_probes") or [])[:16]),
        "exploit payloads sent: no",
    ]
    for item in list(report.get("weaknesses") or [])[:64]:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            f"- `{redact_text(item.get('code'), limit=64)}` {redact_text(item.get('detail'), limit=120)}"
        )
    dns_value = report.get("dns")
    dns: Mapping[str, Any] = dns_value if isinstance(dns_value, Mapping) else {}
    records = list(dns.get("records") or [])
    if records:
        digest = hashlib.sha256("\n".join(str(item) for item in records).encode("utf-8")).hexdigest()
        lines.append(f"dns evidence: {len(records)} record groups, sha256 `{digest}`")
    nmap_value = report.get("nmap")
    nmap: Mapping[str, Any] = nmap_value if isinstance(nmap_value, Mapping) else {}
    projection_value = nmap.get("report")
    projection: Mapping[str, Any] = projection_value if isinstance(projection_value, Mapping) else {}
    structured_value = projection.get("result")
    structured: Mapping[str, Any] = structured_value if isinstance(structured_value, Mapping) else {}
    if projection.get("label") == "UNTRUSTED_HOST_APPLICATION_EVIDENCE":
        requested = structured.get("targets_requested")
        scanned = structured.get("targets_scanned")
        open_ports = structured.get("open_ports")
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (requested, scanned, open_ports)
        ):
            lines.append(
                "nmap structured result: "
                f"targets {scanned}/{requested}, open ports {open_ports}, "
                f"parser `{redact_text(projection.get('parser_status'), limit=24)}`, "
                "label `UNTRUSTED_HOST_APPLICATION_EVIDENCE`"
            )
    evidence = list(nmap.get("evidence") or [])
    first_evidence = evidence[0] if evidence and isinstance(evidence[0], Mapping) else {}
    evidence_digest = str(first_evidence.get("sha256") or "")
    evidence_size = first_evidence.get("size_bytes")
    if len(evidence_digest) == 64 and isinstance(evidence_size, int):
        coverage_value = nmap.get("coverage")
        coverage: Mapping[str, Any] = coverage_value if isinstance(coverage_value, Mapping) else {}
        lines.append(
            "nmap XML evidence: "
            f"{evidence_size} bytes, sha256 `{evidence_digest}`, "
            f"coverage `{redact_text(coverage.get('grade'), limit=24)}`"
        )
    return "\n".join(lines)[:12_000]


__all__ = [
    "EngineerTargetPolicyError",
    "DEFAULT_PORTS",
    "HTTP_PORTS",
    "MAX_PORTS",
    "MAX_AUDIT_SECONDS",
    "MAX_TARGET_ADDRESSES",
    "TLS_PORTS",
    "audit_host",
    "audit_target",
    "active_assessment_requested",
    "admit_pinned_target_policy",
    "authorize_target",
    "host_markdown",
    "http_hunt",
    "pin_target_from_speech",
    "public_host_payload",
    "rehearsal_playbook",
    "resolve_and_authorize",
    "resolve_target",
]
