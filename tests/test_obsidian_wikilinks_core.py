from __future__ import annotations

from pathlib import Path

import pytest

from friday.organs.obsidian.contracts import RevisionConflictError, VaultLimitError
from friday.organs.obsidian.vault_store import VaultStore
from friday.organs.obsidian.wikilinks import (
    LinkLimits,
    LinkResolutionStatus,
    LinkSyntax,
    build_link_graph,
    build_vault_link_graph,
    execute_move_plan,
    move_plan_postcondition,
    parse_links,
    plan_move,
)


def test_parser_finds_wikilinks_embeds_and_markdown_but_ignores_inert_regions() -> None:
    text = """
[[Projects/Friday#Plan|Friday]]
![[Attachments/map.png]]
[project](../Projects/Friday.md#Plan)
![diagram](<../Attachments/map image.png>)
`[[Inline/Code]]`
<!-- [[Commented/Link]] -->
```markdown
[[Fenced/Code]]
```
plain Projects/Friday
"""

    links = parse_links(text)

    assert [link.syntax for link in links] == [
        LinkSyntax.WIKILINK,
        LinkSyntax.WIKILINK,
        LinkSyntax.MARKDOWN,
        LinkSyntax.MARKDOWN,
    ]
    assert [(link.target_path, link.fragment) for link in links] == [
        ("Projects/Friday", "#Plan"),
        ("Attachments/map.png", ""),
        ("../Projects/Friday.md", "#Plan"),
        ("../Attachments/map image.png", ""),
    ]
    assert links[0].alias == "Friday"
    assert links[1].embed is True
    assert links[3].angle_destination is True


def test_graph_resolves_exact_paths_before_titles_and_exposes_every_ambiguity() -> None:
    graph = build_link_graph(
        {
            "Projects/Friday.md": "target",
            "Archive/Friday.md": "other target",
            "Notes/Search.md": (
                "[[Projects/Friday]] [[Friday]] [[Missing]] "
                "[relative](../Projects/Friday.md) [web](https://example.test/x)"
            ),
        }
    )

    outgoing = graph.outgoing("Notes/Search")

    assert outgoing[0].status is LinkResolutionStatus.RESOLVED
    assert outgoing[0].resolved_path == "Projects/Friday.md"
    assert outgoing[1].status is LinkResolutionStatus.AMBIGUOUS
    assert outgoing[1].candidates == ("Archive/Friday.md", "Projects/Friday.md")
    assert outgoing[2].status is LinkResolutionStatus.UNRESOLVED
    assert outgoing[3].resolved_path == "Projects/Friday.md"
    assert outgoing[4].status is LinkResolutionStatus.EXTERNAL
    assert [item.link.raw for item in graph.backlinks("Projects/Friday.md")] == [
        "[[Projects/Friday]]",
        "[relative](../Projects/Friday.md)",
    ]


def test_exact_frontmatter_title_can_resolve_but_never_break_a_title_tie() -> None:
    notes = {
        "One.md": "[[Release overview]]",
        "Two.md": "target",
        "Three.md": "target",
    }
    unique = build_link_graph(notes, titles={"Two.md": "Release overview"})
    ambiguous = build_link_graph(
        notes,
        titles={"Two.md": "Release overview", "Three.md": "Release overview"},
    )

    assert unique.outgoing("One.md")[0].resolved_path == "Two.md"
    assert ambiguous.outgoing("One.md")[0].status is LinkResolutionStatus.AMBIGUOUS


