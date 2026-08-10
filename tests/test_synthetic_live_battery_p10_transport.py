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


def test_p10_sentence_initial_target_is_repaired_before_delivery(tmp_path: Path) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 16)
    marker = battery._marker(case, "TELEGRAM")
    source = f"Готово — {marker}."

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="normal", home=tmp_path)

    assert repaired == f"**Готово** — {marker}."
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_word_list_metadata_suffix_is_removed_before_delivery(tmp_path: Path) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 12)
    marker = battery._marker(case, "TELEGRAM")
    source = f"- alpha1\n- beta2\n- gamma3\n\nMarker: {marker}\nControl: {case.id}"

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="markup_fallback", home=tmp_path)

    assert repaired == f"- alpha1\n- beta2\n- {marker}"
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["transport_retry_sequence_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_request_bound_paired_metadata_suffix_keeps_values_and_drops_metadata(
    tmp_path: Path,
) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 12)
    marker = battery._marker(case, "TELEGRAM")
    source = f"- alpha1\n- beta2\n- gamma3\n\nМаркер: {marker}, контроль {case.id}"

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="markup_fallback", home=tmp_path)

    assert repaired == f"- alpha1\n- beta2\n- {marker}"
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["transport_retry_sequence_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_inline_marker_row_drops_parser_metadata_before_delivery(tmp_path: Path) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 12)
    marker = battery._marker(case, "TELEGRAM")
    source = f"- слово1\n- слово2\n- слово3 — маркер {marker}, контроль {case.id}"

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="markup_fallback", home=tmp_path)

    assert repaired == f"- слово1\n- слово2\n- {marker}"
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["transport_retry_sequence_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_bracketed_inline_marker_metadata_is_removed_before_delivery(tmp_path: Path) -> None:
    from friday.text_shape import TEXT_SHAPE_VALID, explicit_text_shape_status, repair_explicit_text_shape

    case = _case("A", 12)
    marker = battery._marker(case, "TELEGRAM")
    source = f"- alpha1\n- beta2\n- Маркер [{marker}]. Контроль {case.id}"
    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="markup_fallback", home=tmp_path)

    assert repaired == f"- alpha1\n- beta2\n- {marker}"
    assert explicit_text_shape_status(case.question, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["transport_retry_sequence_exact"] is True
    assert state["rendered_html_safe"] is True


@pytest.mark.parametrize(
    "marker_row",
    [
        "Маркер [{marker}]. Контроль OTHER-A10-12",
        "Маркер [{marker}]. Контроль syn-a10-12",
        "Маркер [X{marker}Y]. Контроль SYN-A10-12",
        "Статус [{marker}]. Контроль SYN-A10-12",
        "Маркер [[{marker}]]. Контроль SYN-A10-12",
    ],
)
def test_p10_ambiguous_bracketed_marker_metadata_is_not_projected(marker_row: str) -> None:
    from friday.text_shape import TEXT_SHAPE_INVALID, explicit_text_shape_status, repair_explicit_text_shape

    case = _case("A", 12)
    marker = battery._marker(case, "TELEGRAM")
    source = f"- alpha1\n- beta2\n- {marker_row.format(marker=marker)}"

    assert repair_explicit_text_shape(case.question, source) == source
    assert (
        repair_explicit_text_shape(case.question, repair_explicit_text_shape(case.question, source)) == source
    )
    assert explicit_text_shape_status(case.question, source) == TEXT_SHAPE_INVALID


@pytest.mark.parametrize("index", [15, 20])
def test_p10_terminal_marker_item_is_merged_before_delivery(index: int, tmp_path: Path) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", index)
    marker = battery._marker(case, "TELEGRAM")
    source = f"- первый синтетический пункт\n- второй синтетический пункт\n- Маркер: {marker}"
    expected = f"- первый синтетический пункт\n- второй синтетический пункт Маркер: {marker}"

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="normal", home=tmp_path)

    assert repaired == expected
    assert repaired.splitlines()[0] == source.splitlines()[0]
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_missing_marker_is_appended_to_the_request_bound_control_row(
    tmp_path: Path,
) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 20)
    marker = battery._marker(case, "TELEGRAM")
    source = (
        f"- первый синтетический пункт: проверка готова\n- второй синтетический пункт: контроль {case.id}"
    )
    expected = f"{source} {marker}"

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="rate_limit", home=tmp_path)

    assert repaired == expected
    assert repaired[: len(source)] == source
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["transport_retry_sequence_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_empty_quote_separator_is_closed_before_delivery(tmp_path: Path) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 18)
    marker = battery._marker(case, "TELEGRAM")
    source = f"> Короткая цитата\n>\n> Пояснение готово {marker}"
    expected = f"> Короткая цитата\nПояснение готово {marker}"

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="rate_limit", home=tmp_path)

    assert repaired == expected
    assert repaired.splitlines()[0] == source.splitlines()[0]
    assert repaired.splitlines()[1] == source.splitlines()[2][2:]
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["transport_retry_sequence_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_inline_quote_tail_is_split_before_delivery_without_losing_bytes(
    tmp_path: Path,
) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 18)
    marker = battery._marker(case, "TELEGRAM")
    source = f"> «Короткая цитата.» Пояснение {marker}. Контроль {case.id}."
    expected = f"> «Короткая цитата.»\nПояснение {marker}."

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="markup_fallback", home=tmp_path)

    assert repaired == expected
    assert repaired.replace("\n", " ", 1) == f"> «Короткая цитата.» Пояснение {marker}."
    assert case.id not in repaired
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["transport_retry_sequence_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_bullet_colon_prefix_is_repaired_before_delivery(tmp_path: Path) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 1)
    marker = battery._marker(case, "TELEGRAM")
    source = f"- {marker}: первый синтетический пункт\n-: второй синтетический пункт"
    expected = f"- {marker}: первый синтетический пункт\n- второй синтетический пункт"

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="normal", home=tmp_path)

    assert battery._telegram_shape_matches(case, source) is False
    assert repaired == expected
    assert repaired.splitlines()[0] == source.splitlines()[0]
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["rendered_html_safe"] is True


