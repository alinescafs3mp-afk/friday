"""Собственный credential Friday не доезжает до модели внутри документа.

`secret_hygiene` — offline-доктор: он ищет ключи этого экземпляра в файлах НА
ДИСКЕ и говорит владельцу, где они лежат. Путь чата он не трогал вовсе, поэтому
PDF или docx с ключом внутри превращался в обычный текст и уезжал в контекст
модели, в архив и в поисковый индекс — и дальше в любой ответ, который его
процитирует, и в любую выгрузку.

Ворота стоят на `DocumentExtractor.extract` — это ЕДИНСТВЕННАЯ дорога, по которой
байты файла становятся текстом: и приход из чата, и повторный разбор при
подтверждении из Inbox, и документ, скачанный из сети. Ворота на одной из трёх
дорог не охраняли бы ничего.

Умолчание закрытое: `secret_values=None` означает «взять свои из окружения», а не
«секретов нет». Забыть можно только в одну сторону, и эта сторона — утечка.
"""

from __future__ import annotations

import base64
import io
import zipfile
from dataclasses import replace

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.api.projections import public_conversation_message
from friday.documents import SECRET_PLACEHOLDER, DocumentExtractor
from friday.permissions import ActorContext

# Форма настоящего ключа: длиннее порога `secret_hygiene` (20 знаков).
SECRET = "sk-friday-9f2b7c41a8de5063b7"
FRIDAY_API_TOKEN = "jrc_" + "Ab0_-xYz9" * 4 + "QrsTuvw"


def _extractor(*, secrets=(SECRET,)) -> DocumentExtractor:
    return DocumentExtractor(secret_values=secrets)


def test_a_key_inside_a_document_never_becomes_text() -> None:
    body = f"Договор №14.\nДоступ к сервису: {SECRET}\nПодпись.".encode()
    result = _extractor().extract(body, "договор.txt", "text/plain")

    assert SECRET not in result.text, "ключ уехал бы в контекст модели и в архив"
    assert SECRET_PLACEHOLDER in result.text
    # Остальной документ цел: убран credential, а не абзац вокруг него.
    assert "Договор №14." in result.text
    assert "Подпись." in result.text


def test_the_loss_is_named_not_silent() -> None:
    """Человеку важно знать, что его ключ лежит в файле, который он загрузил."""
    body = f"{SECRET} и ещё раз {SECRET}".encode()
    result = _extractor().extract(body, "заметка.txt", "text/plain")

    assert result.metadata.get("secrets_redacted") == 2, result.metadata


def test_a_document_without_secrets_is_untouched() -> None:
    """Ни подмены, ни лишней пометки: обычный документ проходит как проходил."""
    body = "Обычный документ без единого ключа.".encode()
    result = _extractor().extract(body, "обычный.txt", "text/plain")

    assert result.text.strip() == "Обычный документ без единого ключа."
    assert "secrets_redacted" not in result.metadata


def test_the_default_takes_this_instance_own_credentials(monkeypatch) -> None:
    """Ключевая проверка: умолчание закрытое.

    Конструктор без `secret_values` обязан взять credential из окружения. Если бы
    умолчанием было «секретов нет», защита работала бы ровно там, где о ней
    вспомнили, — то есть нигде.
    """
    monkeypatch.setenv("FRIDAY_API_TOKEN", SECRET)
    result = DocumentExtractor().extract(f"Токен: {SECRET}".encode(), "утечка.txt", "text/plain")

    assert SECRET not in result.text, "конструктор по умолчанию не знал своих credential"


def test_a_short_value_is_not_treated_as_a_secret(monkeypatch) -> None:
    """Порог длины не наш, а `secret_hygiene`: ниже него совпадение случайно.

    Без порога любое короткое значение переменной окружения вычищало бы из
    документов обычные слова.
    """
    monkeypatch.setenv("FRIDAY_API_TOKEN", "короткий")
    result = DocumentExtractor().extract("слово короткий внутри".encode(), "текст.txt", "text/plain")

    assert "короткий" in result.text


