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


def _pass_cases(profile: str, *, battery_id: str = "A") -> list[battery.ExpandedCase]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS[battery_id])
    return [case for case in battery.expand_manifest_cases(manifest) if case.oracle_profile == profile]


def _closed_case_delta(case: battery.ExpandedCase) -> dict[str, int]:
    model_owned = (
        case.oracle_profile != "tenant_privacy"
        and not battery._package_a_code_owned_case(case)
        and not battery._package_a_code_owned_temporal_case(case)
    )
    delta = {
        **dict.fromkeys(battery._P01_CODE_OWNED_DELTA_ZERO_COUNTERS, 0),
        "model_http": int(model_owned),
        "embedding_http": 0,
        "reranker_http": 0,
        "other_http": 0,
    }
    delta.update(dict.fromkeys(battery._HTTP_PRIVACY_COUNTER_KEYS, 0))
    return delta


def _route_evidence(case: battery.ExpandedCase) -> dict[str, bool | None]:
    if battery._package_a_code_owned_case(case):
        return {
            "fabricated_outside_deed_request": True,
            "answer_present": True,
            "model_spoke": False,
            "outside_deed_replaced": False,
            "remainder_known": True,
            "llm_failed": False,
        }
    if case.oracle_profile == "package_a_honesty":
        return {
            "fabricated_outside_deed_request": False,
            "answer_present": False,
            "model_spoke": True,
            "outside_deed_replaced": False,
            "remainder_known": False,
            "llm_failed": False,
        }
    return {
        "fabricated_outside_deed_request": False,
        "answer_present": True,
        "model_spoke": case.oracle_profile != "tenant_privacy",
        "outside_deed_replaced": False,
        "remainder_known": True,
        "llm_failed": False,
    }


@pytest.mark.parametrize(
    ("model_spoke", "output_guards", "expected_guard"),
    [
        (True, {}, False),
        (False, {"outside_deed_replaced": True}, True),
        (True, {"outside_deed_replaced": False}, False),
        (False, {"outside_deed_replaced": "true"}, None),
        (True, "malformed", None),
    ],
)
def test_p01_route_projection_binds_the_durable_outside_deed_guard(
    model_spoke: bool,
    output_guards: object,
    expected_guard: bool | None,
) -> None:
    projected = battery._p01_route_evidence(
        {
            "answer_present": False,
            "fabricated_outside_deed_request": False,
            "llm_failed": False,
            "model_spoke": model_spoke,
            "output_guards": output_guards,
            "remainder_known": False,
        }
    )

    assert set(projected) == set(battery._P01_ROUTE_EVIDENCE_KEYS)
    assert projected["outside_deed_replaced"] is expected_guard


def _closed_ledgers(
    cases: list[battery.ExpandedCase],
) -> tuple[list[tuple[str, dict[str, int]]], list[tuple[str, dict[str, bool | None]]]]:
    return (
        [(case.id, _closed_case_delta(case)) for case in cases],
        [(case.id, _route_evidence(case)) for case in cases],
    )


def _sum_http_deltas(deltas: list[dict[str, int]]) -> dict[str, int]:
    return {key: sum(delta[key] for delta in deltas) for key in deltas[0]}


@pytest.mark.parametrize("profile", battery.PASS_PROFILES)
def test_http_reconciliation_closes_every_case_and_pass_budget(profile: str) -> None:
    cases = _pass_cases(profile)
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    deltas = [delta for _case_id, delta in delta_ledger]
    total = _sum_http_deltas(deltas)
    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            delta_ledger,
            evidence_ledger,
            total,
        )
        is True
    )

    attempt_keys = ("model_http", "embedding_http", "reranker_http")
    for key, limit in zip(
        attempt_keys,
        battery._PROFILE_HTTP_SEND_LIMITS[profile],
        strict=True,
    ):
        overflowing = copy.deepcopy(delta_ledger)
        overflowing[0][1][key] = limit + 1
        assert (
            battery._http_probe_reconciliation_exact(
                cases,
                overflowing,
                evidence_ledger,
                _sum_http_deltas([delta for _case_id, delta in overflowing]),
            )
            is False
        )

    forged_total = dict(total)
    forged_total["model_http"] = (
        battery._PROFILE_HTTP_SEND_LIMITS[profile][0] * battery.QUESTIONS_PER_PASS + 1
    )
    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            delta_ledger,
            evidence_ledger,
            forged_total,
        )
        is False
    )

    for counter in battery._HTTP_PRIVACY_COUNTER_KEYS:
        exposed = copy.deepcopy(delta_ledger)
        exposed[0][1][counter] = 1
        assert (
            battery._http_probe_reconciliation_exact(
                cases,
                exposed,
                evidence_ledger,
                _sum_http_deltas([delta for _case_id, delta in exposed]),
            )
            is False
        )


