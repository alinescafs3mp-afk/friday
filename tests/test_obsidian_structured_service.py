from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from friday.organs.obsidian.base_spec import parse_base
from friday.organs.obsidian.contracts import PropertyType
from friday.organs.obsidian.frontmatter import parse_frontmatter
from friday.organs.obsidian.structured_service import (
    MAX_NOTE_RECORDS,
    StructuredNoteRecord,
    StructuredNoteService,
    StructuredServiceError,
)

SERVICE = StructuredNoteService()


def test_meta_update_merges_typed_properties_and_tags_without_touching_body() -> None:
    source = (
        "---\n"
        "aliases: [legacy]\n"
        "plugin:\n"
        "  nested: keep-byte-for-byte\n"
        "status: draft\n"
        "tags:\n"
        '  - "obsidian"\n'
        "---\n"
        "# Friday Test\n\n"
        "Body with --- inside remains unchanged.\n"
    )
    original_body = parse_frontmatter(source).body

    first = SERVICE.merge_properties_and_tags(
        source,
        {
            "status": "review",
            "project": "Friday",
            "reviewed": False,
            "priority": 3,
        },
        tags=("integration", "obsidian", "test"),
    )
    parsed = parse_frontmatter(first.content)

    assert first.changed is True
    assert parsed.body == original_body
    assert "plugin:\n  nested: keep-byte-for-byte\n" in first.content
    assert parsed.properties["status"].value == "review"
    assert parsed.properties["project"].value == "Friday"
    assert parsed.properties["reviewed"].type is PropertyType.CHECKBOX
    assert parsed.properties["priority"].type is PropertyType.NUMBER
    assert parsed.properties["tags"].type is PropertyType.LIST
    assert parsed.properties["tags"].value == ("obsidian", "integration", "test")

    replay = SERVICE.merge_properties_and_tags(
        first.content,
        {
            "status": "review",
            "project": "Friday",
            "reviewed": False,
            "priority": 3,
        },
        tags=("integration", "OBSIDIAN", "test"),
    )

    assert replay.changed is False
    assert replay.content == first.content
    assert replay.properties["tags"].value.count("obsidian") == 1


def test_meta_update_upgrades_a_scalar_tag_but_rejects_ambiguous_tag_authority() -> None:
    upgraded = SERVICE.merge_properties_and_tags(
        "---\ntags: obsidian\n---\nBody\n",
        {"status": "review"},
        tags=("obsidian", "integration"),
    )
    assert parse_frontmatter(upgraded.content).properties["tags"].value == (
        "obsidian",
        "integration",
    )

    with pytest.raises(StructuredServiceError, match="merge-tags"):
        SERVICE.merge_properties_and_tags("Body\n", {"tags": ["test"]}, tags=("integration",))
    with pytest.raises(StructuredServiceError, match="existing tags"):
        SERVICE.merge_properties_and_tags("---\ntags: 42\n---\nBody\n", {}, tags=("test",))


def test_meta_update_is_bounded_before_frontmatter_rendering() -> None:
    too_many = {f"field_{index}": "value" for index in range(129)}
    with pytest.raises(StructuredServiceError, match="field limit"):
        SERVICE.merge_properties_and_tags("Body\n", too_many)
    with pytest.raises(StructuredServiceError, match="size limit"):
        SERVICE.merge_properties_and_tags("x" * (4 * 1024 * 1024 + 1), {})


