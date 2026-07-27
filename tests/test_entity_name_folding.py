"""A graph node is a thing, and «Пётр Иванов» is not a different person from «Петра Иванова».

`entities.normalized_name` is what `find_entity_by_name` looks up, so whatever it
folds decides which extraction attaches to which node — and what it does NOT fold
accumulates one node per grammatical case. The owner's own graph held
«CIDR-ПОДПИСКА» beside «CIDR-ПОДПИСКУ»; a node that exists twice can be neither
reliably found nor reliably counted, and every link splits between the copies.

Measured before this shipped, on real graphs: 47 stand entities and 30 live ones
produced exactly two collisions — both of them the known duplicate pairs — and
zero false merges. That is the number that made it safe; the tests below are its
shape.
"""

from __future__ import annotations

import pytest

from jericho.storage import normalize_entity_name
from jericho.storage.models import Entity, new_id


@pytest.mark.parametrize(
    "forms",
    [
        ("Пётр Иванов", "Петр Иванов", "ПЕТРА ИВАНОВА"),
        ("Чёрные Списки", "Черных Списков", "чёрным спискам"),
        ("CIDR-ПОДПИСКА", "CIDR-ПОДПИСКУ", "cidr-подписки"),
        ("SNI-ПОДПИСКА", "SNI-ПОДПИСКУ"),
        ("Конфигурация", "Конфигурации", "КОНФИГУРАЦИЮ"),
    ],
)
def test_one_thing_normalizes_to_one_name(forms):
    normalized = {normalize_entity_name(form) for form in forms}
    assert len(normalized) == 1, f"{forms} -> {normalized}"


@pytest.mark.parametrize(
    "identifier",
    ["BRK.A", "PK-04-04", "GPL-3.0", "ERC-20", "ABC/B", "autovacuum_vacuum_scale_factor"],
)
def test_codes_survive_byte_for_byte(identifier):
    """A code is not a word: folding one is how «ABC.A» and «ABC/B» collapse."""
    assert normalize_entity_name(identifier) == identifier.casefold()


@pytest.mark.parametrize(
    ("left", "right"),
    [("Атлас", "Атлант"), ("Волга", "Волк"), ("Марс", "Марш"), ("BRK.A", "BRK.B")],
)
def test_different_things_stay_different(left, right):
    assert normalize_entity_name(left) != normalize_entity_name(right)


def test_a_lookup_finds_the_node_whatever_case_the_question_uses(storage):
    """The wiring: folding that no lookup uses would change nothing at all."""
    storage.ensure_user("alice")
    entity = Entity(id=new_id("ent"), user_id="alice", name="CIDR-ПОДПИСКА", entity_type="thing")
    storage.create_entity(entity)

    for spelling in ("CIDR-ПОДПИСКА", "CIDR-ПОДПИСКУ", "cidr-подписки"):
        found = storage.find_entity_by_name("alice", spelling)
        assert found is not None, f"{spelling!r} found nothing"
        assert found["id"] == entity.id


def test_existing_nodes_are_not_merged_behind_the_owners_back(storage):
    """Folding changes what a LOOKUP resolves to. Merging two nodes that already
    exist is a decision about the owner's data, and it stays theirs — the pair
    becomes visible as a duplicate candidate instead."""
    storage.ensure_user("alice")
    first = Entity(id=new_id("ent"), user_id="alice", name="CIDR-ПОДПИСКА", entity_type="thing")
    second = Entity(id=new_id("ent"), user_id="alice", name="CIDR-ПОДПИСКУ", entity_type="thing")
    storage.create_entity(first)
    storage.create_entity(second)

    rows = storage.execute(
        "SELECT id, normalized_name FROM entities WHERE user_id='alice' AND deleted_at IS NULL"
    ).fetchall()
    assert len(rows) == 2, "an existing node disappeared"
    assert len({row["normalized_name"] for row in rows}) == 1, "the pair is not recognised as one name"
