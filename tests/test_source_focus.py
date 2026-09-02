from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict

import pytest

import friday.retrieval.source_focus as source_focus_module
from friday.retrieval.source_focus import (
    SourceFocusMatchKind,
    project_source_focus,
    source_focus_fts_tokens,
)


@pytest.mark.parametrize("surname", ["Иванов", "Иванова", "Иванову", "Ивановым"])
def test_projection_preserves_closed_surname_morphology_and_exact_offsets(surname: str) -> None:
    body = f"\n\t{surname}\nДолжность: ведущий инженер\n"

    result = project_source_focus(body, "иванов", "иванов должност", max_chars=600)

    assert result is not None
    assert result.excerpt == f"{surname}\nДолжность: ведущий инженер"
    assert body[result.start : result.end] == result.excerpt
    assert result.matched_focus_count == 2
    assert result.context_count >= 2
    assert result.focus_match_kind is SourceFocusMatchKind.FULL


@pytest.mark.parametrize(
    ("query", "stored"),
    [
        ("Иванова", "Иванов"),
        ("Иванову", "Иванов"),
        ("Артемьева", "Артемьев"),
        ("Петровского", "Петровский"),
        ("Петровской", "Петровская"),
    ],
)
def test_inflected_query_surname_matches_closed_nominative_record(query: str, stored: str) -> None:
    body = f"{stored}\nДолжность: ведущий инженер"

    result = project_source_focus(body, query, "должность", max_chars=600)

    assert result is not None
    assert result.excerpt == body


@pytest.mark.parametrize("collision", ["Ивановский", "Иванович"])
def test_projection_rejects_surname_prefix_collisions(collision: str) -> None:
    assert (
        project_source_focus(
            f"{collision}\nДолжность: ведущий инженер",
            "иванов",
            "иванов должност",
            max_chars=600,
        )
        is None
    )


@pytest.mark.parametrize(
    ("focus", "predicate"),
    [
        ("иванов рол", "Пароль: PRIVATE-VALUE"),
        ("иванов рол", "Контроль: PRIVATE-VALUE"),
        ("иванов позици", "Позиционирование продукта"),
        ("иванов должност", "Должностная инструкция"),
    ],
)
def test_focus_uses_closed_forms_not_unrelated_substrings(focus: str, predicate: str) -> None:
    result = project_source_focus(
        f"Иванов — ведущий инженер\n{predicate}",
        "иванов",
        focus,
        max_chars=600,
    )

    assert result is not None
    assert result.matched_focus_count == 1
    assert result.focus_match_kind is SourceFocusMatchKind.ANCHOR_CONTEXT


def test_predicate_only_material_cannot_admit_a_projection() -> None:
    result = project_source_focus(
        "Должность: посторонний предикат без искомой фамилии",
        "Иванов",
        "Иванов должность",
        max_chars=600,
    )

    assert result is None


def test_far_section_predicate_is_never_joined_to_the_anchor() -> None:
    body = (
        "Иванов\n"
        + ("нейтральный раздел без кадровых сведений\n" * 30)
        + "Петров\nДолжность: генеральный директор\n"
    )

    result = project_source_focus(body, "Иванов", "Иванов должность", max_chars=600)

    assert result is None


def test_adjacent_field_is_bound_but_a_neighbour_record_is_not() -> None:
    safe = "Должность: ведущий инженер\nИванов"
    result = project_source_focus(safe, "иванов", "иванов должност", max_chars=600)
    assert result is not None
    assert result.excerpt == safe
    assert safe[result.start : result.end] == result.excerpt

    hostile = "Петров\nДолжность: генеральный директор\nИванов"
    assert project_source_focus(hostile, "иванов", "иванов должност", max_chars=600) is None

    for hostile in (
        "Иванов\nПетров Должность: генеральный директор",
        "Петров Должность: генеральный директор\nИванов",
        "Соседняя запись\nДолжность: Петров директор\nИванов",
        "Иванов\nДолжность: Артемьев генеральный директор\nСоседняя запись",
    ):
        assert project_source_focus(hostile, "иванов", "должность", max_chars=600) is None