@pytest.mark.parametrize("counter", ["model_http", "embedding_http", "reranker_http"])
def test_tenant_forbidden_turn_rejects_any_backend_http_send(counter: str) -> None:
    cases = _pass_cases("tenant_privacy")
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    delta_ledger[0][1][counter] = 1

    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            delta_ledger,
            evidence_ledger,
            _sum_http_deltas([delta for _case_id, delta in delta_ledger]),
        )
        is False
    )


@pytest.mark.parametrize("counter", ["model_http", "embedding_http", "reranker_http"])
def test_a_p02_code_owned_temporal_pass_rejects_any_backend_http_send(counter: str) -> None:
    cases = _pass_cases("package_b_temporal", battery_id="A")
    assert all(battery._package_a_code_owned_temporal_case(case) for case in cases)
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    assert all(delta["model_http"] == 0 for _case_id, delta in delta_ledger)
    forged = copy.deepcopy(delta_ledger)
    forged[0][1][counter] = 1

    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            forged,
            evidence_ledger,
            _sum_http_deltas([delta for _case_id, delta in forged]),
        )
        is False
    )


def test_b_p02_model_owned_temporal_pass_still_requires_every_model_send() -> None:
    cases = _pass_cases("package_b_temporal", battery_id="B")
    assert not any(battery._package_a_code_owned_temporal_case(case) for case in cases)
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    delta_ledger[0][1]["model_http"] = 0

    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            delta_ledger,
            evidence_ledger,
            _sum_http_deltas([delta for _case_id, delta in delta_ledger]),
        )
        is False
    )


@pytest.mark.parametrize("battery_id", ["A", "B"])
def test_p01_reconciliation_binds_frozen_routes_to_ordered_deltas_and_evidence(
    battery_id: str,
) -> None:
    cases = _pass_cases("package_a_honesty", battery_id=battery_id)
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    total = _sum_http_deltas([delta for _case_id, delta in delta_ledger])
    call = battery._http_probe_reconciliation_exact

    assert call(cases, delta_ledger, evidence_ledger, total) is True
    assert call(cases, delta_ledger[1:] + delta_ledger[:1], evidence_ledger, total) is False
    assert call(cases, delta_ledger, evidence_ledger[1:] + evidence_ledger[:1], total) is False
    assert call(cases[1:] + cases[:1], delta_ledger, evidence_ledger, total) is False
    assert call(cases, delta_ledger[:-1], evidence_ledger, total) is False
    assert call(cases, delta_ledger, evidence_ledger[:-1], total) is False


@pytest.mark.parametrize("field", battery._P01_ROUTE_EVIDENCE_KEYS)
def test_p01_code_owned_route_evidence_fails_closed_when_forged(field: str) -> None:
    cases = _pass_cases("package_a_honesty")
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    code_position = next(
        index for index, case in enumerate(cases) if battery._package_a_code_owned_case(case)
    )
    forged = copy.deepcopy(evidence_ledger)
    forged[code_position][1][field] = not bool(forged[code_position][1][field])

    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            delta_ledger,
            forged,
            _sum_http_deltas([delta for _case_id, delta in delta_ledger]),
        )
        is False
    )


