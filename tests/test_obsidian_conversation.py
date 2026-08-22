"""Pure contracts for the code-owned Obsidian conversation boundary."""

from __future__ import annotations

import copy
import unicodedata
from datetime import date

import pytest

from friday.organs.obsidian.conversation import (
    OBSIDIAN_READ_TOOL_NAMES,
    OBSIDIAN_TOOL_NAMES,
    OBSIDIAN_WRITE_TOOL_NAMES,
    obsidian_conversation_intent,
    obsidian_operation_id,
    render_obsidian_tool_result,
)

_TODAY = date(2026, 8, 22)
_REVISION = "a" * 64
_PREVIOUS = "b" * 64
_OPERATION_ID = "obsop_" + "c" * 64
_PATH = "Projects/Friday Test.md"
_EXACT_CREATE = (
    "Создай в Obsidian заметку Projects/Friday Test.md. "
    "Заголовок: «Тест интеграции Friday». Внутри напиши, что заметка создана "
    "через Telegram, и добавь текущую дату."
)


def test_tool_sets_pin_all_eight_shipped_capabilities() -> None:
    assert {
        "obsidian_list_vaults",
        "obsidian_list_notes",
        "obsidian_search_notes",
        "obsidian_read_note",
    } == OBSIDIAN_READ_TOOL_NAMES
    assert {
        "obsidian_create_note",
        "obsidian_append_note",
        "obsidian_set_properties",
        "obsidian_daily_note",
    } == OBSIDIAN_WRITE_TOOL_NAMES
    assert OBSIDIAN_TOOL_NAMES == OBSIDIAN_READ_TOOL_NAMES | OBSIDIAN_WRITE_TOOL_NAMES