@pytest.mark.parametrize(
    "foreign_name",
    [
        "Путин",
        "Орлов",
        "Ленин",
        "Смит",
        "Smith",
        "путин",
        "орлов",
        "ленин",
        "смит",
        "smith",
        "мария",
        "Smith-Jones",
        "Путин-младший",
        "J.Smith",
        "user_123",
        "Мария-Анна",
        "李雷",
        "Πούτιν",
        "محمد",
        "ＰＵＴＩＮ",
        "Ⓢⓜⓘⓣⓗ",
    ],
)
@pytest.mark.parametrize(
    "body_template",
    [
        "Иванов\n{foreign_name} Должность: генеральный директор",
        "{foreign_name} Должность: генеральный директор\nИванов",
    ],
)
def test_adjacent_field_rejects_extra_material_before_the_field_label(
    foreign_name: str,
    body_template: str,
) -> None:
    body = body_template.format(foreign_name=foreign_name)

    assert project_source_focus(body, "Иванов", "должность", max_chars=600) is None


@pytest.mark.parametrize(
    "value",
    ["бухгалтер", "accountant", "ZXCV", "Путин генеральный директор", "李雷", "李", "7", "A"],
)
@pytest.mark.parametrize(
    "body_template",
    ["Иванов\nДолжность: {value}", "Должность: {value}\nИванов"],
)
def test_exact_two_line_record_allows_arbitrary_source_declared_value(
    value: str,
    body_template: str,
) -> None:
    body = body_template.format(value=value)

    result = project_source_focus(body, "Иванов", "должность", max_chars=600)

    assert result is not None
    assert result.excerpt == body


@pytest.mark.parametrize(
    ("body", "query", "focus"),
    [
        ("Smith\njob title: accountant", "Smith", "job title"),
        ("Иванов\nномер телефона: +7 999 123", "Иванов", "номер телефона"),
    ],
)
def test_exact_two_line_record_allows_closed_multi_token_field_labels(
    body: str,
    query: str,
    focus: str,
) -> None:
    result = project_source_focus(body, query, focus, max_chars=600)

    assert result is not None
    assert result.excerpt == body


@pytest.mark.parametrize(
    "field",
    [
        "employee job title: accountant",
        "job title department: accountant",
        "job / title: accountant",
        "job title: accountant: senior",
    ],
)
def test_multi_token_field_label_rejects_prefix_extra_field_and_punctuation(field: str) -> None:
    assert project_source_focus(f"Smith\n{field}", "Smith", "job title", max_chars=600) is None


@pytest.mark.parametrize(
    "body",
    [
        "Преамбула\nИванов\nДолжность: бухгалтер",
        "Иванов\nДолжность: бухгалтер\nСледующая запись",
        "Иванов Должность\nДолжность: бухгалтер",
        "Иванов\nСведения о должности: бухгалтер",
        "Иванов\nДолжность: бухгалтер: старший",
    ],
)
def test_adjacent_field_requires_an_unambiguous_two_line_record(body: str) -> None:
    assert project_source_focus(body, "Иванов", "должность", max_chars=600) is None


@pytest.mark.parametrize("extra", ["Jones", "user_123", "李雷", "محمد"])
@pytest.mark.parametrize(
    "body_template",
    ["Smith {extra}\nRole: engineer", "Role: engineer\nSmith {extra}"],
)
def test_two_line_anchor_cannot_bind_a_field_across_an_unrequested_identity_token(
    extra: str,
    body_template: str,
) -> None:
    body = body_template.format(extra=extra)
    anchor_line = f"Smith {extra}"

    result = project_source_focus(body, "Smith", "role", max_chars=600)

    assert result is not None
    assert result.excerpt == anchor_line
    assert result.focus_match_kind is SourceFocusMatchKind.ANCHOR_CONTEXT


@pytest.mark.parametrize(
    "body",
    ["John Smith\nRole: engineer", "Role: engineer\nJohn Smith"],
)
def test_two_line_anchor_accepts_an_exact_distinct_multi_token_query(body: str) -> None:
    result = project_source_focus(body, "John Smith", "role", max_chars=600)

    assert result is not None
    assert result.excerpt == body


def test_blank_delimited_two_line_record_is_selected_inside_a_larger_source() -> None:
    body = "Преамбула\n\nИванов\nДолжность: бухгалтер\n\nСледующая запись"

    result = project_source_focus(body, "Иванов", "должность", max_chars=600)

    assert result is not None
    assert result.excerpt == "Иванов\nДолжность: бухгалтер"


@pytest.mark.parametrize(
    ("body", "query", "focus"),
    [
        ("山田\n役職: 技師", "山田", "役職"),
        ("Αλέξανδρος\nrole: engineer", "Αλέξανδρος", "role"),
        ("محمد\nrole: engineer", "محمد", "role"),
        ("Ｓｍｉｔｈ\nrole: engineer", "Smith", "role"),
        ("Ⓢⓜⓘⓣⓗ\nrole: engineer", "Smith", "role"),
    ],
)
def test_all_script_and_compatibility_anchors_keep_exact_offsets(
    body: str,
    query: str,
    focus: str,
) -> None:
    result = project_source_focus(body, query, focus, max_chars=600)

    assert result is not None
    assert result.excerpt == body
    assert body[result.start : result.end] == result.excerpt


