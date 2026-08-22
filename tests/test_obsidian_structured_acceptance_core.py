from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from friday.organs.obsidian.base_spec import (
    evaluate_base,
    friday_active_notes_spec,
    parse_base,
    render_base,
)
from friday.organs.obsidian.contracts import PropertyValue
from friday.organs.obsidian.note_merge import build_preserve_both_preview
from friday.organs.obsidian.structured_notes import (
    StructuredNoteError,
    append_section_item,
    render_conversation_summary,
    replace_section,
)
from friday.organs.obsidian.task_index import append_task, list_tasks
from friday.organs.obsidian.templates import render_template


def test_append_reuses_one_section_and_never_duplicates_the_item() -> None:
    original = "---\nstatus: draft\n---\n# Friday\n\nExisting.\n"
    first = append_section_item(original, "Проверка дополнения", "Новая строка")
    second = append_section_item(first.content, "Проверка дополнения", "Новая строка")

    assert first.changed is True
    assert second.changed is False
    assert second.content == first.content
    assert first.content.startswith(original)
    assert first.content.count("## Проверка дополнения") == 1
    assert first.content.count("Новая строка") == 1


def test_append_fails_closed_when_the_same_section_is_ambiguous() -> None:
    with pytest.raises(StructuredNoteError, match="ambiguous"):
        append_section_item("## Friday\n\nA\n\n## friday\n\nB\n", "Friday", "C")


def test_replace_section_preserves_everything_outside_the_exact_section() -> None:
    source = "# Root\n\nBefore\n\n## Проверка дополнения\n\nOld\n\n## Tail\n\nAfter\n"
    result = replace_section(source, "Проверка дополнения", "Версия, записанная Friday")

    assert result.startswith("# Root\n\nBefore")
    assert result.endswith("## Tail\n\nAfter\n")
    assert "Old" not in result
    assert "Версия, записанная Friday" in result


def test_section_edits_ignore_headings_inside_frontmatter_fences_and_comments() -> None:
    source = (
        "---\ndescription: |\n  ## Проверка дополнения\n---\n"
        "```markdown\n## Проверка дополнения\ncode body\n```\n"
        "<!--\n## Проверка дополнения\ncomment body\n-->\n"
        "## Проверка дополнения\n\nReal body\n\n## Tail\n\nKeep\n"
    )

    replaced = replace_section(source, "Проверка дополнения", "Safe replacement")

    assert "  ## Проверка дополнения" in replaced
    assert "## Проверка дополнения\ncode body" in replaced
    assert "## Проверка дополнения\ncomment body" in replaced
    assert "## Проверка дополнения\n\nSafe replacement\n## Tail" in replaced
    assert "Real body" not in replaced


def test_append_does_not_reuse_a_fake_heading_inside_a_fence() -> None:
    source = "```markdown\n## Friday\ninside code\n```\n"
    edit = append_section_item(source, "Friday", "real item")

    assert edit.section_reused is False
    assert edit.content.endswith("## Friday\n\nreal item\n")


def test_dated_task_is_concrete_searchable_and_completed_tasks_are_excluded() -> None:
    edit = append_task(
        "# Daily\n",
        section="Friday",
        text="Проверить поиск в Obsidian",
        due_date=date(2026, 8, 23),
        due_time=time(10, 0),
        operation_id="obsop-task-1",
    )
    markdown = edit.content + "- [x] Старый поиск в Obsidian 📅 2026-08-01\n"

    tasks = list_tasks(markdown, query="Obsidian", incomplete_only=True)

    assert len(tasks) == 1
    assert tasks[0].due_date == date(2026, 8, 23)
    assert tasks[0].due_time == time(10, 0)
    assert tasks[0].block_id.startswith("friday-task-")


def test_template_fills_supplied_fields_and_preserves_unknown_syntax() -> None:
    template = (
        "---\ntype: meeting\ndate: {{date}}\nproject: {{project}}\n---\n\n"
        "# {{title}}\n\n{{participants}}\n\n{{discussion}}\n\n{{actions}}\n\n{{plugin_macro}}\n"
    )
    result = render_template(
        template,
        {
            "title": "Проверка интеграции Obsidian",
            "project": "Friday",
            "participants": ["Алиса", "Борис"],
            "discussion": "Базовая синхронизация работает.",
            "actions": "- [ ] Проверить конфликты",
        },
        current_date=date(2026, 8, 22),
    )

    assert "date: 2026-08-22" in result.content
    assert "project: Friday" in result.content
    assert "Алиса, Борис" in result.content
    assert "{{plugin_macro}}" in result.content
    assert result.unresolved == ("plugin_macro",)


def test_generated_base_and_server_evaluator_select_the_same_active_notes() -> None:
    spec = friday_active_notes_spec()
    parsed = parse_base(render_base(spec))
    notes = [
        {
            "path": "Projects/Active.md",
            "title": "Active",
            "modified_at": datetime(2026, 8, 22, 9, tzinfo=UTC),
            "properties": {
                "project": PropertyValue.coerce("Friday"),
                "status": PropertyValue.coerce("active"),
            },
        },
        {
            "path": "Projects/Done.md",
            "title": "Done",
            "modified_at": datetime(2026, 8, 22, 10, tzinfo=UTC),
            "properties": {
                "project": PropertyValue.coerce("Friday"),
                "status": PropertyValue.coerce("done"),
            },
        },
    ]

    rows = evaluate_base(parsed, notes)

    assert [row["file.name"] for row in rows] == ["Active"]
    assert rows[0]["status"] == "active"


def test_conflict_preview_is_non_destructive_and_contains_both_versions() -> None:
    preview = build_preserve_both_preview("# Note\n\nFriday edit\n", "# Note\n\nAndroid edit\n")

    assert "Friday edit" in preview.merged_content
    assert "Android edit" in preview.merged_content
    assert "-Friday edit" in preview.unified_diff
    assert "+Android edit" in preview.unified_diff
    assert preview.identical is False


def test_conversation_summary_has_all_three_sections_and_no_hidden_trace() -> None:
    result = render_conversation_summary(
        conclusions=["Синхронизация работает"],
        open_questions=["Проверить конфликты"],
        next_actions=["Запустить батарею"],
    )

    assert result.startswith("# Conversation Summary")
    assert "## Conclusions" in result
    assert "## Open questions" in result
    assert "## Next actions" in result
    assert "tool_call" not in result
