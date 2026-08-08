"""Independent P10 transport oracles; no model or public network is contacted."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


def _case(battery_id: str, index: int) -> battery.ExpandedCase:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS[battery_id])
    cases = battery.expand_manifest_cases(manifest)
    return next(case for case in cases if case.pass_index == 10 and case.question_index == index)


def _patch_both_renderers(monkeypatch, renderer) -> None:  # noqa: ANN001
    import friday.telegram_bridge._markup as markup
    import friday.telegram_bridge._transport as transport

    monkeypatch.setattr(markup, "to_telegram_html", renderer)
    monkeypatch.setattr(transport, "to_telegram_html", renderer)


def test_p10_real_renderer_preserves_visible_text_and_requested_style() -> None:
    from friday.telegram_bridge._markup import to_telegram_html

    examples = [
        ("A", 1, "- SYN-TELEGRAM-A10-01\n- второй"),
        ("A", 2, "**SYN-TELEGRAM-A10-02**"),
        ("A", 4, "*синтетика* SYN-TELEGRAM-A10-04"),
        ("A", 6, "### Заголовок\n- SYN-TELEGRAM-A10-06"),
        ("A", 7, "SYN-TELEGRAM-A10-07 <safe> & тест"),
        ("A", 8, "> Короткая цитата SYN-TELEGRAM-A10-08"),
        ("A", 16, "**готово** SYN-TELEGRAM-A10-16"),
        ("B", 18, "> SYN-TELEGRAM-B10-18\nпояснение"),
        ("B", 20, "SYN-TELEGRAM-B10-20"),
    ]
    for battery_id, index, source in examples:
        assert battery._telegram_p10_content_equivalent(
            source,
            to_telegram_html(source),
            battery_id=battery_id,
            index=index,
        )


def test_p10_rejects_same_marker_and_style_with_changed_visible_content(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    case = _case("A", 2)
    marker = battery._marker(case, "TELEGRAM")
    _patch_both_renderers(monkeypatch, lambda _source: f"<b>{marker}</b> лишний текст")

    state = battery._telegram_transport_probe(f"**{marker}**", mode="normal", home=tmp_path)

    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is False


@pytest.mark.parametrize(
    "rendered",
    [
        '<a href="https://example.invalid/">SYN-TELEGRAM-B10-20</a>',
        "<strong>SYN-TELEGRAM-B10-20</strong>",
    ],
)
def test_p10_rejects_unrequested_anchor_or_tag(rendered: str, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _patch_both_renderers(monkeypatch, lambda _source: rendered)

    state = battery._telegram_transport_probe("SYN-TELEGRAM-B10-20", mode="normal", home=tmp_path)

    assert state["transport_delivery_marker_exact"] is True
    assert state["rendered_html_safe"] is True
    assert state["transport_delivery_shape_exact"] is False


def test_p10_rejects_changed_visible_angle_literal(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    marker = "SYN-TELEGRAM-A10-07"
    _patch_both_renderers(monkeypatch, lambda _source: f"{marker} &lt;wrong&gt;")

    state = battery._telegram_transport_probe(f"{marker} <safe>", mode="normal", home=tmp_path)

    assert state["transport_delivery_marker_exact"] is True
    assert state["rendered_html_safe"] is True
    assert state["transport_delivery_shape_exact"] is False


def test_probe_requires_exact_endpoint_and_only_json_kwarg(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from friday.telegram_bridge._transport import TransportMixin

    async def corrupted_post(self, client, payload, chunk):  # noqa: ANN001, ANN202, ARG001
        return await client.post(
            "https://api.telegram.org/bot123:synthetic-live-battery-token/sendMessage/extra",
            json=payload,
            timeout=0.01,
        )

    monkeypatch.setattr(TransportMixin, "_post_message_chunk", corrupted_post)
    state = battery._telegram_transport_probe("SYN-TELEGRAM-B10-20", mode="normal", home=tmp_path)

    assert state["transport_endpoint_exact"] is False
    assert state["transport_request_kwargs_exact"] is False


@pytest.mark.parametrize(
    "message,mode",
    [
        ("- SYN-TELEGRAM-B10-01", "normal"),
        ("**SYN-TELEGRAM-B10-02**", "rate_limit"),
        ("1. SYN-TELEGRAM-B10-03\n2. второй", "markup_fallback"),
    ],
)
def test_probe_accepts_exact_endpoint_and_request_kwargs(message: str, mode: str, tmp_path: Path) -> None:
    state = battery._telegram_transport_probe(message, mode=mode, home=tmp_path)

    assert state["transport_endpoint_exact"] is True
    assert state["transport_request_kwargs_exact"] is True
    assert state["transport_delivery_shape_exact"] is True


def test_probe_does_not_replace_process_global_asyncio_sleep() -> None:
    source = inspect.getsource(battery._telegram_transport_probe)

    assert "asyncio.sleep =" not in source
    assert "transport_module.asyncio.sleep" not in source
