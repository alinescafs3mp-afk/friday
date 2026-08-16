#!/usr/bin/env python3
"""Run the sealed synthetic pre-release acceptance slices without retries.

The runner reuses the live battery's process, network, privacy and reconciliation
boundaries.  It adds only deterministic suite selection and closed aggregate
accounting for the two release-blocking slices:

* ``p06``: A-P06 plus B-P06, 40 cases;
* ``focused``: A-P01/P02/P04/P08/P09/P10, 120 cases;
* ``all``: both slices, dispatched from one immutable candidate snapshot.

Raw questions and responses stay below the ignored private run directory.  Stdout
contains only synthetic case IDs, closed failure codes, hashes and counters.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import hashlib
import json
import os
import queue
import re
import secrets
import signal
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402

RUNNER_PATH = Path(__file__).resolve()
RUNNER_RELATIVE_PATH = RUNNER_PATH.relative_to(ROOT).as_posix()
FOCUSED_PASS_INDEXES = (1, 2, 4, 8, 9, 10)
P06_PASS_KEYS = (("A", 6), ("B", 6))
FOCUSED_PASS_KEYS = tuple(("A", index) for index in FOCUSED_PASS_INDEXES)
SUITE_PASS_KEYS = {
    "p06": P06_PASS_KEYS,
    "focused": FOCUSED_PASS_KEYS,
    "all": (*FOCUSED_PASS_KEYS, *P06_PASS_KEYS),
}
SUITE_CASE_COUNTS = {"p06": 40, "focused": 120, "all": 160}
SUMMARY_SCHEMA = "friday.synthetic-live-battery.pre-release.v1"
P06_SCHEMA = "friday.synthetic-live-battery.p06-final.v1"
FOCUSED_SCHEMA = "friday.synthetic-live-battery.focused-final.v1"
# Four production-sized classifier requests have to finish together.  This is
# the same dispatcher workload and concurrency which the battery will exercise,
# so its per-request bound must not be shorter than Friday's configured 240-second
# foreground model contract.  A shorter gate bound produced false readiness
# failures even when one complete, valid classifier response arrived after about
# thirty seconds and the four-way wave stayed healthy but unfinished past two
# minutes.  The 255-second aggregate preserves a hard local ceiling while leaving
# room for two quiet intervals and three bounded metrics reads.
MODEL_READINESS_BUDGET_SEC = 255.0
MODEL_READINESS_METRICS_TIMEOUT_SEC = 2.0
MODEL_READINESS_GENERATION_TIMEOUT_SEC = 240.0
MODEL_READINESS_QUIET_SEC = 1.0
MODEL_READINESS_CONCURRENCY = 4
MODEL_READINESS_METRICS_MAX_BYTES = 2 * 1024 * 1024
MODEL_READINESS_GENERATION_MAX_BYTES = 256 * 1024
MODEL_READINESS_MIN_USABLE_RESPONSES = 3
# The remote dispatcher is intentionally saturated by a single long-context
# acceptance pass.  A second heavy pass can leave an otherwise tiny request
# queued beyond the production 240-second read deadline, poisoning the rest of
# that worker through the normal silent-endpoint cooldown.  Keep the public
# executor at four workers, but serialize only the four model-heavy profiles.
MODEL_HEAVY_PASS_CONCURRENCY = 1
_MODEL_HEAVY_PASS_PROFILES = frozenset(
    {
        "package_a_honesty",
        "k03_tag_inventory",
        "tools_and_fallback",
        "telegram_fake_transport",
    }
)
_MODEL_LANE_ACQUIRE_POLL_SEC = 0.1
MODEL_READINESS_CLASSIFIER_USER_MESSAGES = (
    "Найди актуальное расписание TEST-001",
    "Что написано в синтетическом акте TEST-002?",
    "Что писал участник Альфа TEST-003?",
    "Напомни завтра проверить TEST-004",
)
MODEL_READINESS_CLASSIFIER_EXPECTED_KINDS = (
    "интернет",
    "архив",
    "человек",
    "действие",
)
MODEL_READINESS_CLASSIFIER_KINDS = frozenset(
    {
        "архив",
        "быт",
        "действие",
        "другое",
        "знание",
        "интернет",
        "материал",
        "поправка",
        "правило",
        "файл",
        "человек",
    }
)
MODEL_READINESS_CLASSIFIER_KEYS = frozenset({"вид", "запрос", "кто", "дни", "правило"})

# This prompt is acceptance-owned and contains only synthetic examples.  It is
# deliberately the same two-message, approximately 6.9k-character workload as
# AgentRuntime._web_query_by_arbiter, but is fixed so a readiness run cannot
# collect conversation history, today's host state, or any private corpus text.
MODEL_READINESS_CLASSIFIER_SYSTEM_PROMPT = "\n".join(
    (
        "Реши, что от тебя хотят, и верни ОДНУ строку JSON: "
        '{"вид": "интернет|знание|архив|человек|файл|действие|быт|правило|'
        'поправка|материал|другое", "запрос": "строка для поисковика", '
        '"кто": "имя человека", "дни": ["число или дата"], '
        '"правило": "как себя вести впредь"}.',
        "действие — просят СДЕЛАТЬ что-то в системе, а не рассказать: «напомни завтра», "
        "«сохрани это», «озвучь», «поставь задачу», «добавь организацию», «запиши, что…». "
        "Это поручение, и оно требует действия, даже если сказано в два слова.",
        "человек — спрашивают про ПЕРЕПИСКУ И ДЕЙСТВИЯ в этой системе: что писал, "
        "спрашивал, присылал или делал участник, о чём был разговор, что говорилось раньше. "
        "«что писал участник Альфа», «чем занимался участник Бета», «активность участника "
        "Гамма», «о чём мы вчера говорили», «покажи начало нашей переписки», «что я тебе "
        "писал про поверку», «процитируй моё первое сообщение», а также короткое продолжение "
        "такого вопроса — «а участник Бета?», «а участник Альфа», «а он что?». Имя клади в "
        "поле «кто»; если спрашивают про СВОИ сообщения или про общий разговор, поле «кто» "
        "оставь пустым.",
        "Различай по тому, ГДЕ лежит ответ: разговоры и сообщения — это «человек», а "
        "документы, файлы и записи — «архив». «О чём был наш разговор про поверку» — "
        "человек; «что написано в акте поверки» — архив.",
        "Если вместо имени местоимение («а он что?», «а она?»), возьми ПОСЛЕДНЕГО названного "
        "человека из предыдущих реплик, а не первого.",
        "файл — просят СОБРАТЬ документ: «сделай справку в word», «оформи отчёт», «собери это "
        "в таблицу», «пришли файлом», «сделай из этого документ». Не путать с «покажи "
        "документ» — это архив.",
        "Если просят собрать ПРИСЛАННЫЕ файлы за какие-то дни («собери документы за 10, 13 "
        "и 25 число», «скинь архивом всё за вчера», «выгрузи файлы за 29 июля»), это тоже "
        '«файл», и дни перечисли в поле «дни» списком: ["10","13","25"] или '
        '["2099-01-29"]. Три числа — это три дня, а не отрезок между ними. Если про дни '
        "речи нет, поле «дни» оставь пустым.",
        "интернет — ответ мог ИЗМЕНИТЬСЯ с тех пор, как ты училась: новости, погода, курсы, "
        "цены, «кто сейчас», «сколько стоит», расписания, свежие версии, состояние дел на "
        "сегодня.",
        "знание — ответ не меняется И его не надо ВСПОМИНАТЬ: объяснения, определения, "
        "принципы, «что такое консенсус Raft», «чем отличается лизинг от аренды», «расскажи "
        "что-нибудь познавательное», а также вычислимое — «сколько дней в феврале 2096», "
        "«какой день недели 9 мая 2099».",
        "Сюда же — просьба о СУЖДЕНИИ или совете: «как думаешь», «стоит ли», «посоветуй», "
        "«твоё мнение», «что лучше выбрать». В интернете нет ответа на вопрос, что лучше для "
        "ЭТОГО человека; отвечай сама и честно скажи, что это твоё мнение, а не найденный "
        "факт.",
        "ВАЖНО: конкретный факт-справка — имя, дата, число, порядковый номер, название («кто "
        "был вторым президентом синтетической страны», «когда родился испытатель Альфа», "
        "«какая высота тестовой вершины», «столица страны Бета») — это «интернет», даже если "
        "он никогда не изменится. На проверочном стенде модель иногда уверенно отвечала "
        "неверным именем. Проверить такое стоит секунды, а ошибка выглядит как твёрдое "
        "знание.",
        "Оборот «что там по…», «как там с…», «что по…», «как обстоят дела с…» о деле, "
        "документе, задаче или рабочей теме — это АРХИВ: человек спрашивает о состоянии "
        "своего дела, а не о факте из внешнего мира. Отличай от «что нового в мире» и «что "
        "там в новостях» — вот это интернет.",
        "архив — спрашивают о личных МАТЕРИАЛАХ: «что у меня по…», «найди приказ», «сколько "
        "документов», «покажи акт», а также любой вопрос о том, что происходило в названный "
        "день или час («что было 26 июля в 15 часов», «чем занимались вчера»). Про САМИ "
        "РАЗГОВОРЫ — это «человек», а не «архив».",
        "материал — это не вопрос, а присланный текст: документ, приказ, письмо, пересланное "
        "сообщение, заметка на сохранение.",
        "быт — про обычную жизнь, а не про работу и не про архив: еда, сон, самочувствие, "
        "погода за окном, отдых, досуг, личные предпочтения («хочу супчика», «чай или кофе», "
        "«устал», «что посмотреть вечером»). Тут не нужны ни документы, ни поиск — просто "
        "поговори по-человечески.",
        "правило — человек говорит, КАК ТЕБЕ СЕБЯ ВЕСТИ впредь, а не спрашивает и не "
        "поручает разовое дело: «обращайся ко мне по имени-отчеству», «отвечай короче», «не "
        "называй меня так», «говори эту фразу только в ответ на благодарность», «больше не "
        "здоровайся каждый раз», «всегда пиши дату рядом с цифрами». Сюда же — ОТМЕНА "
        "раньше сказанного: «забудь, что я просил про…», «можно снова так делать». Сам текст "
        "указания положи в поле «правило» одной фразой, как запомнишь его для себя.",
        "Только ПРЯМОЕ указание ТЕБЕ является этим видом. Рассказ о чужом регламенте, "
        "инструкции или правиле организации — материал, а не правило твоего поведения: "
        "«Регламент требует указывать дату», «В инструкции сказано: всегда указывать дату», "
        "«Правило отдела: отчёты подписывает руководитель» — это «материал».",
        "Отличай от разового: «ответь покороче» про ЭТОТ ответ — это не правило, а «отвечай "
        "мне покороче» — правило. Сомневаешься, разовое это или впредь, — выбирай НЕ "
        "правило.",
        "Указание часто высказывают ВОПРОСОМ или упрёком, и это тоже правило: «ты зачем "
        "каждый раз говоришь мне…», «сколько можно повторять одно и то же», «почему ты всё "
        "время здороваешься», «тебе обязательно писать так длинно». Признак — недовольство "
        "ПОВТОРЯЮЩИМСЯ поведением, а не вопрос о факте. В поле «правило» положи то, чего "
        "человек хочет впредь, своими словами.",
        "Не путай с вопросом о переписке: «сколько раз я просил» и «сколько можно повторять» "
        "— это правило (человеку надоело), а «что я тебе писал про поверку» — это «человек» "
        "(он спрашивает содержание).",
        "поправка — человек исправляет СКАЗАННОЕ ТОБОЙ: «нет, не 27 июля, а 27 ноября», "
        "«неверно, он испытатель», «это уже не так — договор закрыли в мае», «ты путаешь, "
        "это другой отдел». Признак — человек утверждает, что верно, взамен того, что ты "
        "сказала.",
        "В поле «правило» положи поправку ОДНОЙ фразой и так, чтобы она была понятна сама по "
        "себе, без предыдущей реплики: не «нет, 27 ноября», а «Синтетический праздник — 27 "
        "ноября, а не 27 июля».",
        "Отличай от указания о поведении: «не называй меня так» — правило, «меня зовут не "
        "Альфа, а Бета» — поправка. Отличай и от нового вопроса: «а когда тестовый праздник?» "
        "— не поправка, там ничего не исправляют.",
        "другое — разговор, просьба сделать что-то в системе.",
        "Поле «запрос» заполняй только для вида «интернет»: коротко, до десяти слов, как "
        "человек набрал бы в поисковой строке.",
        "Сейчас 2099-01-01 12:00 (четверг), часовой пояс UTC. Считай относительные даты от "
        "этого синтетического момента. Не полагайся на память о текущей дате.",
        "Если в запрос просится год — бери ТЕКУЩИЙ, а не тот, что помнишь.",
        "ЯЗЫК ЗАПРОСА выбирай по тому, где лежит ответ. Просят зарубежные, иностранные, "
        "мировые источники или новости не из рунета — пиши запрос ПО-АНГЛИЙСКИ: русская "
        "формулировка приводит на русские сайты, чем бы ни был задан регион поиска. "
        "Спрашивают про синтетические страны Альфа, Бета или Гамма — пиши на языке страны, "
        "если знаешь его. В остальных случаях — на языке человека.",
        "Никаких пояснений, только JSON.",
    )
)
REQUIRED_ACCEPTANCE_PROFILE = "qwen36-27b-nvfp4-nvidia"
REQUIRED_ACCEPTANCE_MODEL = "dispatcher"
_VLLM_LOAD_METRICS = {
    "vllm:num_requests_running": "running",
    "vllm_num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
    "vllm_num_requests_waiting": "waiting",
}
_PROMETHEUS_SAMPLE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{.*\})?\s+"
    r"(?P<value>[^\s]+)(?:\s+[0-9]+)?\s*$"
)


@dataclass(frozen=True)
class SealedPass:
    manifest: Mapping[str, Any]
    pass_spec: Mapping[str, Any]
    cases: tuple[battery.ExpandedCase, ...]
    context: battery.PassContext

    @property
    def key(self) -> tuple[str, int]:
        return self.context.battery_id, self.context.pass_index


@dataclass(frozen=True)
class ExecutionResult:
    results: Mapping[tuple[str, int], Mapping[str, Any]]
    worker_codes: Mapping[tuple[str, int], str]
    dispatches: Mapping[str, int]
    candidate_files: tuple[str, ...]
    candidate_pre_sha256: str
    candidate_sealed_sha256: str
    candidate_post_sha256: str

    @property
    def candidate_identity(self) -> bool:
        return bool(self.candidate_pre_sha256 == self.candidate_sealed_sha256 == self.candidate_post_sha256)


@dataclass(frozen=True)
class ModelReadinessResult:
    queue_state: str
    metrics_samples: int
    probes_requested: int
    probes_completed: int
    usable_responses: int
    maximum_latency_ms: int

    @property
    def probes_clear(self) -> bool:
        return bool(
            self.probes_requested == self.probes_completed == MODEL_READINESS_CONCURRENCY
            and type(self.usable_responses) is int
            and MODEL_READINESS_MIN_USABLE_RESPONSES <= self.usable_responses <= MODEL_READINESS_CONCURRENCY
            and type(self.maximum_latency_ms) is int
            and 0 <= self.maximum_latency_ms <= round(MODEL_READINESS_BUDGET_SEC * 1000)
        )

    @property
    def dispatch_clear(self) -> bool:
        return bool((self.queue_state, self.metrics_samples) == ("clear", 3) and self.probes_clear)


@dataclass(frozen=True)
class ModelLoadSample:
    running: float
    waiting: float
    process_start_time_seconds: float


def _assert_required_acceptance_setting(
    environment: Mapping[str, str],
    *,
    friday_name: str,
    required: str,
    code_prefix: str,
) -> None:
    """Require one exact launch value without hiding a legacy-alias conflict."""

    legacy_name = "JERICHO_" + friday_name.removeprefix("FRIDAY_")
    friday_present = friday_name in environment
    legacy_present = legacy_name in environment
    friday_value = str(environment.get(friday_name, "")).strip()
    legacy_value = str(environment.get(legacy_name, "")).strip()
    if friday_present and legacy_present and friday_value != legacy_value:
        raise battery.BatteryContractError(f"{code_prefix}_alias_conflict")
    value = friday_value if friday_present else legacy_value
    if not (friday_present or legacy_present) or not value:
        raise battery.BatteryContractError(f"{code_prefix}_missing")
    if value != required:
        raise battery.BatteryContractError(f"{code_prefix}_mismatch")


def _assert_frozen_dispatcher_environment(environment: Mapping[str, str]) -> None:
    """Bind every live acceptance slice to the attested dispatcher profile."""

    _assert_required_acceptance_setting(
        environment,
        friday_name="FRIDAY_PROFILE",
        required=REQUIRED_ACCEPTANCE_PROFILE,
        code_prefix="acceptance_profile",
    )
    _assert_required_acceptance_setting(
        environment,
        friday_name="FRIDAY_LLM_MODEL",
        required=REQUIRED_ACCEPTANCE_MODEL,
        code_prefix="acceptance_model",
    )


async def _read_bounded_response(response: httpx.Response, *, maximum_bytes: int) -> bytes:
    """Read a readiness response without retaining an unbounded remote body."""

    chunks: list[bytes] = []
    observed = 0
    async for chunk in response.aiter_bytes():
        observed += len(chunk)
        if observed > maximum_bytes:
            raise battery.BatteryContractError("model_readiness_response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _vllm_load(body: bytes) -> ModelLoadSample | None:
    """Return load plus metric epoch, or ``None`` for non-vLLM metrics."""

    observed: dict[str, list[float]] = {"running": [], "waiting": []}
    process_starts: list[float] = []
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return None
    for line in text.splitlines():
        match = _PROMETHEUS_SAMPLE.fullmatch(line.strip())
        if match is None:
            continue
        name = match.group("name")
        kind = _VLLM_LOAD_METRICS.get(name)
        if kind is None and name != "process_start_time_seconds":
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            raise battery.BatteryContractError("model_readiness_metrics_invalid") from None
        if not (value >= 0.0 and value < float("inf")):
            raise battery.BatteryContractError("model_readiness_metrics_invalid")
        if kind is None:
            process_starts.append(value)
        else:
            observed[kind].append(value)
    if not observed["running"] and not observed["waiting"]:
        return None
    if not observed["running"] or not observed["waiting"] or len(process_starts) != 1:
        raise battery.BatteryContractError("model_readiness_metrics_incomplete")
    return ModelLoadSample(
        running=sum(observed["running"]),
        waiting=sum(observed["waiting"]),
        process_start_time_seconds=process_starts[0],
    )


def _bounded_http_timeout(deadline: float, *, ceiling: float) -> httpx.Timeout:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise battery.BatteryContractError("model_readiness_deadline_exhausted")
    bounded = min(ceiling, remaining)
    return httpx.Timeout(bounded, connect=min(2.0, bounded))


def _model_readiness_classifier_payloads(model: str) -> tuple[dict[str, Any], ...]:
    """Build four fixed, production-shaped classifier requests without private input."""

    if not (
        len(MODEL_READINESS_CLASSIFIER_USER_MESSAGES)
        == len(MODEL_READINESS_CLASSIFIER_EXPECTED_KINDS)
        == MODEL_READINESS_CONCURRENCY
    ):
        raise battery.BatteryContractError("model_readiness_probe_inventory_invalid")
    return tuple(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": MODEL_READINESS_CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        for user_message in MODEL_READINESS_CLASSIFIER_USER_MESSAGES
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting keys hidden by last-value wins."""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate classifier JSON key")
        value[key] = item
    return value