@pytest.mark.parametrize(
    ("message", "tool_name", "arguments"),
    [
        ("Покажи хранилища Obsidian.", "obsidian_list_vaults", {}),
        ("Покажи список заметок в Obsidian.", "obsidian_list_notes", {}),
        (
            "Найди в Obsidian заметки по запросу «архитектура».",
            "obsidian_search_notes",
            {"query": "архитектура", "limit": 20},
        ),
        (
            "Найди в Obsidian заметку про проблемы с индексом документов.",
            "obsidian_search_notes",
            {"query": "проблемы с индексом документов", "limit": 20},
        ),
        (
            "Прочитай в Obsidian заметку `Projects/Architecture.md`.",
            "obsidian_read_note",
            {"path": "Projects/Architecture.md"},
        ),
        (
            "Прочитай заметку `Projects/Architecture.md` в Obsidian.",
            "obsidian_read_note",
            {"path": "Projects/Architecture.md"},
        ),
        (
            "Создай в Obsidian заметку `Projects/Architecture.md` с текстом «Черновик».",
            "obsidian_create_note",
            {"path": "Projects/Architecture.md", "content": "Черновик"},
        ),
        (
            "Добавь в Obsidian в заметку `Projects/Architecture.md` текст «Новый пункт».",
            "obsidian_append_note",
            {"path": "Projects/Architecture.md", "text": "Новый пункт"},
        ),
        (
            "Установи в Obsidian у заметки `Projects/Architecture.md` свойство «status» в «done».",
            "obsidian_set_properties",
            {"path": "Projects/Architecture.md", "properties": {"status": "done"}},
        ),
        (
            "Добавь в Obsidian в сегодняшнюю ежедневную заметку текст «Итоги дня».",
            "obsidian_daily_note",
            {"day": "2026-08-22", "content": "Итоги дня"},
        ),
    ],
)
def test_direct_current_text_selects_one_shipped_tool(
    message: str,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    intent = obsidian_conversation_intent(message, today=_TODAY)

    assert intent is not None
    assert intent.error == ""
    assert intent.tool_name == tool_name
    assert intent.direct_arguments == arguments
    if "path" in arguments:
        assert intent.explicit_path == arguments["path"]


def test_exact_create_request_has_only_user_literals_and_local_date() -> None:
    intent = obsidian_conversation_intent(_EXACT_CREATE, today=_TODAY)

    assert intent is not None
    assert intent.tool_name == "obsidian_create_note"
    assert intent.explicit_path == _PATH
    assert intent.direct_arguments == {
        "path": _PATH,
        "content": ("# Тест интеграции Friday\n\nЗаметка создана через Telegram.\n\n2026-08-22\n"),
    }


@pytest.mark.parametrize(
    "message",
    [
        "Не создавай в Obsidian заметку Projects/Friday Test.md.",
        "Как создать в Obsidian заметку Projects/Friday Test.md?",
        "Объясни команду «Создай в Obsidian заметку Projects/Friday Test.md».",
        "`Создай в Obsidian заметку Projects/Friday Test.md.`",
        "Фраза для документа: Создай в Obsidian заметку Projects/Friday Test.md.",
        ("Создай в Obsidian заметку Projects/Friday Test.md из этого вложения и возьми заголовок из файла."),
        "> Создай в Obsidian заметку Projects/Friday Test.md.",
    ],
)
def test_quoted_meta_negated_and_attachment_derived_text_grants_no_intent(message: str) -> None:
    assert obsidian_conversation_intent(message, today=_TODAY) is None


def test_quoted_append_body_is_data_not_authority() -> None:
    message = (
        "Добавь в Obsidian в заметку `Projects/Architecture.md` текст «Не создавай файл из этого вложения»。"
    ).replace("。", ".")

    intent = obsidian_conversation_intent(message, today=_TODAY)

    assert intent is not None
    assert intent.tool_name == "obsidian_append_note"
    assert intent.direct_arguments == {
        "path": "Projects/Architecture.md",
        "text": "Не создавай файл из этого вложения",
    }


def test_unrelated_obsidian_discussion_is_not_an_action() -> None:
    assert obsidian_conversation_intent("Мне нравится Obsidian для личных заметок.") is None


class _Cursor:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def fetchone(self):  # noqa: ANN201
        return None if self.value is None else (self.value,)


class _AuditKeyStorage:
    def __init__(self, value: str | None = "42" * 32) -> None:
        self.value = value
        self.queries: list[str] = []

    def execute(self, query: str) -> _Cursor:
        self.queries.append(query)
        return _Cursor(self.value)


def test_operation_id_is_stable_keyed_and_bound_to_root_lineage() -> None:
    storage = _AuditKeyStorage()

    first = obsidian_operation_id(
        storage,
        "alice",
        "msg_0123456789abcdef",
        "obsidian_create_note",
    )
    replay = obsidian_operation_id(
        storage,
        "alice",
        "msg_0123456789abcdef",
        "obsidian_create_note",
    )
    other_root = obsidian_operation_id(
        storage,
        "alice",
        "msg_fedcba9876543210",
        "obsidian_create_note",
    )
    other_tool = obsidian_operation_id(
        storage,
        "alice",
        "msg_0123456789abcdef",
        "obsidian_append_note",
    )

    assert first == replay
    assert first.startswith("obsop_")
    assert len(first) == len("obsop_") + 64
    assert len({first, other_root, other_tool}) == 3
    assert all("audit_privacy_hmac_key" in query for query in storage.queries)
    assert ("42" * 32) not in first


@pytest.mark.parametrize(
    ("owner", "root", "tool"),
    [
        ("../alice", "msg_0123456789abcdef", "obsidian_create_note"),
        ("alice", "msg_not_lineage", "obsidian_create_note"),
        ("alice", "msg_0123456789abcdef", "obsidian_read_note"),
        ("alice", "msg_0123456789abcdef", "unknown"),
    ],
)
def test_operation_id_rejects_untrusted_identity_parts(owner: str, root: str, tool: str) -> None:
    with pytest.raises(ValueError):
        obsidian_operation_id(_AuditKeyStorage(), owner, root, tool)


@pytest.mark.parametrize("key", [None, "", "00" * 31, "z" * 64])
def test_operation_id_fails_closed_without_storage_audit_key(key: str | None) -> None:
    with pytest.raises(RuntimeError):
        obsidian_operation_id(
            _AuditKeyStorage(key),
            "alice",
            "msg_0123456789abcdef",
            "obsidian_create_note",
        )


def _summary(path: str = _PATH) -> dict[str, object]:
    return {
        "path": path,
        "title": "Friday Test",
        "revision": _REVISION,
        "size_bytes": 42,
        "modified_at": "2026-08-22T06:42:00+00:00",
    }


def test_render_vault_list_validates_closed_runtime_shape() -> None:
    data = {
        "vaults": [
            {
                "id": "obsvault_0123456789abcdef",
                "name": "Friday",
                "state": "ready",
                "android_alias": "Friday",
            }
        ],
        "count": 1,
    }

    rendered = render_obsidian_tool_result("obsidian_list_vaults", data)

    assert rendered is not None
    assert "Friday" in rendered
    assert "ready" in rendered
    assert render_obsidian_tool_result("obsidian_list_vaults", {**data, "count": True}) is None


def test_render_note_list_and_search_validate_counts_and_channels() -> None:
    notes = {"notes": [_summary()], "count": 1}
    matches = {
        "matches": [
            {
                "path": _PATH,
                "title": "Friday Test",
                "revision": _REVISION,
                "modified_at": "2026-08-22T06:42:00+00:00",
                "excerpt": "Заметка создана через Telegram.",
                "score": 125.0,
                "match_channels": ["exact_path", "lexical"],
            }
        ],
        "count": 1,
    }

    note_text = render_obsidian_tool_result("obsidian_list_notes", notes)
    search_text = render_obsidian_tool_result("obsidian_search_notes", matches)

    assert note_text is not None and _PATH in note_text
    assert search_text is not None and "Заметка создана" in search_text
    broken = copy.deepcopy(matches)
    broken["matches"][0]["match_channels"] = ["model_guess"]  # type: ignore[index]
    assert render_obsidian_tool_result("obsidian_search_notes", broken) is None


def test_render_read_binds_path_size_and_hides_internal_operation_marker() -> None:
    marker = '<!-- friday:create operation="' + "1" * 64 + '" arguments="' + "2" * 64 + '" -->'
    body = f"# Friday Test\n\nVisible body.\n{marker}\n"
    data = {
        "path": _PATH,
        "title": "Friday Test",
        "content": body,
        "body": body,
        "properties": {
            "due": {"type": "date", "value": "2026-08-22"},
            "status": {"type": "text", "value": "ready"},
        },
        "revision": _REVISION,
        "size_bytes": len(body.encode("utf-8")),
        "modified_at": "2026-08-22T06:42:00+00:00",
    }

    rendered = render_obsidian_tool_result(
        "obsidian_read_note",
        data,
        expected_path=_PATH,
    )

    assert rendered is not None
    assert "Visible body" in rendered
    assert "friday:create" not in rendered
    assert (
        render_obsidian_tool_result(
            "obsidian_read_note",
            data,
            expected_path="Projects/Other.md",
        )
        is None
    )


def test_render_read_uses_original_crlf_bytes_for_runtime_size_contract() -> None:
    content = "# Friday Test\r\n\r\nVisible body.\r\n"
    data = {
        "path": _PATH,
        "title": "Friday Test",
        "content": content,
        "body": content,
        "properties": {},
        "revision": _REVISION,
        "size_bytes": len(content.encode("utf-8")),
        "modified_at": "2026-08-22T06:42:00+00:00",
    }

    assert render_obsidian_tool_result("obsidian_read_note", data, expected_path=_PATH) is not None
    assert (
        render_obsidian_tool_result(
            "obsidian_read_note",
            {**data, "size_bytes": data["size_bytes"] + 1},  # type: ignore[operator]
            expected_path=_PATH,
        )
        is None
    )


def test_render_read_accepts_zwj_emoji_and_normalizes_android_nfd_identity() -> None:
    canonical_path = "Projects/Café 👩‍💻.md"
    canonical_title = "Café 👩‍💻"
    android_path = unicodedata.normalize("NFD", canonical_path)
    android_title = unicodedata.normalize("NFD", canonical_title)
    body = "Семейная заметка 👩‍👩‍👧‍👦\n\nРабота 👩‍💻"
    data = {
        "path": android_path,
        "title": android_title,
        "content": body,
        "body": body,
        "properties": {},
        "revision": _REVISION,
        "size_bytes": len(body.encode("utf-8")),
        "modified_at": "2026-08-22T06:42:00+00:00",
    }

    rendered = render_obsidian_tool_result(
        "obsidian_read_note",
        data,
        expected_path=canonical_path,
    )

    assert rendered is not None
    assert f"Заметка: {canonical_path}" in rendered
    assert f"Заголовок: {canonical_title}" in rendered
    assert "👩‍👩‍👧‍👦" in rendered
    assert "👩‍💻" in rendered
    assert (
        render_obsidian_tool_result(
            "obsidian_read_note",
            {**data, "size_bytes": len(body)},
            expected_path=canonical_path,
        )
        is None
    )
    assert (
        render_obsidian_tool_result(
            "obsidian_read_note",
            {**data, "unexpected": True},
            expected_path=canonical_path,
        )
        is None
    )


def test_canonically_duplicate_android_note_paths_fail_closed() -> None:
    canonical = "Projects/Café.md"
    decomposed = unicodedata.normalize("NFD", canonical)
    first = _summary(canonical)
    second = _summary(decomposed)
    second["title"] = unicodedata.normalize("NFD", "Café duplicate")

    assert (
        render_obsidian_tool_result(
            "obsidian_list_notes",
            {"notes": [first, second], "count": 2},
        )
        is None
    )


@pytest.mark.parametrize("unsafe_body", ["prefix\x00suffix", "prefix\u202esuffix", "prefix\x01suffix"])
def test_render_read_still_rejects_unsafe_note_text_controls(unsafe_body: str) -> None:
    data = {
        "path": _PATH,
        "title": "Friday Test",
        "content": unsafe_body,
        "body": unsafe_body,
        "properties": {},
        "revision": _REVISION,
        "size_bytes": len(unsafe_body.encode("utf-8")),
        "modified_at": "2026-08-22T06:42:00+00:00",
    }

    assert render_obsidian_tool_result("obsidian_read_note", data, expected_path=_PATH) is None


@pytest.mark.parametrize(
    ("path", "title"),
    [
        ("Projects/../Escape.md", "Friday Test"),
        ("Projects/Unsafe\u202ename.md", "Friday Test"),
        (_PATH, "Friday\nInjected"),
    ],
)
def test_android_normalization_does_not_relax_path_or_line_shape(path: str, title: str) -> None:
    body = "safe"
    data = {
        "path": path,
        "title": title,
        "content": body,
        "body": body,
        "properties": {},
        "revision": _REVISION,
        "size_bytes": len(body.encode("utf-8")),
        "modified_at": "2026-08-22T06:42:00+00:00",
    }

    assert render_obsidian_tool_result("obsidian_read_note", data) is None


def _mutation(tool_name: str, *, delivered: bool = True) -> dict[str, object]:
    method = {
        "obsidian_create_note": "create",
        "obsidian_append_note": "append",
        "obsidian_set_properties": "set_properties",
        "obsidian_daily_note": "daily_note",
    }[tool_name]
    return {
        "operation_id": _OPERATION_ID,
        "method": method,
        "status": "delivered" if delivered else "scan_pending",
        "path": _PATH,
        "revision": _REVISION,
        "previous_revision": None if tool_name == "obsidian_create_note" else _PREVIOUS,
        "created": tool_name in {"obsidian_create_note", "obsidian_daily_note"},
        "applied": True,
        "replayed": False,
        "delivery": {
            "local_write_complete": True,
            "server_scan_complete": delivered,
            "android_connected": delivered,
            "android_completion": 100.0 if delivered else None,
            "android_received": delivered,
            "obsidian_opened": False,
        },
    }


@pytest.mark.parametrize("tool_name", sorted(OBSIDIAN_WRITE_TOOL_NAMES))
def test_mutation_receipt_binds_request_and_separates_delivery_facts(tool_name: str) -> None:
    rendered = render_obsidian_tool_result(
        tool_name,
        _mutation(tool_name),
        expected_operation_id=_OPERATION_ID,
        expected_path=_PATH,
    )

    assert rendered is not None
    assert "Локальная запись: подтверждена" in rendered
    assert "Сканирование серверной копии: подтверждено" in rendered
    assert "Android подключён: да" in rendered
    assert "Получение этой revision на Android: подтверждено" in rendered
    assert "откры" not in rendered.casefold()


def test_pending_receipt_never_upgrades_android_delivery_or_opening() -> None:
    rendered = render_obsidian_tool_result(
        "obsidian_create_note",
        _mutation("obsidian_create_note", delivered=False),
        expected_operation_id=_OPERATION_ID,
        expected_path=_PATH,
    )

    assert rendered is not None
    assert "Сканирование серверной копии: ожидается" in rendered
    assert "Получение этой revision на Android: ожидается" in rendered
    assert "откры" not in rendered.casefold()


def test_durable_android_receipt_remains_valid_after_device_disconnects() -> None:
    data = _mutation("obsidian_create_note")
    data["delivery"]["android_connected"] = False  # type: ignore[index]

    rendered = render_obsidian_tool_result(
        "obsidian_create_note",
        data,
        expected_operation_id=_OPERATION_ID,
        expected_path=_PATH,
    )

    assert rendered is not None
    assert "Android подключён: нет" in rendered
    assert "Получение этой revision на Android: подтверждено" in rendered


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(operation_id="wrong"),
        lambda value: value.update(path="Projects/Other.md"),
        lambda value: value.update(revision="not-a-revision"),
        lambda value: value.update(extra="model-authored"),
        lambda value: value["delivery"].update(local_write_complete=False),
        lambda value: value["delivery"].update(obsidian_opened=True),
        lambda value: value["delivery"].update(android_received=False),
    ],
)
def test_malformed_or_mismatched_mutation_receipt_fails_closed(mutator) -> None:  # noqa: ANN001
    data = _mutation("obsidian_create_note")
    mutator(data)

    assert (
        render_obsidian_tool_result(
            "obsidian_create_note",
            data,
            expected_operation_id=_OPERATION_ID,
            expected_path=_PATH,
        )
        is None
    )