@pytest.mark.parametrize(
    ("focus", "field"),
    [
        ("должность", "Должности: ведущий инженер"),
        ("роли", "Ролью: ведущий инженер"),
    ],
)
def test_natural_focus_forms_bind_their_closed_field_family(focus: str, field: str) -> None:
    body = f"Иванов\n{field}"

    result = project_source_focus(body, "Иванов", focus, max_chars=600)

    assert result is not None
    assert result.excerpt == body
    assert result.focus_match_kind is SourceFocusMatchKind.FULL


def test_short_numeric_field_value_is_substantive_context() -> None:
    body = "Иванов\nКод: 42"

    result = project_source_focus(body, "Иванов", "код", max_chars=600)

    assert result is not None
    assert result.excerpt == body
    assert result.focus_match_kind is SourceFocusMatchKind.FULL


def test_short_uppercase_identifier_is_substantive_but_short_word_is_not() -> None:
    body = "Иванов\nКод: AB"

    result = project_source_focus(body, "Иванов", "код", max_chars=600)

    assert result is not None
    assert result.excerpt == body
    assert project_source_focus("Иванов\nКод: ab", "Иванов", "код", max_chars=600) is None


def test_every_query_anchor_must_belong_to_the_exact_passage() -> None:
    assert (
        project_source_focus(
            "Анкета\nДолжность: директор",
            "Артемьев анкета",
            "должность",
            max_chars=600,
        )
        is None
    )


def test_ninth_query_anchor_cannot_be_taken_from_an_adjacent_record() -> None:
    query = "alpha bravo charlie delta echo foxtrot golf hotel india"
    body = "alpha bravo charlie delta echo foxtrot golf hotel\nRole: engineer\n\nindia"

    assert project_source_focus(body, query, "role", max_chars=600) is None


def test_query_anchor_uses_the_same_closed_inflection_as_fts_recall() -> None:
    body = "Анкета Артемьев\nДолжность: ведущий инженер"

    result = project_source_focus(body, "в анкете Артемьева", "должность", max_chars=600)

    assert result is not None
    assert result.excerpt == body


def test_identifier_anchor_does_not_accept_a_longer_prefix_collision() -> None:
    assert (
        project_source_focus(
            "Ticket ABC1234\nRole: engineer",
            "ABC123",
            "role",
            max_chars=600,
        )
        is None
    )


@pytest.mark.parametrize(
    ("query", "collision"),
    [
        ("роль", "ролик"),
        ("код", "кодекс"),
        ("черных", "черновик"),
        ("акт", "актив"),
        ("акте", "актив"),
        ("акта", "актив"),
        ("актом", "актив"),
    ],
)
def test_stem_prefix_is_recall_only_and_cannot_admit_a_source_record(
    query: str,
    collision: str,
) -> None:
    assert (
        project_source_focus(
            f"{collision}\nДолжность: директор",
            query,
            "должность",
            max_chars=600,
        )
        is None
    )


@pytest.mark.parametrize(
    ("query", "collision"),
    [
        ("угол", "уголь"),
        ("сталь", "стал"),
        ("даль", "дал"),
        ("мел", "мель"),
        ("пар", "пари"),
        ("new", "news"),
    ],
)
def test_generic_suffixes_cannot_equate_independent_lexemes(query: str, collision: str) -> None:
    assert (
        project_source_focus(
            f"{collision}\nДолжность: директор",
            query,
            "должность",
            max_chars=600,
        )
        is None
    )


@pytest.mark.parametrize("control", ["\t", "\r", "\0"])
def test_projection_never_returns_non_lf_control_text(control: str) -> None:
    assert (
        project_source_focus(
            f"Иванов{control}Должность: ведущий инженер",
            "Иванов",
            "должность",
            max_chars=600,
        )
        is None
    )


def test_field_label_without_a_value_fails_closed() -> None:
    assert (
        project_source_focus(
            "Иванов\nДолжность:",
            "иванов",
            "иванов должност",
            max_chars=600,
        )
        is None
    )