def test_p10_missing_ampersand_carrier_is_repaired_before_delivery(tmp_path: Path) -> None:
    from friday.text_shape import repair_explicit_text_shape

    case = _case("A", 11)
    marker = battery._marker(case, "TELEGRAM")
    source = f"Левая часть < 5 и правая > 3. {marker}"
    expected = f"Левая часть < 5 & правая > 3. {marker}"

    repaired = repair_explicit_text_shape(case.question, source)
    state = battery._telegram_transport_probe(repaired, mode="rate_limit", home=tmp_path)

    assert repaired == expected
    assert repair_explicit_text_shape(case.question, repaired) == repaired
    assert battery._telegram_shape_matches(case, repaired) is True
    assert state["transport_render_exact"] is True
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True
    assert state["transport_retry_sequence_exact"] is True
    assert state["rendered_html_safe"] is True


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


def test_p10_source_rejects_unrequested_bold_in_a_plain_list() -> None:
    case = _case("A", 1)
    message = "- **SYN-TELEGRAM-A10-01**\n- второй"

    assert battery._telegram_shape_matches(case, message) is False


@pytest.mark.parametrize(
    ("index", "message"),
    [
        (2, "**SYN-TELEGRAM-A10-02**"),
        (4, "*синтетика* SYN-TELEGRAM-A10-04"),
        (18, "> SYN-TELEGRAM-A10-18\nпояснение"),
    ],
)
def test_p10_source_accepts_the_exact_requested_style(index: int, message: str) -> None:
    assert battery._telegram_shape_matches(_case("A", index), message) is True


def test_p10_source_accepts_a_plain_list_without_inline_style() -> None:
    case = _case("A", 1)
    message = "- SYN-TELEGRAM-A10-01\n- второй"

    assert battery._telegram_shape_matches(case, message) is True