def test_task_add_and_query_are_idempotent_concrete_and_source_aware() -> None:
    due_at = datetime(2026, 8, 23, 10, 0)
    source = "# 2026-08-22\n\n## Friday\n\nExisting item.\n"

    first = SERVICE.add_dated_task(
        source,
        section="Friday",
        text="Проверить поиск в Obsidian",
        due_at=due_at,
        operation_id="obsop-task-20260822",
    )
    replay = SERVICE.add_dated_task(
        first.content,
        section="Friday",
        text="Проверить поиск в Obsidian",
        due_at=due_at,
        operation_id="obsop-task-20260822",
    )

    assert first.changed is True
    assert first.section_reused is True
    assert first.task.block_id.startswith("friday-task-")
    assert first.task.due_date == date(2026, 8, 23)
    assert first.task.due_time is not None and first.task.due_time.isoformat() == "10:00:00"
    assert replay.changed is False
    assert replay.content == first.content
    assert replay.task.block_id == first.task.block_id

    current = StructuredNoteRecord(path="Daily/2026-08-22.md", content=first.content)
    completed = StructuredNoteRecord(
        path="Daily/Old.md",
        content="- [x] Проверить поиск в Obsidian 📅 2026-08-01 ⏰ 10:00 ^friday-task-deadbeef0000\n",
    )
    undated = StructuredNoteRecord(
        path="Projects/Backlog.md",
        content="- [ ] Иная задача про Obsidian ^friday-task-aabbccddeeff\n",
    )

    hits = SERVICE.query_incomplete_tasks((completed, undated, current), query="Obsidian")

    assert [(hit.path, hit.task.completed) for hit in hits] == [
        ("Daily/2026-08-22.md", False),
        ("Projects/Backlog.md", False),
    ]
    assert hits[0].due_at == due_at
    assert hits[0].task.block_id == first.task.block_id
    assert hits[1].due_at is None


@pytest.mark.parametrize(
    "due_at",
    [
        datetime(2026, 8, 23, 10, 0, 1),
        datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
    ],
)
def test_task_add_rejects_temporal_values_that_would_be_silently_truncated(
    due_at: datetime,
) -> None:
    with pytest.raises(StructuredServiceError, match="minute-precise local"):
        SERVICE.add_dated_task(
            "# Daily\n",
            section="Friday",
            text="Проверить поиск",
            due_at=due_at,
            operation_id="obsop-task",
        )


def test_task_query_rejects_ambiguous_or_untyped_note_snapshots() -> None:
    first = StructuredNoteRecord(path="Daily/A.md", content="- [ ] Task\n")
    duplicate = StructuredNoteRecord(path="daily/a.md", content="- [ ] Other\n")
    with pytest.raises(StructuredServiceError, match="duplicate path"):
        SERVICE.query_incomplete_tasks((first, duplicate))
    with pytest.raises(StructuredServiceError, match="non-record"):
        SERVICE.query_incomplete_tasks((first, object()))  # type: ignore[arg-type]


def test_template_render_fills_known_fields_preserves_unknown_syntax_and_valid_yaml() -> None:
    template = (
        "---\n"
        "type: meeting\n"
        "date: {{date}}\n"
        "project: {{project}}\n"
        "---\n\n"
        "# {{title}}\n\n"
        "## Participants\n\n{{participants}}\n\n"
        "## Discussion\n\n{{discussion}}\n\n"
        "## Actions\n\n{{actions}}\n\n"
        "{{plugin_macro}}\n"
    )
    result = SERVICE.render_from_template(
        template,
        {
            "title": "Проверка интеграции Obsidian",
            "project": "Friday",
            "participants": ("Алиса", "Борис"),
            "discussion": "Базовая синхронизация работает.",
            "actions": "- [ ] Проверить конфликты",
        },
        current_date=date(2026, 8, 22),
    )
    properties = parse_frontmatter(result.content).properties

    assert properties["type"].value == "meeting"
    assert properties["date"].value == date(2026, 8, 22)
    assert properties["project"].value == "Friday"
    assert "Алиса, Борис" in result.content
    assert "{{plugin_macro}}" in result.content
    assert result.unresolved == ("plugin_macro",)


def test_template_render_fails_closed_on_missing_known_values_and_expansion_overflow() -> None:
    with pytest.raises(StructuredServiceError, match="missing required.*project"):
        SERVICE.render_from_template(
            "# {{title}}\n\n{{project}}\n",
            {"title": "Meeting"},
            current_date=date(2026, 8, 22),
        )
    with pytest.raises(StructuredServiceError, match="concrete date"):
        SERVICE.render_from_template(
            "# {{title}}\n",
            {"title": "Meeting"},
            current_date=datetime(2026, 8, 22),  # type: ignore[arg-type]
        )
    with pytest.raises(StructuredServiceError, match="size limit"):
        SERVICE.render_from_template(
            "{{discussion}}\n" * 43,
            {"discussion": "x" * 100_000},
            current_date=date(2026, 8, 22),
        )


