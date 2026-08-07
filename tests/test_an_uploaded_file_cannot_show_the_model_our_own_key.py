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

from friday.documents import SECRET_PLACEHOLDER, DocumentExtractor

# Форма настоящего ключа: длиннее порога `secret_hygiene` (20 знаков).
SECRET = "sk-friday-9f2b7c41a8de5063b7"


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