def test_table_projection_uses_one_exact_record_and_direct_header() -> None:
    body = "Фамилия | Должность\nИванов | ведущий инженер\nПетров | директор"

    result = project_source_focus(body, "иванов", "иванов должност", max_chars=480)

    assert result is not None
    assert result.excerpt == "Фамилия | Должность\nИванов | ведущий инженер"
    assert body[result.start : result.end] == result.excerpt
    assert "Петров" not in result.excerpt
    assert result.focus_match_kind is SourceFocusMatchKind.FULL


def test_distant_table_header_is_not_cross_joined_over_neighbour_records() -> None:
    body = "\n".join(
        ["Фамилия | Должность"]
        + [f"Петров-{index:02d} | директор" for index in range(20)]
        + ["Иванов | ведущий инженер"]
    )

    result = project_source_focus(body, "иванов", "иванов должност", max_chars=480)

    assert result is not None
    assert result.excerpt == "Иванов | ведущий инженер"
    assert body[result.start : result.end] == result.excerpt
    assert result.focus_match_kind is SourceFocusMatchKind.ANCHOR_CONTEXT


def test_sparse_table_section_binds_exactly_its_first_record() -> None:
    body = (
        "ORION platoon |  |  | \n"
        "ALPHA person | Commander platoon | Senior | 41\n"
        "BRAVO person | Operator | Junior | 42"
    )

    result = project_source_focus(body, "ORION", "ORION commander platoon", max_chars=600)

    assert result is not None
    assert result.excerpt == ("ORION platoon |  |  | \nALPHA person | Commander platoon | Senior | 41")
    assert body[result.start : result.end] == result.excerpt
    assert "BRAVO" not in result.excerpt
    assert result.focus_match_kind is SourceFocusMatchKind.FULL


def test_long_unicode_body_keeps_python_codepoint_offsets() -> None:
    body = ("ﬁ" * 500) + ("before " * 30) + "\nИванов — ведущий инженер по эксплуатации\n" + ("after " * 500)

    result = project_source_focus(body, "иванов", "иванов должност", max_chars=120)

    assert result is not None
    assert len(result.excerpt) <= 120
    assert body[result.start : result.end] == result.excerpt
    assert "Иванов — ведущий инженер" in result.excerpt


