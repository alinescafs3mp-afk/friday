"""Closed HTTP-boundary checks for the synthetic live-battery harness."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_base_url="http://127.0.0.1:18001/v1",
        embeddings_base_url="http://127.0.0.1:18002/v1",
        rerank_base_url="http://127.0.0.1:18003/v1",
        llm_api_key="LEGITIMATE-MODEL-SERVICE-KEY",
        embeddings_api_key="LEGITIMATE-EMBEDDING-SERVICE-KEY",
        rerank_api_key="LEGITIMATE-RERANK-SERVICE-KEY",
    )


@pytest.mark.asyncio
async def test_http_probe_counts_real_sends_without_treating_service_keys_as_foreign(
    monkeypatch,
) -> None:  # noqa: ANN001
    import httpx

    forwarded: list[str] = []

    async def fake_send(client, request, *args, **kwargs):  # noqa: ANN001, ANN202
        del client, args, kwargs
        forwarded.append(str(request.url))
        return httpx.Response(204, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    foreign = "SYN-FOREIGN-HTTP-BOUNDARY-CANARY"
    settings = _settings()
    probe = battery.LocalEndpointHttpProbe(settings, [foreign])
    probe.install()
    try:
        async with httpx.AsyncClient() as client:
            requests = (
                httpx.Request(
                    "POST",
                    f"{settings.llm_base_url}/chat/completions",
                    headers={"authorization": f"Bearer {settings.llm_api_key}"},
                    json={"messages": [{"content": "clean"}]},
                ),
                httpx.Request(
                    "POST",
                    f"{settings.embeddings_base_url}/embeddings",
                    headers={"authorization": f"Bearer {settings.embeddings_api_key}"},
                    json={"input": ["clean"]},
                ),
                httpx.Request(
                    "POST",
                    f"{settings.rerank_base_url}/rerank",
                    headers={"authorization": f"Bearer {settings.rerank_api_key}"},
                    json={"query": "clean", "documents": ["clean"]},
                ),
                httpx.Request("GET", "http://127.0.0.1:18001/v1/models"),
            )
            for request in requests:
                assert (await client.send(request)).status_code == 204
    finally:
        probe.restore()

    assert probe.counts == {"model": 1, "embedding": 1, "reranker": 1, "other": 1}
    assert probe.foreign_canary_sends == {
        "model": 0,
        "embedding": 0,
        "reranker": 0,
        "other": 0,
    }
    assert probe.foreign_canary_surfaces == {"url": 0, "headers": 0, "body": 0}
    assert probe.scan_failures == 0
    assert len(forwarded) == 4
    assert httpx.AsyncClient.send is fake_send


@pytest.mark.asyncio
async def test_http_probe_scans_decoded_url_headers_and_body_at_the_send_boundary(
    monkeypatch,
) -> None:  # noqa: ANN001
    import httpx

    async def fake_send(client, request, *args, **kwargs):  # noqa: ANN001, ANN202
        del client, args, kwargs
        return httpx.Response(204, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    foreign = "SYN-FOREIGN-HTTP-BOUNDARY-CANARY"
    settings = _settings()
    probe = battery.LocalEndpointHttpProbe(settings, [foreign])
    probe.install()
    try:
        async with httpx.AsyncClient() as client:
            await client.send(
                httpx.Request(
                    "POST",
                    f"{settings.llm_base_url}/chat/completions?q=%53YN-FOREIGN-HTTP-BOUNDARY-CANARY",
                    json={"messages": [{"content": "clean"}]},
                )
            )
            await client.send(
                httpx.Request(
                    "POST",
                    f"{settings.embeddings_base_url}/embeddings",
                    headers={"x-synthetic-private": foreign},
                    json={"input": ["clean"]},
                )
            )
            await client.send(
                httpx.Request(
                    "POST",
                    f"{settings.rerank_base_url}/rerank",
                    content=json.dumps({"query": foreign}).encode(),
                )
            )
    finally:
        probe.restore()

    assert probe.counts == {"model": 1, "embedding": 1, "reranker": 1, "other": 0}
    assert probe.foreign_canary_sends == {
        "model": 1,
        "embedding": 1,
        "reranker": 1,
        "other": 0,
    }
    assert probe.foreign_canary_surfaces == {"url": 1, "headers": 1, "body": 1}
    assert probe.scan_failures == 0


@pytest.mark.asyncio
async def test_http_probe_scan_error_is_a_closed_failure_counter(monkeypatch) -> None:  # noqa: ANN001
    import httpx

    async def fake_send(client, request, *args, **kwargs):  # noqa: ANN001, ANN202
        del client, request, args, kwargs
        return "forwarded"

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    probe = battery.LocalEndpointHttpProbe(_settings(), ["SYN-FOREIGN-HTTP-CANARY"])
    probe.install()
    try:
        client = httpx.AsyncClient()
        try:
            assert await client.send(object()) == "forwarded"
        finally:
            await client.aclose()
    finally:
        probe.restore()

    assert probe.counts["other"] == 1
    assert probe.scan_failures == 1


def _closed_case_delta(profile: str) -> dict[str, int]:
    delta = {
        "model_http": 1,
        "embedding_http": int(profile == "tenant_privacy"),
        "reranker_http": int(profile == "tenant_privacy"),
        "other_http": 0,
    }
    delta.update(dict.fromkeys(battery._HTTP_PRIVACY_COUNTER_KEYS, 0))
    return delta


def _sum_http_deltas(deltas: list[dict[str, int]]) -> dict[str, int]:
    return {key: sum(delta[key] for delta in deltas) for key in deltas[0]}


@pytest.mark.parametrize("profile", battery.PASS_PROFILES)
def test_http_reconciliation_closes_every_case_and_pass_budget(profile: str) -> None:
    deltas = [_closed_case_delta(profile) for _index in range(battery.QUESTIONS_PER_PASS)]
    total = _sum_http_deltas(deltas)
    assert battery._http_probe_reconciliation_exact(profile, deltas, total) is True

    attempt_keys = ("model_http", "embedding_http", "reranker_http")
    for key, limit in zip(
        attempt_keys,
        battery._PROFILE_HTTP_SEND_LIMITS[profile],
        strict=True,
    ):
        overflowing = copy.deepcopy(deltas)
        overflowing[0][key] = limit + 1
        assert (
            battery._http_probe_reconciliation_exact(
                profile,
                overflowing,
                _sum_http_deltas(overflowing),
            )
            is False
        )

    forged_total = dict(total)
    forged_total["model_http"] = (
        battery._PROFILE_HTTP_SEND_LIMITS[profile][0] * battery.QUESTIONS_PER_PASS + 1
    )
    assert battery._http_probe_reconciliation_exact(profile, deltas, forged_total) is False

    for counter in battery._HTTP_PRIVACY_COUNTER_KEYS:
        exposed = copy.deepcopy(deltas)
        exposed[0][counter] = 1
        assert (
            battery._http_probe_reconciliation_exact(
                profile,
                exposed,
                _sum_http_deltas(exposed),
            )
            is False
        )


def test_case_oracle_exposes_closed_http_counts_clear_flag_and_overflow_verdicts() -> None:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["A"])
    cases = battery.expand_manifest_cases(manifest)
    by_profile = {case.oracle_profile: case for case in cases}

    for profile in battery.PASS_PROFILES:
        oracle = battery.oracle_for_case(by_profile[profile])["state"]
        equals = oracle["equals"]
        maximums = oracle["max"]
        assert equals["http_privacy_canary_clear"] is True
        assert equals["http_privacy_scan_failures"] == 0
        assert all(
            equals[key] == 0
            for key in (
                "foreign_canary_model_http_sends",
                "foreign_canary_embedding_http_sends",
                "foreign_canary_reranker_http_sends",
                "foreign_canary_other_http_sends",
                "foreign_canary_http_url_hits",
                "foreign_canary_http_header_hits",
                "foreign_canary_http_body_hits",
            )
        )
        assert all(
            equals[f"{kind}_http_attempts_overflow"] is False for kind in ("model", "embedding", "reranker")
        )
        assert (
            tuple(maximums[f"{kind}_http_attempts"] for kind in ("model", "embedding", "reranker"))
            == battery._PROFILE_HTTP_SEND_LIMITS[profile]
        )