def test_move_plan_rewrites_only_links_that_resolved_to_the_exact_source() -> None:
    graph = build_link_graph(
        {
            "Projects/Friday.md": "# Friday\n",
            "Archive/Friday.md": "different note\n",
            "Notes/Search.md": (
                "[[Projects/Friday]] [project](../Projects/Friday.md#Plan) plain Projects/Friday\n"
            ),
            "Notes/Obsidian.md": ("![[Projects/Friday#Plan|alias]] [[Friday]] [[Missing]] [[{{dynamic}}]]\n"),
        }
    )

    plan = plan_move(graph, "Projects/Friday", "Architecture/Friday")
    rewritten = {item.output_path: item.content for item in plan.rewrites}

    assert rewritten["Notes/Search.md"] == (
        "[[Architecture/Friday]] [project](../Architecture/Friday.md#Plan) plain Projects/Friday\n"
    )
    assert rewritten["Notes/Obsidian.md"].startswith("![[Architecture/Friday#Plan|alias]] [[Friday]]")
    assert [item.link.raw for item in plan.ambiguous] == ["[[Friday]]"]
    assert [item.link.raw for item in plan.unresolved] == ["[[Missing]]"]
    assert [item.link.raw for item in plan.dynamic] == ["[[{{dynamic}}]]"]
    assert "Archive/Friday.md" not in plan.changed_paths


def test_move_plan_preserves_resolved_relative_links_inside_the_moved_note() -> None:
    graph = build_link_graph(
        {
            "Projects/Friday.md": "[search](../Notes/Search.md) [[Notes/Search]]\n",
            "Notes/Search.md": "target\n",
        }
    )

    plan = plan_move(graph, "Projects/Friday.md", "Architecture/Nested/Friday.md")

    assert len(plan.rewrites) == 1
    assert plan.rewrites[0].output_path == "Architecture/Nested/Friday.md"
    assert plan.rewrites[0].content == ("[search](../../Notes/Search.md) [[Notes/Search]]\n")


def test_move_execution_is_revision_guarded_and_idempotently_reconcilable(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store = VaultStore(vault)
    store.write_text("Projects/Friday.md", "# Friday\n", create_only=True)
    store.write_text(
        "Notes/Search.md",
        "[[Projects/Friday]] and [project](../Projects/Friday.md)\n",
        create_only=True,
    )
    plan = plan_move(
        build_vault_link_graph(store),
        "Projects/Friday.md",
        "Architecture/Friday.md",
    )

    first = execute_move_plan(store, plan)
    replay = execute_move_plan(store, plan)

    assert first.moved_applied is True
    assert all(change.applied for change in first.link_rewrites.changes)
    assert replay.moved_applied is False
    assert all(not change.applied for change in replay.link_rewrites.changes)
    assert not store.exists("Projects/Friday.md")
    assert store.read("Architecture/Friday.md").text() == "# Friday\n"
    assert store.read("Notes/Search.md").text() == (
        "[[Architecture/Friday]] and [project](../Architecture/Friday.md)\n"
    )
    assert move_plan_postcondition(store, plan)
    assert dict(first.changed_revisions)["Architecture/Friday.md"] == plan.moved_revision


def test_link_rewrite_race_never_clobbers_the_peer_revision(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store = VaultStore(vault)
    store.write_text("Projects/Friday.md", "target", create_only=True)
    store.write_text("Notes/Search.md", "[[Projects/Friday]]", create_only=True)
    plan = plan_move(
        build_vault_link_graph(store),
        "Projects/Friday.md",
        "Architecture/Friday.md",
    )
    store.write_text("Notes/Search.md", "peer revision")

    with pytest.raises(RevisionConflictError):
        execute_move_plan(store, plan)

    assert store.read("Notes/Search.md").text() == "peer revision"
    assert store.read("Architecture/Friday.md").text() == "target"
    assert not store.exists("Projects/Friday.md")


def test_parser_and_graph_fail_closed_at_their_declared_bounds() -> None:
    with pytest.raises(VaultLimitError, match="maximum link count"):
        parse_links(
            "[[One]] [[Two]]",
            limits=LinkLimits(max_links=1),
        )
    with pytest.raises(VaultLimitError, match="aggregate Markdown byte budget"):
        build_link_graph(
            {"One.md": "1234", "Two.md": "5678"},
            limits=LinkLimits(max_total_text_bytes=7),
        )
    with pytest.raises(VaultLimitError, match="target exceeds"):
        parse_links(
            "[[target-too-long]]",
            limits=LinkLimits(max_target_chars=4),
        )