def test_a07_source_and_delivery_reject_multiple_terminal_sentences(tmp_path: Path) -> None:
    message = "Значение 5 больше 3, но меньше 10. <SYN-TELEGRAM-A10-07>. Контроль SYN-A10-07."
    case = _case("A", 7)

    assert battery._telegram_shape_matches(case, message) is False
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)

    assert state["transport_delivery_shape_exact"] is False


@pytest.mark.parametrize(
    "message",
    [
        "«Первая фраза.» «Вторая фраза.» <SYN-TELEGRAM-A10-07>",
        "«(Первая фраза.)» (Вторая фраза.) <SYN-TELEGRAM-A10-07>",
    ],
)
def test_a07_source_and_delivery_count_boundaries_before_closing_delimiters(
    message: str,
    tmp_path: Path,
) -> None:
    case = _case("A", 7)

    assert battery._telegram_shape_matches(case, message) is False
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)

    assert state["transport_delivery_shape_exact"] is False


@pytest.mark.parametrize(
    "message",
    [
        "Значение 5 больше 3, но меньше 10; <SYN-TELEGRAM-A10-07>; контроль SYN-A10-07.",
        "Версия build.v1 равна 3.14; <SYN-TELEGRAM-A10-07>.",
        "Версия build.v1 равна 3.14; <SYN-TELEGRAM-A10-07>",
        "Результат, т. е. i.e. итог, готов; <SYN-TELEGRAM-A10-07>.",
    ],
)
def test_a07_source_and_delivery_accept_at_most_one_terminal_sentence(
    message: str,
    tmp_path: Path,
) -> None:
    case = _case("A", 7)

    assert battery._telegram_shape_matches(case, message) is True
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)

    assert state["transport_delivery_shape_exact"] is True


@pytest.mark.parametrize(
    ("battery_id", "index", "message"),
    [
        ("B", 9, "1. SYN-TELEGRAM-B10-09\n2. второй"),
        ("A", 12, "1. SYN-TELEGRAM-A10-12\n2. второе\n3. три"),
        ("B", 15, "1. SYN-TELEGRAM-B10-15\n2. второй"),
        ("A", 20, "1. SYN-TELEGRAM-A10-20\n2. второй"),
    ],
)
def test_p10_delivery_accepts_numbered_list_when_source_oracle_allows_it(
    battery_id: str,
    index: int,
    message: str,
    tmp_path: Path,
) -> None:
    case = _case(battery_id, index)

    assert battery._telegram_shape_matches(case, message) is True
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)

    assert state["transport_delivery_shape_exact"] is True


@pytest.mark.parametrize(
    ("index", "message"),
    [
        (12, "1. SYN-TELEGRAM-A10-12\n2. SYN-A10-12\n3. три\nлишнее продолжение"),
        (20, "1. SYN-TELEGRAM-A10-20\n2. второй\nлишнее продолжение"),
    ],
)
def test_p10_delivery_rejects_an_orphan_line_after_an_exact_list(
    index: int,
    message: str,
    tmp_path: Path,
) -> None:
    case = _case("A", index)

    assert battery._telegram_shape_matches(case, message) is False
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)

    assert state["transport_delivery_shape_exact"] is False


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "- Тестовое слово 1\n- Тестовое слово 2\n- Тестовое слово 3 (маркер SYN-TELEGRAM-A10-12)",
            False,
        ),
        ("- SYN-TELEGRAM-A10-12\n- второе\n- третье", True),
        ("- SYN-TELEGRAM-A10-12\n- SYN-A10-12\n- третье", False),
        ("- SYN-TELEGRAM-A10-12\n- SYN-A10-12\n- XSYN-A10-12Y", False),
        ("- SYN-TELEGRAM-A10-12\n- SYN-A10-12\n- Xsyn-telegram-a10-12Y", False),
    ],
)
def test_p10_delivery_matches_the_source_three_word_list_contract(
    message: str,
    expected: bool,
    tmp_path: Path,
) -> None:
    case = _case("A", 12)

    assert battery._telegram_shape_matches(case, message) is expected
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)

    assert state["transport_delivery_shape_exact"] is expected