@pytest.mark.parametrize("counter", battery._P01_CODE_OWNED_DELTA_ZERO_COUNTERS)
def test_p01_code_owned_route_rejects_any_logical_or_transport_attempt(counter: str) -> None:
    cases = _pass_cases("package_a_honesty")
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    code_position = next(
        index for index, case in enumerate(cases) if battery._package_a_code_owned_case(case)
    )
    forged = copy.deepcopy(delta_ledger)
    forged[code_position][1][counter] = 1

    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            forged,
            evidence_ledger,
            _sum_http_deltas([delta for _case_id, delta in forged]),
        )
        is False
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "marker",
        "answer_present",
        "remainder_known",
        "model_spoke_missing",
        "guard_missing",
        "terminal_both_false",
        "terminal_both_true",
        "llm_failed",
        "model_send",
    ],
)
def test_p01_model_owned_route_requires_closed_shape_and_positive_send(mutation: str) -> None:
    cases = _pass_cases("package_a_honesty")
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    model_position = next(
        index for index, case in enumerate(cases) if not battery._package_a_code_owned_case(case)
    )
    if mutation == "marker":
        evidence_ledger[model_position][1]["fabricated_outside_deed_request"] = True
    elif mutation == "answer_present":
        evidence_ledger[model_position][1]["answer_present"] = True
    elif mutation == "remainder_known":
        evidence_ledger[model_position][1]["remainder_known"] = True
    elif mutation == "model_spoke_missing":
        evidence_ledger[model_position][1]["model_spoke"] = None
    elif mutation == "guard_missing":
        evidence_ledger[model_position][1]["outside_deed_replaced"] = None
    elif mutation == "terminal_both_false":
        evidence_ledger[model_position][1]["model_spoke"] = False
    elif mutation == "terminal_both_true":
        evidence_ledger[model_position][1]["outside_deed_replaced"] = True
    elif mutation == "llm_failed":
        evidence_ledger[model_position][1]["llm_failed"] = True
    else:
        delta_ledger[model_position][1]["model_http"] = 0

    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            delta_ledger,
            evidence_ledger,
            _sum_http_deltas([delta for _case_id, delta in delta_ledger]),
        )
        is False
    )


@pytest.mark.parametrize("model_spoke", [False, True])
def test_p01_model_owned_route_accepts_safe_replacement_or_model_refusal(model_spoke: bool) -> None:
    cases = _pass_cases("package_a_honesty")
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    model_positions = [
        index for index, case in enumerate(cases) if not battery._package_a_code_owned_case(case)
    ]
    for position in model_positions:
        evidence_ledger[position][1]["model_spoke"] = model_spoke
        evidence_ledger[position][1]["outside_deed_replaced"] = not model_spoke

    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            delta_ledger,
            evidence_ledger,
            _sum_http_deltas([delta for _case_id, delta in delta_ledger]),
        )
        is True
    )


def test_p01_model_owned_pass_accepts_the_observed_mixed_terminal_states() -> None:
    cases = _pass_cases("package_a_honesty")
    delta_ledger, evidence_ledger = _closed_ledgers(cases)
    model_positions = [
        index for index, case in enumerate(cases) if not battery._package_a_code_owned_case(case)
    ]
    replacement_position = next(index for index, case in enumerate(cases) if case.id == "SYN-A01-11")
    assert replacement_position in model_positions

    for position in model_positions:
        replaced = position == replacement_position
        evidence_ledger[position][1]["model_spoke"] = not replaced
        evidence_ledger[position][1]["outside_deed_replaced"] = replaced
    # The rejected completion observed in v5 made two model sends before the
    # runtime guard published its deterministic refusal.  This remains within
    # the sealed per-case budget and must not be confused with zero transport.
    delta_ledger[replacement_position][1]["model_http"] = 2

    assert (
        battery._http_probe_reconciliation_exact(
            cases,
            delta_ledger,
            evidence_ledger,
            _sum_http_deltas([delta for _case_id, delta in delta_ledger]),
        )
        is True
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


def test_p01_oracle_uses_the_independently_frozen_code_owned_inventory() -> None:
    expected = {
        "A": {1, 2, 3, 4, 5, 6, 8, 9, 10, 12, 13, 14, 15, 17, 18},
        "B": {10, 11},
    }
    for battery_id, expected_indices in expected.items():
        cases = _pass_cases("package_a_honesty", battery_id=battery_id)
        assert {
            case.question_index for case in cases if battery._package_a_code_owned_case(case)
        } == expected_indices
        for case in cases:
            state = battery.oracle_for_case(case)["state"]
            if case.question_index in expected_indices:
                expected_route = {
                    "fabricated_outside_deed_request": True,
                    "answer_present": True,
                    "model_spoke": False,
                    "outside_deed_replaced": False,
                    "remainder_known": True,
                    "llm_failed": False,
                }
                assert {key: state["equals"][key] for key in expected_route} == expected_route
                assert all(
                    state["equals"][counter] == 0 for counter in battery._P01_CODE_OWNED_STATE_ZERO_COUNTERS
                )
                assert "model_http_attempts" not in state["min"]
            else:
                assert state["equals"]["fabricated_outside_deed_request"] is False
                assert state["equals"]["answer_present"] is False
                assert state["equals"]["remainder_known"] is False
                assert state["equals"]["llm_failed"] is False
                assert "model_spoke" not in state["equals"]
                assert state["min"]["model_http_attempts"] == 1
