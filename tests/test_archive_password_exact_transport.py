"""Archive credentials survive transport exactly and stay request-ephemeral."""

from __future__ import annotations

import json
import unicodedata

import pytest

from friday.archive_passwords import archive_password_candidates, strip_archive_password_directives
from friday.documents import ArchivePasswordInvalid, DocumentExtractor, DocumentResult
from friday.telegram_bridge import TelegramBridge, TelegramConfig

_EXACT_PASSWORD = "  Cafe\u0301-🔐  "


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _archive_update(update_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": 5001},
            "from": {"id": 1001},
            "caption": "Проверь архив",
            "document": {
                "file_id": "telegram-exact-password-archive",
                "file_unique_id": "telegram-exact-password-unique",
                "file_name": "protected.rar",
                "mime_type": "application/vnd.rar",
                "file_size": 1024,
            },
        },
    }


def test_directive_quotes_preserve_inner_whitespace_and_plain_tail() -> None:
    safe, quoted = strip_archive_password_directives('Проверь архив, пароль: "  Cafe\u0301-🔐  "')
    assert safe == "Проверь архив"
    assert quoted == _EXACT_PASSWORD

    safe, ordinary = strip_archive_password_directives("пароль: ordinary-tail  ")
    assert safe == ""
    assert ordinary == "ordinary-tail  "


def test_password_candidates_are_exact_first_and_only_transport_variants() -> None:
    wrapped = f'"{_EXACT_PASSWORD}"'
    candidates = archive_password_candidates(wrapped)

    assert candidates[0] == wrapped
    assert candidates[1] == _EXACT_PASSWORD
    assert unicodedata.normalize("NFC", wrapped) in candidates
    assert unicodedata.normalize("NFC", _EXACT_PASSWORD) in candidates
    assert len(candidates) <= 6


def test_extractor_candidates_are_exact_first_with_one_budget_and_deadline(monkeypatch) -> None:
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=5)
    seen: list[tuple[str | None, int, float | None]] = []

    def fake_extract_archive(
        _content,
        _filename,
        _ext,
        _depth,
        budget,
        deadline,
        password,
    ):
        seen.append((password, id(budget), deadline))
        if len(seen) == 1:
            assert budget.take_preview() is True
            raise ArchivePasswordInvalid
        return DocumentResult(
            " | ".join(archive_password_candidates(wrapped)),
            {"format": "rar", "encrypted": True},
        )

    monkeypatch.setattr(extractor, "_extract_archive", fake_extract_archive)
    wrapped = f'"{_EXACT_PASSWORD}"'

    result = extractor.extract(b"synthetic-rar", "protected.rar", archive_password=wrapped)

    assert result.success is True
    assert [item[0] for item in seen] == [wrapped, _EXACT_PASSWORD]
    assert len({item[1] for item in seen}) == 1
    assert len({item[2] for item in seen}) == 1
    serialized = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
    assert all(candidate not in serialized for candidate in archive_password_candidates(wrapped))
    assert result.metadata["secrets_redacted"] >= 1
    assert not any("candidate" in key or "password" in key for key in result.metadata)


@pytest.mark.asyncio
async def test_pending_followup_reaches_backend_exactly_and_success_clears_pending(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    backend_payloads: list[dict] = []
    backend_responses = [
        {
            "message": "Нужен пароль",
            "archive_password_required": True,
            "file_ingestion": {"archive_password_required": True, "persisted": False},
        },
        {"message": "Архив прочитан"},
    ]

    async def prepare(_telegram, message, _update):  # noqa: ANN001
        descriptor = message["document"]
        return {
            "filename": descriptor["file_name"],
            "mime_type": descriptor["mime_type"],
            "content_base64": "UmFyIQ==",
            "source_ref": f"telegram-file:{descriptor['file_id']}",
            "media_kind": "document",
        }

    async def backend(_client, method, path, payload, _user, _chat):  # noqa: ANN001
        assert method == "POST" and path == "/api/chat"
        backend_payloads.append(dict(payload))
        return backend_responses.pop(0)

    async def send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend)
    monkeypatch.setattr(bridge, "_send_message", send)

    try:
        await bridge._process_update(  # noqa: SLF001
            object(), object(), _archive_update(5101), cached_response=None
        )
        assert bridge._inbox.archive_password_challenge(5001, 1001) is not None  # noqa: SLF001

        followup = {
            "update_id": 5102,
            "message": {
                "message_id": 5102,
                "chat": {"id": 5001},
                "from": {"id": 1001},
                "text": _EXACT_PASSWORD,
            },
        }
        safe = bridge._sanitize_update_before_store(followup)  # noqa: SLF001
        assert safe["message"]["text"] == ""
        assert bridge._inbox.store(safe) is True  # noqa: SLF001
        await bridge._process_update(object(), object(), safe, cached_response=None)  # noqa: SLF001

        assert backend_payloads[0].get("archive_password") is None
        assert backend_payloads[1]["archive_password"] == _EXACT_PASSWORD
        assert bridge._inbox.archive_password_challenge(5001, 1001) is None  # noqa: SLF001
        database_dump = "\n".join(bridge._inbox._conn.iterdump())  # noqa: SLF001
        assert _EXACT_PASSWORD not in database_dump
    finally:
        bridge._archive_passwords.clear()  # noqa: SLF001
        bridge._inbox.close()  # noqa: SLF001
