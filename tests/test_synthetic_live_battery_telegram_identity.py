"""Telegram marker identity through real rendering and the fake delivery boundary."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402

MARKER = "SYN-TELEGRAM-B10-16"
MODES = ("normal", "markup_fallback", "rate_limit")
DELIMITERS = (("**", "b"), ("__", "b"), ("*", "i"), ("_", "i"))
EXACT_KEYS = (
    "transport_delivered_once",
    "transport_source_exact",
    "transport_render_exact",
    "transport_delivery_marker_exact",
    "transport_delivery_shape_exact",
    "transport_endpoint_exact",
    "transport_request_kwargs_exact",
    "transport_retry_sequence_exact",
    "rendered_html_safe",
)


def _case(index: int = 16) -> battery.ExpandedCase:
    cases = battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS["B"]))
    return next(case for case in cases if case.pass_index == 10 and case.question_index == index)


def _evaluate(message: str, state: dict) -> dict:
    from test_synthetic_live_battery import _satisfying_record

    case = _case()
    record = _satisfying_record(case)
    record["response"]["message"] = message
    record["raw_response"] = json.dumps(record["response"], ensure_ascii=False)
    record["state"].update(state)
    return battery.evaluate_case(case, record, latency_ms=1)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("delimiter", "tag"), DELIMITERS)
def test_four_delimiters_reach_exact_fake_delivery(
    delimiter: str, tag: str, mode: str, tmp_path: Path, record_property
) -> None:
    from friday.telegram_bridge._markup import to_telegram_html

    source = f"{delimiter}готово {MARKER}{delimiter}"
    rendered = f"<{tag}>готово {MARKER}</{tag}>"
    state = battery._telegram_transport_probe(source, mode=mode, home=tmp_path)
    # B10-16's frozen evaluation mode is normal; the other modes exercise
    # the same actual bridge and delivery oracle without changing its corpus.
    verdict = _evaluate(source, state) if mode == "normal" else None
    record_property("source", source)
    record_property("rendered", to_telegram_html(source))
    record_property("transport", json.dumps(state, sort_keys=True))
    record_property("verdict", json.dumps(verdict, sort_keys=True))

    assert battery._telegram_shape_matches(_case(), source) is True
    assert to_telegram_html(source) == rendered
    assert all(state[key] is True for key in EXACT_KEYS), state
    assert state["transport_attempt_count"] == (1 if mode == "normal" else 2)
    assert state["transport_source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    delivered = {"chat_id": 5001, "text": rendered, "parse_mode": "HTML", "disable_web_page_preview": True}
    if mode == "markup_fallback":
        delivered.pop("parse_mode")
        delivered["text"] = source
    assert (
        state["transport_delivery_sha256"]
        == hashlib.sha256(battery._canonical_json_bytes([delivered])).hexdigest()
    )
    assert battery._closed_marker_exact(source, MARKER, kind="TELEGRAM") is True
    if verdict is not None:
        assert verdict["passed"] is True, verdict["failure_codes"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("delimiter", "index"), (("**", 2), ("__", 2), ("*", 4), ("_", 4)))
def test_delimiters_touching_both_marker_edges_are_identity_neutral(
    delimiter: str, index: int, mode: str, tmp_path: Path
) -> None:
    marker = f"SYN-TELEGRAM-B10-{index:02d}"
    source = f"{delimiter}{marker}{delimiter}"
    state = battery._telegram_transport_probe(source, mode=mode, home=tmp_path)

    assert battery._telegram_shape_matches(_case(index), source) is True
    assert battery._closed_marker_exact(source, marker, kind="TELEGRAM") is True
    assert all(state[key] is True for key in EXACT_KEYS), state


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "value",
    (
        f"{MARKER}_EXTRA",
        f"X{MARKER}",
        f"{MARKER}X",
        f"Я{MARKER}",
        f"{MARKER}я",
        f"prefix-{MARKER}",
        f"{MARKER}-suffix",
        f"{MARKER}_",
        f"{MARKER} {MARKER}",
        f"{MARKER} SYN-TELEGRAM-A10-16",
        f"{MARKER} {MARKER}_EXTRA",
        f"{MARKER} X{MARKER}",
        f"{MARKER} {MARKER.lower()}",
        "missing",
    ),
)
def test_source_marker_identity_rejects_attached_or_duplicate_tokens(
    value: str, mode: str, tmp_path: Path
) -> None:
    source = f"__готово {value}__"
    state = battery._telegram_transport_probe(source, mode=mode, home=tmp_path)

    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is False
    assert state["transport_delivery_shape_exact"] is False
    assert battery._closed_marker_exact(source, MARKER, kind="TELEGRAM") is False
    assert _evaluate(source, state)["passed"] is False


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("delimiter", ("**", "__", "*", "_"))
def test_marker_outside_requested_span_remains_a_shape_failure(
    delimiter: str, mode: str, tmp_path: Path
) -> None:
    source = f"{delimiter}готово{delimiter} {MARKER}"
    state = battery._telegram_transport_probe(source, mode=mode, home=tmp_path)

    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_shape_exact"] is False
    assert battery._telegram_shape_matches(_case(), source) is False
    assert _evaluate(source, state)["passed"] is False


@pytest.mark.parametrize(
    ("rendered", "marker_exact"),
    (
        (f'<b title="{MARKER}">готово</b>', False),
        (f'<a href="https://example.invalid/{MARKER}">готово</a>', False),
        (f"<b>готово</b> {MARKER}", True),
        (f"<b>готово {MARKER} {MARKER}</b>", False),
        (f"<b>готово {MARKER}_EXTRA</b>", False),
        (f"<b>готово X{MARKER}</b>", False),
        (f"<b>готово {MARKER}X</b>", False),
        ("<b>готово SYN-TELEGRAM-B10-15</b>", False),
        (f"<b>лишнее готово {MARKER}</b>", True),
        (f"<i>готово {MARKER}</i>", True),
        (f"<script>готово {MARKER}</script>", True),
    ),
)
@pytest.mark.parametrize("mode", MODES)
def test_agreeing_corrupt_renderers_cannot_forge_visible_identity_or_requested_shape(
    rendered: str, marker_exact: bool, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import friday.telegram_bridge._markup as markup
    import friday.telegram_bridge._transport as transport

    monkeypatch.setattr(markup, "to_telegram_html", lambda _source: rendered)
    monkeypatch.setattr(transport, "to_telegram_html", lambda _source: rendered)
    source = f"__готово {MARKER}__"
    state = battery._telegram_transport_probe(source, mode=mode, home=tmp_path)

    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is (True if mode == "markup_fallback" else marker_exact)
    assert state["transport_delivery_shape_exact"] is False
    assert _evaluate(source, state)["passed"] is False


@pytest.mark.parametrize("mode", MODES)
def test_source_visible_equivalence_still_checks_the_actual_delivery(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from friday.telegram_bridge._transport import TransportMixin

    original = TransportMixin._post_message_chunk

    async def corrupt(self, client, payload, chunk):
        return await original(self, client, {**payload, "text": payload["text"] + " extra"}, chunk + " extra")

    monkeypatch.setattr(TransportMixin, "_post_message_chunk", corrupt)
    state = battery._telegram_transport_probe(f"__готово {MARKER}__", mode=mode, home=tmp_path)

    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_render_exact"] is False
    assert state["transport_retry_sequence_exact"] is False
    assert state["transport_delivery_shape_exact"] is False


@pytest.mark.parametrize("mode", MODES)
def test_duplicate_actual_delivery_still_fails(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from friday.telegram_bridge._transport import TransportMixin

    original = TransportMixin._post_message_chunk

    async def duplicate(self, client, payload, chunk):
        await original(self, client, payload, chunk)
        return await original(self, client, payload, chunk)

    monkeypatch.setattr(TransportMixin, "_post_message_chunk", duplicate)
    state = battery._telegram_transport_probe(f"_готово {MARKER}_", mode=mode, home=tmp_path)

    assert state["transport_delivered_once"] is False
    assert state["transport_delivery_marker_exact"] is False
    assert state["transport_delivery_shape_exact"] is False
    assert state["transport_render_exact"] is False
    assert state["transport_retry_sequence_exact"] is False


@pytest.mark.parametrize("mutation", ("endpoint", "kwargs", "chat", "preview", "parse_mode", "payload_key"))
def test_underscore_delivery_keeps_endpoint_and_payload_constraints(
    mutation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from friday.telegram_bridge._transport import TransportMixin

    async def corrupt(self, client, payload, chunk):
        endpoint = "https://api.telegram.org/bot123:synthetic-live-battery-token/sendMessage"
        payload = dict(payload)
        kwargs = {}
        if mutation == "endpoint":
            endpoint += "/extra"
        elif mutation == "kwargs":
            kwargs["timeout"] = 0.01
        elif mutation == "chat":
            payload["chat_id"] = 5002
        elif mutation == "preview":
            payload["disable_web_page_preview"] = False
        elif mutation == "parse_mode":
            payload.pop("parse_mode")
        else:
            payload["extra"] = True
        return await client.post(endpoint, json=payload, **kwargs)

    monkeypatch.setattr(TransportMixin, "_post_message_chunk", corrupt)
    source = f"__готово {MARKER}__"
    state = battery._telegram_transport_probe(source, mode="normal", home=tmp_path)
    failed_key = {
        "endpoint": "transport_endpoint_exact",
        "kwargs": "transport_request_kwargs_exact",
    }.get(mutation, "transport_delivery_shape_exact")
    assert state[failed_key] is False
    assert _evaluate(source, state)["passed"] is False


def test_marker_cardinality_override_remains_explicit() -> None:
    source = f"__готово {MARKER} {MARKER}__"
    assert battery._closed_marker_exact(source, MARKER, kind="TELEGRAM") is False
    assert battery._closed_marker_exact(source, MARKER, kind="TELEGRAM", exact_once=False) is True


@pytest.mark.parametrize("mode", ("rate_limit", "markup_fallback"))
def test_second_attempt_cannot_change_the_delivered_text(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from friday.telegram_bridge._transport import TransportMixin

    original = TransportMixin._post_message_chunk

    async def corrupt_retry(self, client, payload, chunk):
        post = client.post
        attempts = 0

        async def corrupt_post(url, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                kwargs["json"] = {**kwargs["json"], "text": kwargs["json"]["text"] + " extra"}
            return await post(url, **kwargs)

        client.post = corrupt_post
        return await original(self, client, payload, chunk)

    monkeypatch.setattr(TransportMixin, "_post_message_chunk", corrupt_retry)
    source = f"__готово {MARKER}__"
    state = battery._telegram_transport_probe(source, mode=mode, home=tmp_path)

    assert state["transport_attempt_count"] == 2
    assert state["transport_delivered_once"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_render_exact"] is False
    assert state["transport_retry_sequence_exact"] is False
    assert state["transport_delivery_shape_exact"] is False