def test_repeated_tokens_keep_long_line_candidate_work_bounded(monkeypatch) -> None:
    calls = 0
    original = source_focus_module._window_around_anchor  # noqa: SLF001

    def observed(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(source_focus_module, "_window_around_anchor", observed)
    body = " ".join(["Иванов Должность инженер"] * 1_000)

    result = project_source_focus(body, "Иванов", "Должность", max_chars=120)

    assert result is not None
    assert body[result.start : result.end] == result.excerpt
    assert calls <= 40


@pytest.mark.parametrize("carrier", ["X.Role", "X-Role", "X#Role", "X\u0301Role"])
def test_long_line_window_cannot_create_a_focus_token_at_its_left_cut_edge(carrier: str) -> None:
    body = "!" * 818 + f"{carrier} Engineer " + "!" * 166 + "Smith " + "!" * 1_000

    result = project_source_focus(body, "Smith", "Role", max_chars=720)

    assert result is not None
    assert result.focus_match_kind is SourceFocusMatchKind.ANCHOR_CONTEXT
    assert result.matched_focus_count == 0
    assert carrier not in result.excerpt


@pytest.mark.parametrize("carrier", ["Role.X", "Role-X", "Role#X", "Role\u0301X"])
def test_long_line_window_cannot_create_a_focus_token_at_its_right_cut_edge(carrier: str) -> None:
    body = "!" * 818 + "Smith Engineer " + "!" * 521 + carrier + "!" * 1_000

    result = project_source_focus(body, "Smith", "Role", max_chars=720)

    assert result is not None
    assert result.focus_match_kind is SourceFocusMatchKind.ANCHOR_CONTEXT
    assert result.matched_focus_count == 0
    assert carrier not in result.excerpt


def test_pathological_line_and_token_counts_fail_before_unbounded_materialization() -> None:
    line_flood = ("\n" * source_focus_module.MAX_SOURCE_FOCUS_LINES) + "Иванов"
    token_flood = " ".join(["token"] * (source_focus_module.MAX_SOURCE_FOCUS_TOKENS + 1))
    single_character_field_flood = "Smith\ncode: " + ("A " * 300_000)

    assert project_source_focus(line_flood, "Иванов", "должность", max_chars=600) is None
    assert project_source_focus(token_flood, "token", "role", max_chars=600) is None
    assert project_source_focus(single_character_field_flood, "Smith", "code", max_chars=600) is None


def test_exact_proof_terms_fail_closed_instead_of_truncating_the_request() -> None:
    query_terms = tuple(f"x{index:02d}" for index in range(25))
    focus_terms = tuple(f"f{index:02d}" for index in range(25))

    assert (
        project_source_focus(
            f"{' '.join(query_terms[:-1])}\nRole: engineer",
            " ".join(query_terms),
            "role",
            max_chars=600,
        )
        is None
    )
    assert (
        project_source_focus(
            f"Smith {' '.join(focus_terms[:-1])} engineer",
            "Smith",
            " ".join(focus_terms),
            max_chars=600,
        )
        is None
    )


def test_exact_proof_budget_accepts_all_twenty_four_terms() -> None:
    query = " ".join(f"x{index:02d}" for index in range(24))
    body = f"{query}\nRole: engineer"

    result = project_source_focus(body, query, "role", max_chars=600)

    assert result is not None
    assert result.excerpt == body


@pytest.mark.parametrize("term", ["X", "7", "李"])
def test_single_character_terms_are_part_of_the_exact_proof(term: str) -> None:
    assert project_source_focus("Smith\nRole: engineer", f"Smith {term}", "role", max_chars=600) is None

    anchored = project_source_focus(
        f"Smith {term}\nRole: engineer",
        f"Smith {term}",
        "role",
        max_chars=600,
    )
    assert anchored is not None
    assert anchored.focus_match_kind is SourceFocusMatchKind.FULL

    missing_focus = project_source_focus(
        "Smith\nRole: engineer",
        "Smith",
        f"role {term}",
        max_chars=600,
    )
    assert missing_focus is not None
    assert missing_focus.focus_match_kind is SourceFocusMatchKind.ANCHOR_CONTEXT
    assert missing_focus.matched_focus_count == 1

    full_focus = project_source_focus(
        f"Smith\nRole: {term} engineer",
        "Smith",
        f"role {term}",
        max_chars=600,
    )
    assert full_focus is not None
    assert full_focus.focus_match_kind is SourceFocusMatchKind.FULL
    assert full_focus.matched_focus_count == 2


def test_single_character_terms_count_toward_the_exact_proof_cap() -> None:
    terms = tuple(chr(ord("A") + index) for index in range(25))
    accepted_query = " ".join(terms[:-1])
    accepted_body = f"{accepted_query}\nRole: engineer"

    assert project_source_focus(accepted_body, accepted_query, "role", max_chars=600) is not None
    assert project_source_focus(accepted_body, " ".join(terms), "role", max_chars=600) is None


def test_fts_lead_tokens_keep_the_same_substantive_single_character_terms() -> None:
    assert source_focus_fts_tokens("в X 7 李") == ("X", "7", "李")


@pytest.mark.parametrize(
    ("body", "query", "focus"),
    [("李\nX: engineer", "李", "X"), ("Smith\n7: engineer", "Smith", "7")],
)
def test_exact_two_line_record_accepts_one_character_field_labels(
    body: str,
    query: str,
    focus: str,
) -> None:
    result = project_source_focus(body, query, focus, max_chars=600)

    assert result is not None
    assert result.excerpt == body
    assert result.focus_match_kind is SourceFocusMatchKind.FULL


@pytest.mark.parametrize("term", ["A", "I", "Ａ", "Ｉ", "Ⓐ", "Ⓘ"])
def test_nfkc_equivalent_uppercase_codes_remain_exact_terms(term: str) -> None:
    assert source_focus_fts_tokens(term) == (term,)
    assert project_source_focus("Smith\nRole: engineer", f"Smith {term}", "role", max_chars=600) is None

    body = f"Smith {term}\nRole: engineer"
    result = project_source_focus(body, f"Smith {term}", "role", max_chars=600)

    assert result is not None
    assert result.excerpt == body


def test_projection_is_typed_and_immutable() -> None:
    sentinel = "FRIDAY-PRIVATE-EXCERPT-SENTINEL"
    result = project_source_focus(
        f"Иванов — ведущий инженер {sentinel}",
        "иванов",
        "иванов должност",
        max_chars=600,
    )
    assert result is not None
    assert result.focus_terms_matched == result.matched_focus_count
    assert result.anchor_context_terms == result.context_count

    with pytest.raises(FrozenInstanceError):
        result.excerpt = "mutated"  # type: ignore[misc]
    assert sentinel not in repr(result)
    with pytest.raises(TypeError):
        asdict(result)  # type: ignore[arg-type]
