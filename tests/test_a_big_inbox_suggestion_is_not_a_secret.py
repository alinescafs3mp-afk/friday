"""Размер предложений не делает строку Inbox приватной (§77).

Предел `_INBOX_PUBLIC_JSON_MAX_BYTES` стоял `8 192` — в 128 раз ниже такого же
предела у Raw/Knowledge, хотя сторожит то же самое: цену обхода `json_tree`.
Строка за пределом объявлялась приватной целиком, а вместе с ней пропадал её Raw
Object. Замерено на архиве владельца: медиана `2 436` байт, p99 `10 537`,
максимум `18 217`; за прежним пределом было `85` строк из `2071`, и ровно они
делали невидимыми `85` Raw Objects.

Размер блоба не говорит ничего о том, копирует ли он приватную личность.
"""

from __future__ import annotations

import pytest

from friday.storage import PrivateMaterialQuarantineError
from friday.storage._privacy import (
    _not_private_inbox_dependency,
    _not_private_raw_dependency,
)
from friday.storage.models import Entity, EntityType, InboxItem, RawObject, new_id

TENANT = "inbox-owner"


def _raw(storage, text: str = "обычная заметка про смету") -> RawObject:
    storage.ensure_user(TENANT)
    return storage.store_raw_object(
        RawObject(new_id("raw"), TENANT, "api", f"note:{new_id('src')}", text, "text")
    )


def _inbox(storage, raw: RawObject, suggestions: dict) -> InboxItem:
    return storage.store_inbox_item(
        InboxItem(
            new_id("inbox"),
            TENANT,
            raw.id,
            suggestions_json=suggestions,
            classification_notes="разбор",
        )
    )


def _wide_suggestions() -> dict:
    """Предложения заметно больше прежних 8 КиБ — как у живых строк владельца."""

    return {"entities": [f"позиция сметы № {index:04d}" for index in range(700)]}


def _inbox_visible(storage, inbox_id: str) -> bool:
    return (
        storage.execute(
            f"SELECT 1 FROM inbox i WHERE i.id=? AND {_not_private_inbox_dependency('i')}",  # nosec B608
            (inbox_id,),
        ).fetchone()
        is not None
    )


def _raw_visible(storage, raw_id: str) -> bool:
    return (
        storage.execute(
            f"SELECT 1 FROM raw_objects r WHERE r.id=? AND {_not_private_raw_dependency('r')}",  # nosec B608
            (raw_id,),
        ).fetchone()
        is not None
    )


def test_a_wide_suggestion_stays_visible(storage) -> None:
    raw = _raw(storage)
    suggestions = _wide_suggestions()
    import json

    assert len(json.dumps(suggestions, ensure_ascii=False).encode("utf-8")) > 8_192

    item = _inbox(storage, raw, suggestions)

    assert _inbox_visible(storage, item.id), "строка разбора пропала из-за своего размера"


def test_the_raw_object_behind_a_wide_suggestion_stays_visible(storage) -> None:
    """Именно это и теряло записи: невидимая строка разбора топила свой источник."""

    raw = _raw(storage)
    _inbox(storage, raw, _wide_suggestions())

    assert _raw_visible(storage, raw.id), "исходная запись исчезла вслед за разбором"


def test_a_wide_suggestion_copying_a_private_name_is_still_hidden(storage) -> None:
    """Размер ничего не отменяет: копия чужой приватной личности прячется."""

    storage.ensure_user(TENANT)
    private = Entity(new_id("ent"), TENANT, "СЕКРЕТНОЕ СОБЫТИЕ f31a", EntityType.EVENT)
    storage.create_entity(private)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, '2026-08-13T00:00:00Z', 'day', 'reminder:somebody-else',
                      '2026-08-07T00:00:00Z')""",
            (private.id, TENANT),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'somebody-else', 'reminder', '2026-08-07T00:00:00Z')""",
            (private.id,),
        )

    raw = _raw(storage)
    wide = _wide_suggestions()
    wide["entities"].append(private.name)
    item = _inbox(storage, raw, wide)

    assert not _inbox_visible(storage, item.id), "приватное имя утекло через большой блоб"


def test_a_note_copying_a_private_name_is_still_refused(storage) -> None:
    """Контроль: сам карантин на месте и по-прежнему отказывает."""

    storage.ensure_user(TENANT)
    private = Entity(new_id("ent"), TENANT, "СЕКРЕТНОЕ СОБЫТИЕ 90cc", EntityType.EVENT)
    storage.create_entity(private)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, '2026-08-13T00:00:00Z', 'day', 'reminder:somebody-else',
                      '2026-08-07T00:00:00Z')""",
            (private.id, TENANT),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'somebody-else', 'reminder', '2026-08-07T00:00:00Z')""",
            (private.id,),
        )

    with pytest.raises(PrivateMaterialQuarantineError):
        _raw(storage, f"напоминание про {private.name} на неделе")