def test_work_summary_adds_exact_links_later_without_replacing_or_duplicating_body() -> None:
    summary = SERVICE.render_summary(
        conclusions=("Синхронизация работает",),
        open_questions=("Проверить конфликты",),
        next_actions=("Запустить батарею",),
    )
    first = SERVICE.add_summary_links(
        summary,
        (
            "Projects/Friday Architecture.md",
            "Projects/Retrieval.md",
            "projects/retrieval.md",
        ),
    )
    replay = SERVICE.add_summary_links(
        first.content,
        ("Projects/Friday Architecture.md", "Projects/Retrieval.md"),
    )

    assert summary.startswith("# Conversation Summary")
    assert all(heading in summary for heading in ("## Conclusions", "## Open questions", "## Next actions"))
    assert first.content.startswith(summary)
    assert first.added_paths == (
        "Projects/Friday Architecture.md",
        "Projects/Retrieval.md",
    )
    assert first.content.count("## Related notes") == 1
    assert first.content.count("[[Projects/Friday Architecture]]") == 1
    assert first.content.count("[[Projects/Retrieval]]") == 1
    assert replay.changed is False
    assert replay.added_paths == ()
    assert replay.content == first.content


def test_work_summary_rejects_internal_protocol_markup_and_unsafe_links() -> None:
    with pytest.raises(StructuredServiceError, match="internal protocol"):
        SERVICE.render_summary(
            conclusions=("<tool_call>{}</tool_call>",),
            open_questions=(),
            next_actions=(),
        )
    with pytest.raises(StructuredServiceError, match="safe relative"):
        SERVICE.add_summary_links("# Summary\n", ("Projects/Bad|Alias.md",))
    with pytest.raises(StructuredServiceError, match="ambiguous"):
        SERVICE.add_summary_links(
            "# Summary\n\n## Related notes\n\nA\n\n## related NOTES\n\nB\n",
            ("Projects/A.md",),
        )


def _base_note(
    path: str, status: str, modified_at: datetime, *, project: str = "Friday"
) -> StructuredNoteRecord:
    return StructuredNoteRecord(
        path=path,
        modified_at=modified_at,
        content=(
            f"---\nproject: {project}\nstatus: {status}\n---\n"
            f"# {path.rsplit('/', 1)[-1].removesuffix('.md')}\n"
        ),
    )


def test_base_generation_and_friday_evaluation_share_one_spec_and_refresh_from_records() -> None:
    active_a = _base_note("Projects/Active A.md", "active", datetime(2026, 8, 22, 9))
    active_b = _base_note("Projects/Active B.md", "review", datetime(2026, 8, 22, 10))
    done = _base_note("Projects/Done.md", "done", datetime(2026, 8, 22, 11))
    other = _base_note(
        "Projects/Other.md",
        "active",
        datetime(2026, 8, 22, 12),
        project="Other",
    )

    first = SERVICE.generate_friday_base((active_a, active_b, done, other))

    assert first.path == "Bases/Friday Active Notes.base"
    assert first.evaluator == "friday"
    assert parse_base(first.content) == first.spec
    assert [row["file.name"] for row in first.rows] == ["Active B", "Active A"]
    assert [row["status"] for row in first.rows] == ["review", "active"]

    changed_b = _base_note("Projects/Active B.md", "done", datetime(2026, 8, 22, 13))
    refreshed = SERVICE.generate_friday_base((active_a, changed_b, done, other))
    assert [row["file.name"] for row in refreshed.rows] == ["Active A"]


def test_base_and_record_inputs_are_bounded_and_fail_closed() -> None:
    with pytest.raises(StructuredServiceError, match="unsafe segment"):
        StructuredNoteRecord(path="../Outside.md", content="# Unsafe\n")

    records = (
        StructuredNoteRecord(path=f"Projects/{index}.md", content="# Note\n")
        for index in range(MAX_NOTE_RECORDS + 1)
    )
    with pytest.raises(StructuredServiceError, match="record limit"):
        SERVICE.generate_friday_base(records)
