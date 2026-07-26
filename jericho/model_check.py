"""Probe a configured LLM endpoint by actually using it, not by connecting to it.

A TCP connect proves nothing, and ``/models`` only proves the server has heard of the
model. What breaks an integration is what the model *emits*: a reasoning runtime that
narrates before it answers fills the output budget with its own monologue, and every
caller that parses the reply gets prose where it expected an answer or JSON.

So this generates. Each probe is a real request whose response is inspected for the
failure modes that matter, and the report says which of them Jericho would survive.
Verified against a vLLM serving a Qwen-family reasoning model, where an unsuppressed
run spent all 256 permitted tokens thinking and produced no answer at all.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from jericho.config import JerichoSettings

# Reasoning leaks announce themselves in the text. The closing tag is the reliable one;
# the prose markers catch runtimes that narrate without emitting any tag.
_THINKING_SIGNS = ("</think>", "<think>", "thinking process", "let me think", "here's a thinking")


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str
    seconds: float = 0.0
    tokens: int | None = None


@dataclass
class ModelReport:
    base_url: str
    model: str
    probes: list[Probe] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(probe.ok for probe in self.probes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "ok": self.ok,
            "probes": [
                {
                    "name": p.name,
                    "ok": p.ok,
                    "detail": p.detail,
                    "seconds": round(p.seconds, 3),
                    **({"tokens": p.tokens} if p.tokens is not None else {}),
                }
                for p in self.probes
            ],
        }


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _completion(
    client: httpx.Client,
    base_url: str,
    model: str,
    api_key: str,
    prompt: str,
    *,
    max_tokens: int,
    suppress_thinking: bool,
) -> tuple[dict[str, Any] | None, float, str]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    if suppress_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    started = time.monotonic()
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/chat/completions", headers=_headers(api_key), json=payload
        )
    except Exception as exc:
        return None, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    if response.status_code != 200:
        return None, elapsed, f"HTTP {response.status_code}: {response.text[:160]}"
    return response.json(), elapsed, ""


def _text_of(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    return str((message or {}).get("content") or "")


def _probe_embedding_batch(
    client: httpx.Client, settings: JerichoSettings, api_key: str, report: ModelReport
) -> None:
    """Send a batch the size the indexer actually sends.

    A single-input probe proves the service answers. It does not prove it accepts the
    requests Jericho makes: the indexer batches up to
    ``JERICHO_EMBEDDINGS_MAX_INPUTS_PER_REQUEST`` inputs per call. Measured against a
    real service — one input returned a 1024-dimension vector while every indexing
    request failed 422 with "batch size 64 > maximum allowed batch size 32", so the
    corpus ended up with zero vectors and retrieval scored exactly as it had without
    embeddings at all.
    """
    size = max(1, int(settings.embeddings_max_inputs_per_request))
    started = time.monotonic()
    try:
        response = client.post(
            f"{settings.embeddings_base_url.rstrip('/')}/embeddings",
            headers=_headers(settings.embeddings_api_key),
            json={
                "model": settings.embeddings_model or settings.llm_model,
                "input": [f"проверка {index}" for index in range(size)],
            },
        )
    except Exception as exc:
        report.probes.append(
            Probe("embeddings batch", False, f"{type(exc).__name__}: {exc}", time.monotonic() - started)
        )
        return
    elapsed = time.monotonic() - started
    if response.status_code == 200:
        returned = len(response.json().get("data", []))
        report.probes.append(
            Probe("embeddings batch", returned == size, f"{returned}/{size} векторов", elapsed)
        )
        return
    # The service usually states its limit; pass that through rather than making the
    # owner go and read its log.
    detail = response.text[:160].strip()
    report.probes.append(
        Probe(
            "embeddings batch",
            False,
            f"HTTP {response.status_code} на пакете из {size}: {detail} "
            "— уменьшите JERICHO_EMBEDDINGS_MAX_INPUTS_PER_REQUEST",
            elapsed,
        )
    )


def check_model(settings: JerichoSettings, *, timeout: float = 60.0) -> ModelReport:
    """Run every probe against the configured chat endpoint and the embeddings one."""

    base_url = settings.llm_base_url
    model = settings.llm_model
    api_key = settings.llm_api_key
    report = ModelReport(base_url=base_url, model=model)
    suppress = settings.profile.suppress_model_thinking

    with httpx.Client(timeout=timeout, trust_env=False) as client:
        # 1. Is the model this instance is configured for actually served?
        started = time.monotonic()
        try:
            response = client.get(f"{base_url.rstrip('/')}/models", headers=_headers(api_key))
            elapsed = time.monotonic() - started
            if response.status_code == 200:
                served = [
                    str(item.get("id")) for item in response.json().get("data", []) if isinstance(item, dict)
                ]
                report.probes.append(
                    Probe(
                        "endpoint",
                        model in served,
                        f"served: {', '.join(served) or 'none'}"
                        + ("" if model in served else f" — configured model {model!r} is NOT among them"),
                        elapsed,
                    )
                )
            else:
                hint = " (set JERICHO_LLM_API_KEY)" if response.status_code == 401 else ""
                report.probes.append(Probe("endpoint", False, f"HTTP {response.status_code}{hint}", elapsed))
                return report
        except Exception as exc:
            report.probes.append(
                Probe("endpoint", False, f"{type(exc).__name__}: {exc}", time.monotonic() - started)
            )
            return report

        # 2. Does it answer at all, inside a budget Jericho actually uses?
        data, elapsed, error = _completion(
            client,
            base_url,
            model,
            api_key,
            "Ответь одним словом: столица Франции?",
            max_tokens=256,
            suppress_thinking=suppress,
        )
        if data is None:
            report.probes.append(Probe("chat", False, error, elapsed))
            return report
        text = _text_of(data)
        tokens = int((data.get("usage") or {}).get("completion_tokens") or 0)
        finish = str((data.get("choices") or [{}])[0].get("finish_reason") or "")
        answered = bool(text.strip()) and finish != "length"
        report.probes.append(
            Probe(
                "chat",
                answered,
                f"{text.strip()[:60]!r}"
                if answered
                else f"no answer (finish_reason={finish}, {tokens} tokens spent)",
                elapsed,
                tokens,
            )
        )

        # 3. The one that matters: does the monologue reach the caller? A leak here
        #    means every Inbox suggestion and every answer carries the model's notes.
        lowered = text.casefold()
        leaked = [sign for sign in _THINKING_SIGNS if sign in lowered]
        report.probes.append(
            Probe(
                "reasoning suppressed",
                not leaked,
                "clean"
                if not leaked
                else f"chain-of-thought leaked into the answer: {leaked}"
                + ("" if suppress else " — profile.suppress_model_thinking is False"),
                0.0,
            )
        )

        # 4. Structured output: ingestion advice, entity extraction and vision all
        #    parse the reply as JSON, so prose around it is a silent failure.
        data, elapsed, error = _completion(
            client,
            base_url,
            model,
            api_key,
            'Верни СТРОГО один JSON-объект и ничего больше: {"kind":"note","importance":0.5}',
            max_tokens=256,
            suppress_thinking=suppress,
        )
        if data is None:
            report.probes.append(Probe("json output", False, error, elapsed))
        else:
            body = _text_of(data).strip()
            try:
                json.loads(body[body.find("{") : body.rfind("}") + 1])
                report.probes.append(Probe("json output", True, "parsed", elapsed))
            except (ValueError, json.JSONDecodeError):
                report.probes.append(Probe("json output", False, f"not parseable: {body[:80]!r}", elapsed))

        # 5. Embeddings are a SEPARATE service. Without them dense recall is off and
        #    search falls back to lexical only, which measurably loses cross-script
        #    matches (a Cyrillic query against a Latin name).
        if settings.embeddings_enabled:
            started = time.monotonic()
            try:
                response = client.post(
                    f"{settings.embeddings_base_url.rstrip('/')}/embeddings",
                    headers=_headers(settings.embeddings_api_key),
                    json={"model": settings.embeddings_model or model, "input": "проверка"},
                )
                elapsed = time.monotonic() - started
                if response.status_code == 200:
                    vector = response.json()["data"][0]["embedding"]
                    report.probes.append(Probe("embeddings", True, f"{len(vector)} dimensions", elapsed))
                    _probe_embedding_batch(client, settings, api_key, report)
                else:
                    report.probes.append(Probe("embeddings", False, f"HTTP {response.status_code}", elapsed))
            except Exception as exc:
                report.probes.append(
                    Probe("embeddings", False, f"{type(exc).__name__}: {exc}", time.monotonic() - started)
                )
        else:
            report.probes.append(
                Probe(
                    "embeddings",
                    True,
                    "disabled — dense recall is off, search is lexical only",
                    0.0,
                )
            )
    return report