def _usable_model_readiness_classifier_response(
    response_body: bytes,
    *,
    expected_kind: str,
) -> bool:
    """Accept one response that the production arbiter can actually consume.

    Readiness is an availability gate, not a second and stricter intent parser.
    AgentRuntime extracts the JSON object from harmless prose/fences, normalizes
    the selected field and ignores fields irrelevant to the selected kind.  The
    readiness projection mirrors that boundary while retaining the stricter HTTP
    envelope, size, tool-call, duplicate-key and expected-kind checks.  No model
    content leaves this function.
    """

    try:
        generated = json.loads(response_body)
    except (UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(generated, Mapping):
        return False
    choices = generated.get("choices")
    if type(choices) is not list or len(choices) != 1:
        return False
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        return False
    finish_reason = first_choice.get("finish_reason")
    if finish_reason is not None and finish_reason != "stop":
        return False
    message = first_choice.get("message")
    if not isinstance(message, Mapping):
        return False
    tool_calls = message.get("tool_calls")
    if tool_calls not in (None, []):
        return False
    content = message.get("content")
    if type(content) is not str or not content or len(content) > 2_048:
        return False
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        verdict = json.loads(
            content[start : end + 1],
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (ValueError, TypeError):
        return False
    if type(verdict) is not dict:
        return False

    raw_kind = verdict.get("вид")
    if type(raw_kind) is not str:
        return False
    kind = " ".join(raw_kind.split()).casefold()
    if not kind.startswith(expected_kind) or not any(
        kind.startswith(allowed) for allowed in MODEL_READINESS_CLASSIFIER_KINDS
    ):
        return False
    if expected_kind == "интернет":
        query = verdict.get("запрос")
        return type(query) is str and bool(" ".join(query.split()))
    if expected_kind == "человек":
        who = verdict.get("кто")
        return type(who) is str and bool(" ".join(who.split()))
    return True


async def _async_model_readiness_barrier(
    environment: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None,
    sleeper: Callable[[float], None] | None,
    require_authoritative_metrics: bool,
) -> ModelReadinessResult:
    """Fail closed unless the configured model sustains four real classifiers.

    vLLM exposes queue gauges at ``/metrics``.  When both gauges are present we
    require a stable empty queue before the probes and an empty queue after them.
    Four simultaneous, production-shaped outward-intent classifiers then have to
    return HTTP 200, and at least three must contain a usable closed JSON verdict.
    One malformed semantic response is degraded availability, not dispatcher
    unavailability: the 160-case acceptance run that follows remains the semantic
    oracle.  Two malformed responses still fail closed.  Other
    OpenAI-compatible servers can be diagnosed through the generations, but their
    queue state is explicitly ``unknown`` rather than being presented as clear.
    The official acceptance caller requires authoritative metrics and therefore
    sends no probes when those are absent.  Only fixed synthetic prompts are sent,
    and neither metrics nor response bodies leave this function.
    """

    endpoint = battery._configured_model_endpoint_urls(environment)["model"]
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise battery.BatteryContractError("model_readiness_endpoint_invalid")
    model = battery._environment_setting(environment, "FRIDAY_LLM_MODEL", "dispatcher").strip()
    if not model:
        raise battery.BatteryContractError("model_readiness_model_missing")
    api_key = battery._environment_setting(environment, "FRIDAY_LLM_API_KEY").strip()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    metrics_url = urlunsplit((parsed.scheme, parsed.netloc, "/metrics", "", ""))
    generation_url = f"{endpoint.rstrip('/')}/chat/completions"
    deadline = time.monotonic() + MODEL_READINESS_BUDGET_SEC

    async def sample_load(client: httpx.AsyncClient) -> ModelLoadSample | None:
        try:
            async with client.stream(
                "GET",
                metrics_url,
                timeout=_bounded_http_timeout(
                    deadline,
                    ceiling=MODEL_READINESS_METRICS_TIMEOUT_SEC,
                ),
            ) as response:
                if response.status_code != 200:
                    return None
                return _vllm_load(
                    await _read_bounded_response(
                        response,
                        maximum_bytes=MODEL_READINESS_METRICS_MAX_BYTES,
                    )
                )
        except battery.BatteryContractError:
            raise
        except Exception:  # noqa: BLE001 - URL, token and remote body must not escape
            return None

    def require_idle(
        load: ModelLoadSample | None,
        *,
        metrics_required: bool,
        expected_epoch: float | None = None,
    ) -> bool:
        if load is None:
            if metrics_required:
                raise battery.BatteryContractError("model_readiness_metrics_lost")
            return False
        if expected_epoch is not None and load.process_start_time_seconds != expected_epoch:
            raise battery.BatteryContractError("model_readiness_metrics_epoch_changed")
        if load.running != 0.0 or load.waiting != 0.0:
            raise battery.BatteryContractError("model_readiness_model_busy")
        return True

    async def quiet_interval() -> None:
        if sleeper is None:
            await asyncio.sleep(MODEL_READINESS_QUIET_SEC)
        else:
            sleeper(MODEL_READINESS_QUIET_SEC)

    try:
        async with httpx.AsyncClient(
            headers=headers,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        ) as client:
            first_sample = await sample_load(client)
            metrics_observed = require_idle(first_sample, metrics_required=False)
            metrics_samples = 1 if metrics_observed else 0
            metrics_epoch = first_sample.process_start_time_seconds if first_sample is not None else None
            if not metrics_observed and require_authoritative_metrics:
                return ModelReadinessResult(
                    queue_state="unknown",
                    metrics_samples=0,
                    probes_requested=MODEL_READINESS_CONCURRENCY,
                    probes_completed=0,
                    usable_responses=0,
                    maximum_latency_ms=0,
                )
            if metrics_observed:
                await quiet_interval()
                require_idle(
                    await sample_load(client),
                    metrics_required=True,
                    expected_epoch=metrics_epoch,
                )
                metrics_samples += 1

            payloads = _model_readiness_classifier_payloads(model)
            launch = asyncio.Event()
            all_ready = asyncio.Event()
            ready_count = 0

            async def generation_probe(
                payload: Mapping[str, Any],
                *,
                expected_kind: str,
            ) -> tuple[int, bool]:
                nonlocal ready_count
                ready_count += 1
                if ready_count == MODEL_READINESS_CONCURRENCY:
                    all_ready.set()
                await launch.wait()
                # Give every released task a scheduling turn before the first
                # socket await.  The launch remains a true four-request wave;
                # an async transport can (and the unit contract does) hold all
                # four request bodies concurrently.
                await asyncio.sleep(0)
                started = time.monotonic()
                probe_deadline = min(
                    deadline,
                    started + MODEL_READINESS_GENERATION_TIMEOUT_SEC,
                )

                async def one_request() -> bool:
                    async with client.stream(
                        "POST",
                        generation_url,
                        json=payload,
                        timeout=_bounded_http_timeout(
                            probe_deadline,
                            ceiling=MODEL_READINESS_GENERATION_TIMEOUT_SEC,
                        ),
                    ) as response:
                        if response.status_code != 200:
                            raise battery.BatteryContractError("model_readiness_generation_failed")
                        response_body = await _read_bounded_response(
                            response,
                            maximum_bytes=MODEL_READINESS_GENERATION_MAX_BYTES,
                        )
                    return _usable_model_readiness_classifier_response(
                        response_body,
                        expected_kind=expected_kind,
                    )

                probe_budget = probe_deadline - time.monotonic()
                if probe_budget <= 0:
                    raise battery.BatteryContractError("model_readiness_deadline_exhausted")
                try:
                    usable = await asyncio.wait_for(one_request(), timeout=probe_budget)
                except TimeoutError:
                    raise battery.BatteryContractError("model_readiness_deadline_exhausted") from None
                return max(0, round((time.monotonic() - started) * 1000)), usable

            tasks = [
                asyncio.create_task(
                    generation_probe(payload, expected_kind=expected_kind),
                    name=f"model-readiness-{index}",
                )
                for index, (payload, expected_kind) in enumerate(
                    zip(payloads, MODEL_READINESS_CLASSIFIER_EXPECTED_KINDS, strict=True)
                )
            ]
            try:
                ready_budget = min(2.0, deadline - time.monotonic())
                if ready_budget <= 0:
                    raise battery.BatteryContractError("model_readiness_deadline_exhausted")
                try:
                    await asyncio.wait_for(all_ready.wait(), timeout=ready_budget)
                except TimeoutError:
                    raise battery.BatteryContractError("model_readiness_concurrency_failed") from None
                launch.set()
                completion_budget = deadline - time.monotonic()
                if completion_budget <= 0:
                    raise battery.BatteryContractError("model_readiness_deadline_exhausted")
                try:
                    completed = list(
                        await asyncio.wait_for(
                            asyncio.gather(*tasks),
                            timeout=completion_budget,
                        )
                    )
                except TimeoutError:
                    raise battery.BatteryContractError("model_readiness_deadline_exhausted") from None
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if len(completed) != MODEL_READINESS_CONCURRENCY:
                raise battery.BatteryContractError("model_readiness_concurrency_failed")
            latencies = [latency for latency, _usable in completed]
            usable_responses = sum(usable for _latency, usable in completed)
            if usable_responses < MODEL_READINESS_MIN_USABLE_RESPONSES:
                raise battery.BatteryContractError("model_readiness_generation_invalid")

            if metrics_observed:
                await quiet_interval()
                require_idle(
                    await sample_load(client),
                    metrics_required=True,
                    expected_epoch=metrics_epoch,
                )
                metrics_samples += 1
    except battery.BatteryContractError:
        raise
    except Exception:  # noqa: BLE001 - expose only a closed failure code
        raise battery.BatteryContractError("model_readiness_probe_failed") from None
    if time.monotonic() > deadline:
        raise battery.BatteryContractError("model_readiness_deadline_exhausted")
    return ModelReadinessResult(
        queue_state="clear" if metrics_observed else "unknown",
        metrics_samples=metrics_samples,
        probes_requested=MODEL_READINESS_CONCURRENCY,
        probes_completed=len(latencies),
        usable_responses=usable_responses,
        maximum_latency_ms=max(latencies),
    )


def _model_readiness_barrier(
    environment: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleeper: Callable[[float], None] | None = None,
    require_authoritative_metrics: bool = True,
) -> ModelReadinessResult:
    """Run the asynchronous readiness barrier under one absolute local bound."""

    async def bounded() -> ModelReadinessResult:
        try:
            return await asyncio.wait_for(
                _async_model_readiness_barrier(
                    environment,
                    transport=transport,
                    sleeper=sleeper,
                    require_authoritative_metrics=require_authoritative_metrics,
                ),
                timeout=MODEL_READINESS_BUDGET_SEC,
            )
        except TimeoutError:
            raise battery.BatteryContractError("model_readiness_deadline_exhausted") from None

    try:
        return asyncio.run(bounded())
    except battery.BatteryContractError:
        raise
    except Exception:  # noqa: BLE001 - expose no URL, token or remote detail
        raise battery.BatteryContractError("model_readiness_probe_failed") from None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _private_tree(root: Path) -> bool:
    """Require 0700 directories, 0600 files and no symlinks."""

    try:
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
            return False
        for path in root.rglob("*"):
            if path.is_symlink():
                return False
            mode = stat.S_IMODE(path.stat().st_mode)
            if path.is_dir() and mode != 0o700:
                return False
            if path.is_file() and mode != 0o600:
                return False
            if not path.is_dir() and not path.is_file():
                return False
        return True
    except OSError:
        return False


def _read_reconciliation(
    path: Path,
    *,
    kind: str,
) -> tuple[bool, str, dict[str, bool], str]:
    """Read only the closed reconciliation record and validate its own digest."""

    try:
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            return False, "", {}, "reconciliation_evidence_not_private"
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "", {}, "reconciliation_evidence_invalid"
    if not isinstance(value, Mapping):
        return False, "", {}, "reconciliation_shape_invalid"
    if kind == "pass":
        components = {
            "api_exact",
            "audit_exact",
            "counters_exact",
            "files_exact",
            "http_exact",
            "storage_exact",
            "tools_exact",
        }
        expected = {"schema", "clear", "snapshot_sha256", *components}
        schema = battery.RECONCILIATION_SCHEMA
    elif kind == "tail":
        components = {"probe_exact", "files_exact", "database_exact"}
        expected = {"schema", "clear", "snapshot_sha256", *components}
        schema = "friday.synthetic-live-battery.tail-reconciliation.v1"
    else:
        return False, "", {}, "reconciliation_kind_invalid"
    if (
        set(value) != expected
        or value.get("schema") != schema
        or any(type(value.get(key)) is not bool for key in components | {"clear"})
        or not battery._is_sha256(value.get("snapshot_sha256"))
    ):
        return False, "", {}, "reconciliation_shape_invalid"
    unsigned = {key: value[key] for key in value if key != "snapshot_sha256"}
    snapshot_exact = value["snapshot_sha256"] == battery._sha256_bytes(
        battery._canonical_json_bytes(unsigned)
    )
    clear_exact = value["clear"] is all(value[key] is True for key in components)
    clear = bool(snapshot_exact and clear_exact and value["clear"] is True)
    component_values = {key: value[key] is True for key in components}
    full_hash = battery._sha256_bytes(battery._canonical_json_bytes(value))
    return clear, str(value["snapshot_sha256"]), component_values, full_hash


def _suite_keys(suite: str) -> tuple[tuple[str, int], ...]:
    try:
        return tuple(SUITE_PASS_KEYS[suite])
    except KeyError:
        raise battery.BatteryContractError("acceptance_suite_invalid") from None


def _load_manifests() -> dict[str, tuple[str, Mapping[str, Any]]]:
    audit = battery.audit_frozen_manifests()
    if audit.get("valid") is not True:
        raise battery.BatteryContractError("manifest_audit_failed")
    manifests: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for battery_id in ("A", "B"):
        path = battery.MANIFEST_PATHS[battery_id]
        digest = battery.file_sha256(path)
        manifest = battery.load_manifest(path)
        if digest != battery.FROZEN_MANIFEST_SHA256[battery_id] or battery.manifest_complaints(
            manifest, expected_battery=battery_id
        ):
            raise battery.BatteryContractError("manifest_audit_failed")
        manifests[battery_id] = digest, manifest
    return manifests


def inventory_for_suite(suite: str) -> dict[str, Any]:
    """Return a closed, model-free inventory for tests and operator preflight."""

    manifests = _load_manifests()
    case_ids: list[str] = []
    questions: list[str] = []
    pass_ids: list[str] = []
    for battery_id, pass_index in _suite_keys(suite):
        _manifest_hash, manifest = manifests[battery_id]
        pass_spec = list(manifest["passes"])[pass_index - 1]
        cases = [case for case in battery.expand_manifest_cases(manifest) if case.pass_index == pass_index]
        expected_pass_id = f"{battery_id}-P{pass_index:02d}"
        if (
            len(cases) != battery.QUESTIONS_PER_PASS
            or str(pass_spec.get("pass_id") or "") != expected_pass_id
            or any(case.pass_id != expected_pass_id for case in cases)
        ):
            raise battery.BatteryContractError("acceptance_pass_inventory_invalid")
        if (battery_id, pass_index) in P06_PASS_KEYS and any(
            case.oracle_profile != "tenant_privacy" for case in cases
        ):
            raise battery.BatteryContractError("p06_profile_invalid")
        pass_ids.append(expected_pass_id)
        case_ids.extend(case.id for case in cases)
        questions.extend(case.question for case in cases)
    expected_cases = SUITE_CASE_COUNTS[suite]
    if (
        len(pass_ids) != len(set(pass_ids))
        or len(case_ids) != expected_cases
        or len(case_ids) != len(set(case_ids))
        or len(questions) != len(set(questions))
    ):
        raise battery.BatteryContractError("acceptance_suite_inventory_invalid")
    candidate_files = battery._candidate_source_paths(instrument_path=RUNNER_PATH)
    if RUNNER_RELATIVE_PATH not in candidate_files:
        raise battery.BatteryContractError("acceptance_runner_not_candidate_bound")
    return {
        "schema": "friday.synthetic-live-battery.pre-release-audit.v1",
        "valid": True,
        "suite": suite,
        "passes": len(pass_ids),
        "cases": len(case_ids),
        "pass_ids": pass_ids,
        "manifest_sha256": {
            battery_id: manifest_hash for battery_id, (manifest_hash, _manifest) in manifests.items()
        },
        "candidate_source_sha256": battery._candidate_source_digest(relative_paths=candidate_files),
        "runner_sha256": battery.file_sha256(RUNNER_PATH),
    }


def _make_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    battery._require_private_directory(path)


WORKER_TEARDOWN_WAIT_SEC = 10.0
WORKER_THREAD_TEARDOWN_WAIT_SEC = 15.0
TEARDOWN_HARD_EXIT_CODE = 70
_ACCEPTANCE_LOCK_HOST_ROOT = Path("/tmp")
_ACCEPTANCE_LOCK_NAMESPACE = "friday-synthetic-live-acceptance"
_ACCEPTANCE_LOCK_PROTOCOL = b"friday.synthetic-live-acceptance\0v2"


class _AcceptanceTerminationRequested(BaseException):
    """Private control flow raised by the scoped SIGTERM handler."""


class _TerminationSignalGuard:
    """Translate SIGTERM into unwindable control flow and ignore repeats."""

    def __init__(self) -> None:
        self._previous: Any = None
        self._terminating = False

    def __enter__(self) -> _TerminationSignalGuard:
        if threading.current_thread() is not threading.main_thread():
            raise battery.BatteryContractError("acceptance_signal_guard_unavailable")
        self._previous = signal.getsignal(signal.SIGTERM)

        def terminate(_signum: int, _frame: Any) -> None:
            if self._terminating:
                return
            self._terminating = True
            raise _AcceptanceTerminationRequested()

        signal.signal(signal.SIGTERM, terminate)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        signal.signal(signal.SIGTERM, self._previous)


def _hard_exit_after_incomplete_teardown() -> None:
    """Never release the lifecycle lock while a cancellation-hostile worker lives."""

    os._exit(TEARDOWN_HARD_EXIT_CODE)  # noqa: SLF001 - deliberate fail-closed process exit


def _acceptance_lock_path() -> Path:
    """Return one code-owned host lock for the current operating-system UID.

    ``FRIDAY_HOME`` is deliberately absent from this identity.  Live acceptance
    commonly gives every immutable candidate its own isolated home, while all of
    those candidates still control the same host resources and must serialize.
    The hard-coded ``/tmp`` namespace is host-local; the UID component keeps
    unrelated operating-system accounts independent.
    """

    lock_home = _ACCEPTANCE_LOCK_HOST_ROOT / f"{_ACCEPTANCE_LOCK_NAMESPACE}-{os.getuid()}"
    return lock_home / "runtime" / "locks" / "synthetic-live-acceptance.lock"


def _acceptance_anchor_address() -> bytes:
    """Return one path-free Linux abstract-socket identity for this UID."""

    identity = _ACCEPTANCE_LOCK_PROTOCOL + b"\0uid=" + str(os.getuid()).encode("ascii")
    digest = hashlib.sha256(identity).hexdigest().encode("ascii")
    return b"\0friday.synthetic-live-acceptance." + digest


def _open_acceptance_anchor() -> socket.socket:
    """Acquire the non-replaceable kernel anchor before touching the lock file."""

    anchor: socket.socket | None = None
    try:
        anchor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        anchor.set_inheritable(False)
        if anchor.get_inheritable():
            raise OSError("acceptance anchor remained inheritable")
        anchor.bind(_acceptance_anchor_address())
    except OSError as exc:
        if anchor is not None:
            anchor.close()
        code = (
            "acceptance_run_already_active"
            if exc.errno == errno.EADDRINUSE
            else "acceptance_lock_unavailable"
        )
        raise battery.BatteryContractError(code) from None
    except Exception:
        if anchor is not None:
            anchor.close()
        raise battery.BatteryContractError("acceptance_lock_unavailable") from None
    except BaseException:
        if anchor is not None:
            anchor.close()
        raise
    return anchor


def _open_code_owned_lock_home(operator_home: Path, directory_flags: int) -> int:
    """Create/open the UID namespace beneath a trusted host-local sticky root."""

    expected_home = _ACCEPTANCE_LOCK_HOST_ROOT / f"{_ACCEPTANCE_LOCK_NAMESPACE}-{os.getuid()}"
    if operator_home != expected_home:
        return os.open(operator_home, directory_flags)

    try:
        host_descriptor = os.open(_ACCEPTANCE_LOCK_HOST_ROOT, directory_flags)
    except OSError:
        raise battery.BatteryContractError("acceptance_lock_unavailable") from None
    try:
        host = os.fstat(host_descriptor)
        host_mode = stat.S_IMODE(host.st_mode)
        trusted_system_root = host.st_uid == 0 and bool(host_mode & stat.S_ISVTX)
        trusted_private_root = host.st_uid == os.getuid() and host_mode == 0o700
        if not stat.S_ISDIR(host.st_mode) or not (trusted_system_root or trusted_private_root):
            raise battery.BatteryContractError("acceptance_lock_unavailable")
        with contextlib.suppress(FileExistsError):
            os.mkdir(operator_home.name, 0o700, dir_fd=host_descriptor)
        home_descriptor = os.open(operator_home.name, directory_flags, dir_fd=host_descriptor)
    except OSError:
        raise battery.BatteryContractError("acceptance_lock_unavailable") from None
    finally:
        os.close(host_descriptor)
    home = os.fstat(home_descriptor)
    if not stat.S_ISDIR(home.st_mode) or home.st_uid != os.getuid() or stat.S_IMODE(home.st_mode) != 0o700:
        os.close(home_descriptor)
        raise battery.BatteryContractError("acceptance_lock_unavailable")
    return home_descriptor


def _open_acceptance_lock_directory(path: Path) -> int:
    """Open the private UID lock directory without following path symlinks."""

    locks = path.parent
    runtime = locks.parent
    operator_home = runtime.parent
    if locks.name != "locks" or runtime.name != "runtime" or not path.name:
        raise battery.BatteryContractError("acceptance_lock_unavailable")
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        home_descriptor = _open_code_owned_lock_home(operator_home, directory_flags)
    except OSError:
        raise battery.BatteryContractError("acceptance_lock_unavailable") from None
    try:
        home = os.fstat(home_descriptor)
        if not stat.S_ISDIR(home.st_mode) or home.st_uid != os.getuid():
            raise battery.BatteryContractError("acceptance_lock_unavailable")
        with contextlib.suppress(FileExistsError):
            os.mkdir("runtime", 0o700, dir_fd=home_descriptor)
        runtime_descriptor = os.open("runtime", directory_flags, dir_fd=home_descriptor)
    except OSError:
        raise battery.BatteryContractError("acceptance_lock_unavailable") from None
    finally:
        os.close(home_descriptor)
    try:
        runtime_metadata = os.fstat(runtime_descriptor)
        if (
            not stat.S_ISDIR(runtime_metadata.st_mode)
            or runtime_metadata.st_uid != os.getuid()
            or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        ):
            raise battery.BatteryContractError("acceptance_lock_unavailable")
        with contextlib.suppress(FileExistsError):
            os.mkdir("locks", 0o700, dir_fd=runtime_descriptor)
        locks_descriptor = os.open("locks", directory_flags, dir_fd=runtime_descriptor)
    except OSError:
        raise battery.BatteryContractError("acceptance_lock_unavailable") from None
    finally:
        os.close(runtime_descriptor)
    metadata = os.fstat(locks_descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(locks_descriptor)
        raise battery.BatteryContractError("acceptance_lock_unavailable")
    return locks_descriptor


class _ExclusiveAcceptanceRun:
    """One fail-fast host lock spanning readiness, dispatch and teardown."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None
        self._anchor: socket.socket | None = None

    def __enter__(self) -> _ExclusiveAcceptanceRun:
        import fcntl

        anchor = _open_acceptance_anchor()
        descriptor: int | None = None
        try:
            battery._assert_ignored_or_external(self.path)
            directory_descriptor = _open_acceptance_lock_directory(self.path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path.name, flags, 0o600, dir_fd=directory_descriptor)
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                ):
                    raise OSError("acceptance lock identity invalid")
                os.fchmod(descriptor, 0o600)
                metadata = os.fstat(descriptor)
                if stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise OSError("acceptance lock mode invalid")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                path_metadata = os.stat(
                    self.path.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(path_metadata.st_mode) or (
                    path_metadata.st_dev,
                    path_metadata.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
                    raise OSError("acceptance lock path identity changed")
            finally:
                os.close(directory_descriptor)
        except BlockingIOError:
            if descriptor is not None:
                os.close(descriptor)
            anchor.close()
            raise battery.BatteryContractError("acceptance_run_already_active") from None
        except battery.BatteryContractError:
            if descriptor is not None:
                os.close(descriptor)
            anchor.close()
            raise
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            anchor.close()
            raise battery.BatteryContractError("acceptance_lock_unavailable") from None
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            anchor.close()
            raise
        self._descriptor = descriptor
        self._anchor = anchor
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        import fcntl

        del exc_type, exc, traceback
        descriptor = self._descriptor
        anchor = self._anchor
        self._descriptor = None
        self._anchor = None
        try:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            if anchor is not None:
                anchor.close()


class _TrackedSubprocessModule:
    """Delegate subprocess APIs while interposing on isolated worker Popen calls."""

    def __init__(self, module: Any, registry: _WorkerProcessRegistry) -> None:
        self._module = module
        self.Popen = registry.popen  # noqa: N815 - mirrors subprocess' public class name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


class _WorkerProcessRegistry:
    """Close the Popen-registration race and reap every active worker group."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._module: Any = None
        self._proxy: _TrackedSubprocessModule | None = None
        self._processes: dict[int, Any] = {}
        self._stopping = False

    def __enter__(self) -> _WorkerProcessRegistry:
        self._module = battery.subprocess
        self._proxy = _TrackedSubprocessModule(self._module, self)
        battery.subprocess = self._proxy
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc, traceback
        if exc_type is not None:
            self.stop_and_reap()
        if self._proxy is not None and battery.subprocess is self._proxy:
            battery.subprocess = self._module
        self._proxy = None

    def popen(self, *args: Any, **kwargs: Any) -> Any:
        """Create and register one new-session worker atomically against teardown."""

        if kwargs.get("start_new_session") is not True:
            return self._module.Popen(*args, **kwargs)
        with self._lock:
            if self._stopping:
                raise battery.BatteryContractError("acceptance_worker_dispatch_stopped")
            process = self._module.Popen(*args, **kwargs)
            pid = int(getattr(process, "pid", 0) or 0)
            if pid <= 1:
                with contextlib.suppress(Exception):
                    process.kill()
                with contextlib.suppress(Exception):
                    process.wait(timeout=WORKER_TEARDOWN_WAIT_SEC)
                raise battery.BatteryContractError("acceptance_worker_pid_invalid")
            self._processes[pid] = process
            return process

    def stop_and_reap(self) -> bool:
        """Prevent later spawns, SIGKILL active process groups, then wait each child."""

        with self._lock:
            self._stopping = True
            active = tuple(
                (pid, process) for pid, process in self._processes.items() if process.poll() is None
            )
        clear = True
        for pid, _process in active:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                clear = False
        deadline = time.monotonic() + WORKER_TEARDOWN_WAIT_SEC
        for _pid, process in active:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    clear = False
                    continue
                process.wait(timeout=remaining)
            except Exception:  # noqa: BLE001 - teardown must preserve the initiating BaseException
                clear = False
                with contextlib.suppress(Exception):
                    process.kill()
                with contextlib.suppress(Exception):
                    process.wait(timeout=max(0.0, min(1.0, deadline - time.monotonic())))
        return clear and all(process.poll() is not None for _pid, process in active)


def _preseal_passes(
    suite: str,
    run_root: Path,
    manifests: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> tuple[SealedPass, ...]:
    """Create every isolated home/evidence path before the first dispatch."""

    sealed: list[SealedPass] = []
    passes_root = run_root / "passes"
    _make_private_directory(passes_root)
    for battery_id, pass_index in _suite_keys(suite):
        manifest_hash, manifest = manifests[battery_id]
        pass_spec = list(manifest["passes"])[pass_index - 1]
        cases = tuple(
            case for case in battery.expand_manifest_cases(manifest) if case.pass_index == pass_index
        )
        pass_id = f"{battery_id}-P{pass_index:02d}"
        if len(cases) != battery.QUESTIONS_PER_PASS or pass_spec.get("pass_id") != pass_id:
            raise battery.BatteryContractError("acceptance_pass_inventory_invalid")
        pass_root = passes_root / pass_id
        home = pass_root / "home"
        evidence_dir = pass_root / "evidence"
        for directory in (pass_root, home, evidence_dir):
            _make_private_directory(directory)
        battery._prepare_process_scratch(home)
        sealed.append(
            SealedPass(
                manifest=manifest,
                pass_spec=pass_spec,
                cases=cases,
                context=battery.PassContext(
                    battery_id=battery_id,
                    pass_id=pass_id,
                    pass_index=pass_index,
                    seed=int(manifest["seed"]) + pass_index,
                    clock=str(manifest["clock"]),
                    timezone=str(manifest["timezone"]),
                    manifest_sha256=manifest_hash,
                    home=home.resolve(),
                    evidence_path=(evidence_dir / "raw-responses.jsonl").resolve(),
                ),
            )
        )
    if len(sealed) != len(_suite_keys(suite)):
        raise battery.BatteryContractError("acceptance_preseal_incomplete")
    return tuple(sealed)


def _execute_sealed(
    sealed: Sequence[SealedPass],
    *,
    concurrency: int,
    model_environment: Mapping[str, str],
) -> ExecutionResult:
    if type(concurrency) is not int or not (1 <= concurrency <= battery.MAX_CONCURRENCY):
        raise battery.BatteryContractError("concurrency_out_of_range")
    if not isinstance(model_environment, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in model_environment.items()
    ):
        raise battery.BatteryContractError("model_environment_invalid")
    candidate_files = battery._candidate_source_paths(instrument_path=RUNNER_PATH)
    if RUNNER_RELATIVE_PATH not in candidate_files:
        raise battery.BatteryContractError("acceptance_runner_not_candidate_bound")
    candidate_pre = battery._candidate_source_digest(relative_paths=candidate_files)
    results: dict[tuple[str, int], Mapping[str, Any]] = {}
    worker_codes: dict[tuple[str, int], str] = {}
    dispatches = {item.context.pass_id: 0 for item in sealed}
    dispatch_lock = threading.Lock()
    model_lane = threading.BoundedSemaphore(MODEL_HEAVY_PASS_CONCURRENCY)
    pass_profiles: dict[tuple[str, int], str] = {}
    for item in sealed:
        profiles = {case.oracle_profile for case in item.cases}
        if len(profiles) != 1:
            raise battery.BatteryContractError("acceptance_pass_profile_invalid")
        pass_profiles[item.key] = next(iter(profiles))
    with battery.SubprocessPassExecutor(
        dict(model_environment),
        instrument_path=RUNNER_PATH,
    ) as executor:
        candidate_sealed = str(executor._candidate_source_sha256)
        if executor._candidate_files != candidate_files or candidate_sealed != candidate_pre:
            raise battery.BatteryContractError("candidate_preseal_identity_invalid")

        def execute_one(item: SealedPass) -> tuple[tuple[str, int], Mapping[str, Any], str]:
            with dispatch_lock:
                dispatches[item.context.pass_id] += 1
            code = ""
            try:
                value = executor(
                    item.manifest,
                    item.pass_spec,
                    item.cases,
                    item.context,
                )
            except Exception:  # noqa: BLE001 - raw detail stays inside private worker evidence
                value = battery._pass_failure(item.cases, "pass_worker_error")
                code = "pass_worker_error"
            if not battery._validate_pass_result(value, item.cases):
                value = battery._pass_failure(item.cases, "pass_result_invalid")
                code = "pass_result_invalid"
            return item.key, dict(value), code

        work: queue.Queue[SealedPass | None] = queue.Queue()
        completed: queue.Queue[tuple[str, Any]] = queue.Queue()
        stop = threading.Event()
        worker_count = min(concurrency, len(sealed))
        for item in sealed:
            work.put(item)
        for _index in range(worker_count):
            work.put(None)

        def worker() -> None:
            while not stop.is_set():
                item = work.get()
                if item is None or stop.is_set():
                    return
                model_lane_acquired = False
                try:
                    if pass_profiles[item.key] in _MODEL_HEAVY_PASS_PROFILES:
                        while not stop.is_set():
                            model_lane_acquired = model_lane.acquire(timeout=_MODEL_LANE_ACQUIRE_POLL_SEC)
                            if model_lane_acquired:
                                break
                        if not model_lane_acquired:
                            return
                    completed.put(("result", execute_one(item)))
                except BaseException as exc:  # noqa: BLE001 - propagate after process-group teardown
                    stop.set()
                    completed.put(("error", exc))
                    return
                finally:
                    if model_lane_acquired:
                        model_lane.release()

        threads = [
            threading.Thread(
                target=worker,
                name=f"acceptance-pass-{index}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        started_threads: list[threading.Thread] = []
        with _WorkerProcessRegistry() as registry:
            try:
                # Thread startup belongs to the same lifecycle transaction as
                # result collection.  A signal or start failure here must not
                # release the process proxy and host lock while a previously
                # started worker can still resume and spawn a child.
                for thread in threads:
                    started_threads.append(thread)
                    thread.start()
                for _index in range(len(sealed)):
                    kind, payload = completed.get()
                    if kind == "error":
                        raise payload
                    key, value, code = payload
                    results[key] = value
                    worker_codes[key] = code
            except BaseException:
                stop.set()
                previous_sigint: Any = None
                previous_sigterm: Any = None
                can_mask_signals = threading.current_thread() is threading.main_thread()
                if can_mask_signals:
                    previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
                    previous_sigterm = signal.signal(signal.SIGTERM, signal.SIG_IGN)
                try:
                    processes_clear = registry.stop_and_reap()
                    deadline = time.monotonic() + WORKER_THREAD_TEARDOWN_WAIT_SEC
                    for thread in started_threads:
                        if thread.ident is None:
                            continue
                        thread.join(timeout=max(0.0, deadline - time.monotonic()))
                finally:
                    if can_mask_signals:
                        signal.signal(signal.SIGTERM, previous_sigterm)
                        signal.signal(signal.SIGINT, previous_sigint)
                if not processes_clear or any(thread.is_alive() for thread in started_threads):
                    _hard_exit_after_incomplete_teardown()
                    raise battery.BatteryContractError("acceptance_worker_teardown_incomplete") from None
                raise
            else:
                deadline = time.monotonic() + WORKER_THREAD_TEARDOWN_WAIT_SEC
                for thread in started_threads:
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if any(thread.is_alive() for thread in started_threads):
                    stop.set()
                    registry.stop_and_reap()
                    _hard_exit_after_incomplete_teardown()
                    raise battery.BatteryContractError("acceptance_worker_teardown_incomplete")
        executor._assert_candidate_unchanged()
    candidate_post_files = battery._candidate_source_paths(instrument_path=RUNNER_PATH)
    candidate_post = battery._candidate_source_digest(relative_paths=candidate_post_files)
    if candidate_post_files != candidate_files:
        raise battery.BatteryContractError("candidate_source_changed_during_acceptance")
    return ExecutionResult(
        results=results,
        worker_codes=worker_codes,
        dispatches=dispatches,
        candidate_files=candidate_files,
        candidate_pre_sha256=candidate_pre,
        candidate_sealed_sha256=candidate_sealed,
        candidate_post_sha256=candidate_post,
    )


def _summarize_pass(item: SealedPass, execution: ExecutionResult) -> dict[str, Any]:
    result = execution.results.get(item.key, {})
    result_valid = battery._validate_pass_result(result, item.cases)
    evidence_dir = item.context.evidence_path.parent
    pass_clear, pass_snapshot, pass_components, pass_full_hash = _read_reconciliation(
        evidence_dir / "pass-reconciliation.json",
        kind="pass",
    )
    tail_clear, tail_snapshot, tail_components, _tail_full_hash = _read_reconciliation(
        evidence_dir / "tail-reconciliation.json",
        kind="tail",
    )
    expected_combined = battery._sha256_bytes(
        battery._canonical_json_bytes(
            {
                "pass_reconciliation_sha256": pass_full_hash,
                "tail_reconciliation_sha256": tail_snapshot,
            }
        )
    )
    combined_clear = bool(
        pass_clear
        and tail_clear
        and result.get("pass_reconciliation_clear") is True
        and result.get("pass_reconciliation_sha256") == expected_combined
    )
    rows = result.get("case_results") if isinstance(result.get("case_results"), list) else []
    privacy_clear = bool(rows) and all(
        isinstance(row, Mapping) and row.get("privacy_canary_clear") is True for row in rows
    )
    pass_root = item.context.home.parent
    evidence_private = bool(
        item.context.evidence_path.is_file()
        and stat.S_IMODE(item.context.evidence_path.stat().st_mode) == 0o600
        and _private_tree(pass_root)
    )
    evidence_digest_match = bool(
        evidence_private
        and battery._is_sha256(result.get("evidence_sha256"))
        and result.get("evidence_sha256") == battery.file_sha256(item.context.evidence_path)
    )
    failure_codes = sorted(
        {
            str(code)
            for row in rows
            if isinstance(row, Mapping)
            for code in (row.get("failure_codes") or [])
            if isinstance(code, str)
        }
    )
    failed_case_ids = [
        str(row.get("case_id")) for row in rows if isinstance(row, Mapping) and row.get("passed") is False
    ]
    lifecycle_exact = bool(
        all(
            pass_components.get(key) is True
            for key in (
                "api_exact",
                "audit_exact",
                "counters_exact",
                "files_exact",
                "http_exact",
                "storage_exact",
                "tools_exact",
            )
        )
        and all(tail_components.get(key) is True for key in ("probe_exact", "files_exact", "database_exact"))
    )
    all_gates_exact = bool(
        result_valid
        and pass_clear
        and tail_clear
        and combined_clear
        and privacy_clear
        and evidence_digest_match
        and lifecycle_exact
        and not execution.worker_codes.get(item.key)
        and execution.dispatches.get(item.context.pass_id) == 1
    )
    return {
        "pass_id": item.context.pass_id,
        "cases": int(result.get("cases") or 0),
        "passed": int(result.get("passed") or 0),
        "failed": int(result.get("failed") or 0),
        "failed_case_ids": failed_case_ids,
        "failure_codes": failure_codes,
        "result_valid": result_valid,
        "pass_reconciliation_clear": pass_clear,
        "tail_reconciliation_clear": tail_clear,
        "combined_reconciliation_clear": combined_clear,
        "privacy_canaries_clear": privacy_clear,
        "evidence_private_and_bound": evidence_digest_match,
        "api_exact": bool(pass_components.get("api_exact")),
        "audit_exact": bool(pass_components.get("audit_exact")),
        "counters_exact": bool(pass_components.get("counters_exact")),
        "files_exact": bool(pass_components.get("files_exact")),
        "http_exact": bool(pass_components.get("http_exact")),
        "storage_exact": bool(pass_components.get("storage_exact")),
        "tools_exact": bool(pass_components.get("tools_exact")),
        "tail_probe_exact": bool(tail_components.get("probe_exact")),
        "tail_files_exact": bool(tail_components.get("files_exact")),
        "tail_database_exact": bool(tail_components.get("database_exact")),
        "all_gates_exact": all_gates_exact,
        "worker_error_code": str(execution.worker_codes.get(item.key) or ""),
        "runtime_sha256": str(result.get("runtime_hash") or ""),
        "evidence_sha256": str(result.get("evidence_sha256") or ""),
        "pass_snapshot_prefix": pass_snapshot[:12],
        "tail_snapshot_prefix": tail_snapshot[:12],
    }


def _runtime_identity(rows: Sequence[Mapping[str, Any]]) -> tuple[bool, str]:
    hashes = [str(row.get("runtime_sha256") or "") for row in rows]
    consistent = bool(hashes and all(battery._is_sha256(value) for value in hashes) and len(set(hashes)) == 1)
    return consistent, hashes[0] if consistent else ""


def _focused_summary(
    rows: Sequence[Mapping[str, Any]],
    execution: ExecutionResult,
    *,
    artifact_id: str,
) -> dict[str, Any]:
    focused = [
        row
        for row in rows
        if str(row.get("pass_id") or "") in {f"A-P{index:02d}" for index in FOCUSED_PASS_INDEXES}
    ]
    runtime_consistent, runtime_sha256 = _runtime_identity(focused)
    cases = sum(int(row.get("cases") or 0) for row in focused)
    passed = sum(int(row.get("passed") or 0) for row in focused)
    failed = sum(int(row.get("failed") or 0) for row in focused)
    green = bool(
        len(focused) == len(FOCUSED_PASS_INDEXES)
        and [row.get("pass_id") for row in focused] == [f"A-P{index:02d}" for index in FOCUSED_PASS_INDEXES]
        and cases == passed == 120
        and failed == 0
        and all(row.get("all_gates_exact") is True for row in focused)
        and runtime_consistent
        and execution.candidate_identity
    )
    return {
        "schema": FOCUSED_SCHEMA,
        "artifact_id": artifact_id,
        "status": "green" if green else "red",
        "passes_requested": len(FOCUSED_PASS_INDEXES),
        "passes_completed": len(focused),
        "cases": cases,
        "passed": passed,
        "failed": failed,
        "all_results_valid": all(row.get("result_valid") is True for row in focused),
        "all_pass_reconciliation_clear": all(row.get("pass_reconciliation_clear") is True for row in focused),
        "all_tail_reconciliation_clear": all(row.get("tail_reconciliation_clear") is True for row in focused),
        "all_combined_reconciliation_clear": all(
            row.get("combined_reconciliation_clear") is True for row in focused
        ),
        "privacy_canaries_clear": all(row.get("privacy_canaries_clear") is True for row in focused),
        "all_evidence_private_and_bound": all(
            row.get("evidence_private_and_bound") is True for row in focused
        ),
        "all_lifecycle_components_exact": all(row.get("all_gates_exact") is True for row in focused),
        "runtime_identity_consistent": runtime_consistent,
        "runtime_sha256": runtime_sha256,
        "candidate_digest_identity": execution.candidate_identity,
        "candidate_pre_sha256": execution.candidate_pre_sha256,
        "candidate_sealed_sha256": execution.candidate_sealed_sha256,
        "candidate_post_sha256": execution.candidate_post_sha256,
        "passes": focused,
    }


def _p06_summary(
    sealed: Sequence[SealedPass],
    rows: Sequence[Mapping[str, Any]],
    execution: ExecutionResult,
    *,
    artifact_id: str,
) -> dict[str, Any]:
    p06_ids = {"A-P06", "B-P06"}
    p06_rows = [row for row in rows if str(row.get("pass_id") or "") in p06_ids]
    rows_by_pass = {str(row.get("pass_id") or ""): row for row in p06_rows}
    exact_zero_expected = 0
    exact_zero_observed = 0
    control_expected = 0
    control_observed = 0
    tenant_control_cases_exact = 0
    for item in sealed:
        if item.context.pass_id not in p06_ids:
            continue
        result = execution.results.get(item.key, {})
        case_rows = result.get("case_results") if isinstance(result.get("case_results"), list) else []
        row_by_id = {str(row.get("case_id") or ""): row for row in case_rows if isinstance(row, Mapping)}
        for case in item.cases:
            equals = battery.oracle_for_case(case)["state"]["equals"]
            zero_keys = [key for key, value in equals.items() if type(value) is int and value == 0]
            control_keys = [
                key for key in equals if key.startswith("tenant_control_") and key != "tenant_control_exact"
            ]
            if (
                len(zero_keys) != 72
                or len(control_keys) != 44
                or equals.get("tenant_control_exact") is not True
            ):
                raise battery.BatteryContractError("p06_closed_oracle_shape_invalid")
            exact_zero_expected += len(zero_keys)
            control_expected += len(control_keys)
            row = row_by_id.get(case.id)
            if isinstance(row, Mapping) and row.get("passed") is True:
                exact_zero_observed += len(zero_keys)
                control_observed += len(control_keys)
                tenant_control_cases_exact += 1
    runtime_consistent, runtime_sha256 = _runtime_identity(p06_rows)
    cases = sum(int(row.get("cases") or 0) for row in p06_rows)
    passed = sum(int(row.get("passed") or 0) for row in p06_rows)
    failed = sum(int(row.get("failed") or 0) for row in p06_rows)
    ordered_rows = [rows_by_pass[pass_id] for pass_id in ("A-P06", "B-P06") if pass_id in rows_by_pass]
    green = bool(
        len(ordered_rows) == 2
        and cases == passed == 40
        and failed == 0
        and all(row.get("all_gates_exact") is True for row in ordered_rows)
        and exact_zero_expected == exact_zero_observed == 2880
        and control_expected == control_observed == 1760
        and tenant_control_cases_exact == 40
        and runtime_consistent
        and execution.candidate_identity
    )
    return {
        "schema": P06_SCHEMA,
        "artifact_id": artifact_id,
        "status": "green" if green else "red",
        "cases": cases,
        "passed": passed,
        "failed": failed,
        "exact_zero_expected": exact_zero_expected,
        "exact_zero_observed": exact_zero_observed,
        "tenant_control_fields_expected": control_expected,
        "tenant_control_fields_observed": control_observed,
        "tenant_control_cases_exact": tenant_control_cases_exact,
        "dispatches": {
            battery_id: execution.dispatches.get(f"{battery_id}-P06", 0) for battery_id in ("A", "B")
        },
        "runtime_identity_consistent": runtime_consistent,
        "runtime_sha256": runtime_sha256,
        "candidate_digest_identity": execution.candidate_identity,
        "candidate_pre_sha256": execution.candidate_pre_sha256,
        "candidate_sealed_sha256": execution.candidate_sealed_sha256,
        "candidate_post_sha256": execution.candidate_post_sha256,
        "passes": ordered_rows,
    }


def run_acceptance(
    suite: str,
    *,
    run_directory: Path,
    concurrency: int,
    artifact_id: str,
) -> tuple[int, dict[str, Any]]:
    """Run one suite under the host-wide readiness/worker lifecycle lock."""

    if not re.fullmatch(r"PRE-RELEASE-(?:ALL|P06|FOCUSED)-[0-9a-f]{16}", artifact_id):
        raise battery.BatteryContractError("artifact_id_invalid")
    _suite_keys(suite)
    if type(concurrency) is not int or not (1 <= concurrency <= battery.MAX_CONCURRENCY):
        raise battery.BatteryContractError("concurrency_out_of_range")
    if suite == "all" and concurrency != MODEL_READINESS_CONCURRENCY:
        raise battery.BatteryContractError("acceptance_execution_concurrency_invalid")
    with _TerminationSignalGuard(), _ExclusiveAcceptanceRun(_acceptance_lock_path()):
        return _run_acceptance_locked(
            suite,
            run_directory=run_directory,
            concurrency=concurrency,
            artifact_id=artifact_id,
        )


def _run_acceptance_locked(
    suite: str,
    *,
    run_directory: Path,
    concurrency: int,
    artifact_id: str,
) -> tuple[int, dict[str, Any]]:
    """Run one sealed suite and return only a closed aggregate."""

    if not re.fullmatch(r"PRE-RELEASE-(?:ALL|P06|FOCUSED)-[0-9a-f]{16}", artifact_id):
        raise battery.BatteryContractError("artifact_id_invalid")
    execution_concurrency_exact = concurrency == MODEL_READINESS_CONCURRENCY
    if suite == "all" and not execution_concurrency_exact:
        raise battery.BatteryContractError("acceptance_execution_concurrency_invalid")
    manifests = _load_manifests()
    inventory_for_suite(suite)
    model_environment = battery._inherit_model_environment()
    _assert_frozen_dispatcher_environment(model_environment)
    battery._assert_ignored_or_external(run_directory)
    if run_directory.exists():
        raise battery.BatteryContractError("run_directory_already_exists")
    _make_private_directory(run_directory)
    battery._preflight_private_filesystem(run_directory)
    sealed = _preseal_passes(suite, run_directory, manifests)
    readiness = _model_readiness_barrier(
        model_environment,
        require_authoritative_metrics=suite == "all",
    )
    readiness_required_clear = readiness.dispatch_clear if suite == "all" else readiness.probes_clear
    if not readiness_required_clear:
        raise battery.BatteryContractError("model_readiness_result_invalid")
    execution = _execute_sealed(
        sealed,
        concurrency=concurrency,
        model_environment=model_environment,
    )
    pass_rows_by_id = {item.context.pass_id: _summarize_pass(item, execution) for item in sealed}
    pass_rows = [pass_rows_by_id[item.context.pass_id] for item in sealed]
    suite_summaries: dict[str, dict[str, Any]] = {}
    if suite in {"focused", "all"}:
        suite_summaries["focused"] = _focused_summary(
            pass_rows,
            execution,
            artifact_id=artifact_id,
        )
        battery._secure_write_json(
            run_directory / "focused-sanitized-summary.json",
            suite_summaries["focused"],
        )
    if suite in {"p06", "all"}:
        suite_summaries["p06"] = _p06_summary(
            sealed,
            pass_rows,
            execution,
            artifact_id=artifact_id,
        )
        battery._secure_write_json(
            run_directory / "p06-sanitized-summary.json",
            suite_summaries["p06"],
        )
    runtime_consistent, runtime_sha256 = _runtime_identity(pass_rows)
    cases = sum(int(row.get("cases") or 0) for row in pass_rows)
    passed = sum(int(row.get("passed") or 0) for row in pass_rows)
    failed = sum(int(row.get("failed") or 0) for row in pass_rows)
    dispatches_exact = bool(
        set(execution.dispatches) == {item.context.pass_id for item in sealed}
        and all(value == 1 for value in execution.dispatches.values())
    )
    green = bool(
        set(suite_summaries) == ({"focused", "p06"} if suite == "all" else {suite})
        and all(summary.get("status") == "green" for summary in suite_summaries.values())
        and cases == passed == SUITE_CASE_COUNTS[suite]
        and failed == 0
        and all(row.get("all_gates_exact") is True for row in pass_rows)
        and execution.candidate_identity
        and runtime_consistent
        and dispatches_exact
        and (suite != "all" or execution_concurrency_exact)
        and readiness_required_clear
        and _private_tree(run_directory)
    )
    summary = {
        "schema": SUMMARY_SCHEMA,
        "artifact_id": artifact_id,
        "status": "green" if green else "red",
        "suite": suite,
        "passes": len(pass_rows),
        "cases": cases,
        "passed": passed,
        "failed": failed,
        "suite_status": {name: str(value.get("status") or "red") for name, value in suite_summaries.items()},
        "dispatches_exact_once": dispatches_exact,
        "model_readiness_dispatch_clear": readiness.dispatch_clear,
        "model_readiness_probes_clear": readiness.probes_clear,
        "model_readiness_queue_state": readiness.queue_state,
        "model_readiness_metrics_samples": readiness.metrics_samples,
        "model_readiness_concurrency": readiness.probes_requested,
        "model_readiness_probes_completed": readiness.probes_completed,
        "model_readiness_usable_responses": readiness.usable_responses,
        "model_readiness_maximum_latency_ms": readiness.maximum_latency_ms,
        "execution_concurrency": concurrency,
        "execution_concurrency_exact": execution_concurrency_exact,
        "privacy_evidence_private": _private_tree(run_directory),
        "candidate_digest_identity": execution.candidate_identity,
        "candidate_pre_sha256": execution.candidate_pre_sha256,
        "candidate_sealed_sha256": execution.candidate_sealed_sha256,
        "candidate_post_sha256": execution.candidate_post_sha256,
        "runtime_identity_consistent": runtime_consistent,
        "runtime_sha256": runtime_sha256,
        "runner_sha256": battery.file_sha256(RUNNER_PATH),
        "manifest_sha256": dict(battery.FROZEN_MANIFEST_SHA256),
    }
    battery._secure_write_json(run_directory / "pre-release-sanitized-summary.json", summary)
    if not _private_tree(run_directory):
        summary["status"] = "red"
        summary["privacy_evidence_private"] = False
        return 4, summary
    return (0 if green else 4), summary


def _default_run_directory(artifact_id: str) -> Path:
    return ROOT / "data" / "live-battery-runs" / artifact_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=tuple(SUITE_PASS_KEYS),
        default="all",
        help="Acceptance slice (default: all, one shared immutable snapshot)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=battery.DEFAULT_CONCURRENCY,
        help=f"Independent pass workers (1-{battery.MAX_CONCURRENCY})",
    )
    parser.add_argument(
        "--run-directory",
        type=Path,
        help="New ignored/external directory; existing paths are refused",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Private operator config for live execution; never written to evidence",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Validate manifests, inventory and candidate binding; run no model turns",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    if not (1 <= int(args.concurrency) <= battery.MAX_CONCURRENCY):
        raise SystemExit(f"--concurrency must be between 1 and {battery.MAX_CONCURRENCY}")
    if args.audit_only:
        try:
            audit = inventory_for_suite(str(args.suite))
        except Exception as exc:  # noqa: BLE001 - never print possibly private exception text
            print(
                json.dumps(
                    {
                        "schema": "friday.synthetic-live-battery.pre-release-audit.v1",
                        "valid": False,
                        "code": "pre_release_audit_failed",
                        "error_class_sha256": _sha256_text(type(exc).__name__),
                    },
                    sort_keys=True,
                )
            )
            return 2
        print(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    artifact_id = f"PRE-RELEASE-{str(args.suite).upper()}-{secrets.token_hex(8)}"
    run_directory = (
        args.run_directory.resolve()
        if args.run_directory is not None
        else _default_run_directory(artifact_id)
    )
    run_directory_existed = run_directory.exists()
    try:
        if args.env_file is not None:
            battery._select_live_env_file(args.env_file)
        return_code, summary = run_acceptance(
            str(args.suite),
            run_directory=run_directory,
            concurrency=int(args.concurrency),
            artifact_id=artifact_id,
        )
    except Exception as exc:  # noqa: BLE001 - raw detail stays in private evidence
        failure = {
            "schema": SUMMARY_SCHEMA,
            "artifact_id": artifact_id,
            "status": "red",
            "code": "pre_release_runner_failed",
            "error_class_sha256": _sha256_text(type(exc).__name__),
        }
        try:
            if not run_directory_existed and run_directory.is_dir() and _private_tree(run_directory):
                battery._secure_write_json(
                    run_directory / "pre-release-sanitized-failure.json",
                    failure,
                )
        except Exception:
            pass
        print(json.dumps(failure, sort_keys=True))
        return 4
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