@pytest.mark.parametrize(
    "message",
    [
        "- SYN-TELEGRAM-A10-12\n- повтор\n- повтор",
        "- SYN-TELEGRAM-A10-12\n- два слова\n- третье",
        "- SYN-TELEGRAM-A10-12\n- второе\n- SYN-TELEGRAM-A10-12",
        "- первое\n- второе\n- третье",
        "- SYN-TELEGRAM-A10-12\n- второе\n- третье\n- четвёртое",
        "- SYN-TELEGRAM-A10-12\n- второе\n- третье Control: SYN-A10-12",
    ],
)
def test_p10_three_word_list_rejects_bounded_contract_violations(
    message: str,
    tmp_path: Path,
) -> None:
    case = _case("A", 12)

    assert battery._telegram_shape_matches(case, message) is False
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)

    assert state["transport_delivery_shape_exact"] is False


@pytest.mark.parametrize(
    ("index", "message"),
    [
        (
            8,
            "> Короткая цитата SYN-TELEGRAM-A10-08\n> Ещё одна короткая строка",
        ),
        (
            13,
            "Нейтральная Markdown-фраза SYN-TELEGRAM-A10-13\nКороткое продолжение",
        ),
        (
            19,
            "Безопасный Markdown SYN-TELEGRAM-A10-19\nБез внешней ссылки",
        ),
    ],
)
def test_p10_source_and_delivery_accept_naturally_compact_two_line_a_shapes(
    index: int,
    message: str,
    tmp_path: Path,
) -> None:
    case = _case("A", index)

    assert battery._telegram_shape_matches(case, message) is True
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)

    assert state["transport_delivery_shape_exact"] is True


@pytest.mark.parametrize(
    ("index", "message"),
    [
        (8, "> SYN-TELEGRAM-A10-08\nне цитата"),
        (13, "SYN-TELEGRAM-A10-13\nдва\nтри"),
        (19, "SYN-TELEGRAM-A10-19\nдва\nтри"),
    ],
)
def test_p10_compact_shape_relaxation_remains_bounded(index: int, message: str) -> None:
    assert battery._telegram_shape_matches(_case("A", index), message) is False


def test_p10_telegram_marker_cardinality_is_not_a_factual_polarity_claim() -> None:
    case = _case("A", 14)
    marker = battery._marker(case, "TELEGRAM")

    assert (
        battery._closed_marker_exact(
            f"{marker} передан без ошибок.",
            marker,
            kind="TELEGRAM",
        )
        is True
    )


@pytest.mark.parametrize(
    "message_factory",
    [
        lambda marker, _foreign: f"{marker} {marker}",
        lambda marker, foreign: f"{marker} {foreign}",
        lambda marker, _foreign: f"X{marker}Y",
    ],
)
def test_p10_telegram_marker_identity_and_cardinality_remain_closed(message_factory) -> None:  # noqa: ANN001
    case = _case("A", 14)
    marker = battery._marker(case, "TELEGRAM")
    foreign = battery._marker(_case("A", 13), "TELEGRAM")

    assert (
        battery._closed_marker_exact(
            message_factory(marker, foreign),
            marker,
            kind="TELEGRAM",
        )
        is False
    )


def test_p10_compact_markdown_allows_one_substantive_word_but_not_an_empty_marker() -> None:
    case = _case("A", 19)
    marker = battery._marker(case, "TELEGRAM")

    assert battery._telegram_shape_matches(case, f"Готово {marker}") is True
    assert battery._telegram_shape_matches(case, marker) is False


def test_probe_does_not_replace_process_global_asyncio_sleep() -> None:
    source = inspect.getsource(battery._telegram_transport_probe)

    assert "asyncio.sleep =" not in source
    assert "transport_module.asyncio.sleep" not in source