def test_the_key_is_removed_before_truncation() -> None:
    """Обрез не должен резать секрет пополам и оставлять половину.

    Порядок «сначала убрать, потом обрезать» — не косметика: обратный порядок
    оставил бы в тексте кусок ключа, достаточный для его узнавания.
    """
    filler = "а" * 30_000
    body = f"{filler}{SECRET}{filler}".encode()
    extractor = DocumentExtractor(secret_values=(SECRET,), max_text_chars=10_000)
    result = extractor.extract(body, "длинный.txt", "text/plain")

    assert SECRET[:12] not in result.text
    assert result.metadata.get("secrets_redacted") == 1


def test_a_structural_friday_api_token_is_removed_without_knowing_its_value() -> None:
    """A user's Friday token is secret even when this instance did not mint it."""
    result = DocumentExtractor(secret_values=()).extract(
        f"до {FRIDAY_API_TOKEN} после".encode(),
        "credential.txt",
        "text/plain",
    )

    assert result.success is True
    assert FRIDAY_API_TOKEN not in result.text
    assert result.text.strip() == f"до {SECRET_PLACEHOLDER} после"
    assert result.metadata.get("secrets_redacted") == 1


def test_a_structural_friday_api_token_is_removed_from_a_nested_archive() -> None:
    """Recursive previews cannot smuggle a token past the top-level boundary."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.txt", f"доступ: {FRIDAY_API_TOKEN}")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested.zip", inner.getvalue())

    result = DocumentExtractor(secret_values=()).extract(outer.getvalue(), "bundle.zip")

    assert result.success is True
    assert FRIDAY_API_TOKEN not in result.text
    assert SECRET_PLACEHOLDER in result.text


def test_a_short_jrc_label_remains_ordinary_document_text() -> None:
    result = DocumentExtractor(secret_values=()).extract(
        b"labels: jrc_demo and jrc_short_test_word",
        "labels.txt",
        "text/plain",
    )

    assert "jrc_demo" in result.text
    assert "jrc_short_test_word" in result.text
    assert "secrets_redacted" not in result.metadata


def test_a_historical_message_projection_masks_the_token_without_rewriting_storage() -> None:
    row = {
        "role": "assistant",
        "content": f"Старый ответ: {FRIDAY_API_TOKEN}",
        "created_at": "2026-08-13T16:16:00Z",
    }

    public = public_conversation_message(row)

    assert FRIDAY_API_TOKEN not in public["content"]
    assert "[redacted:token]" in public["content"]
    assert FRIDAY_API_TOKEN in row["content"], "the historical audit row was mutated"


@pytest.mark.asyncio
async def test_an_old_indexed_friday_token_is_redacted_again_at_the_chat_boundary(
    settings,
    storage,
    monkeypatch,
) -> None:
    """Rows indexed before extractor hardening still cannot be printed outward."""

    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage)

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {"content": f"Старый индекс: {FRIDAY_API_TOKEN}", "tools_used": []}

    carried: list[str] = []

    async def make_file(request, answer, actor, **kwargs):  # noqa: ANN001
        del request, actor, kwargs
        carried.append(answer)
        return {
            "kind": "document",
            "filename": "safe.txt",
            "mime_type": "text/plain",
            "content_base64": base64.b64encode(answer.encode()).decode(),
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", make_file)
    result = await runtime.chat(
        "alice",
        "Создай Word-файл по старой записи",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
    )

    assert carried and FRIDAY_API_TOKEN not in carried[0]
    assert FRIDAY_API_TOKEN not in result["message"]
    assert "[redacted:token]" in result["message"]
    assert result["files"]
    delivered = base64.b64decode(result["files"][0]["content_base64"]).decode()
    assert FRIDAY_API_TOKEN not in delivered
    stored = storage.get_message(str(result["message_id"]), "alice")
    assert stored is not None
    assert FRIDAY_API_TOKEN not in str(stored["content"])
