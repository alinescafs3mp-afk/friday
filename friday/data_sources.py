"""Внешние базы данных как ИСТОЧНИК, а не как хранилище.

Заказ владельца (2026-08-05): «научить Пятницу ходить за данными в какую-то СУБД
по сети — postgres, mysql, или ещё что». Это НЕ переезд: собственный архив
остаётся там же, где был, и local-first не нарушается. Внешняя база — такой же
источник, как интернет или загруженный файл, только с расписанной схемой.

Решения, принятые здесь, и цена каждого:

**Учётные данные не хранятся в базе.** Источник объявляет ИМЯ переменной
окружения, в которой лежит строка подключения; сама строка не попадает ни в
`data_sources`, ни в бэкапы, ни в экспорт аккаунта, ни в ответ API. Иначе пароль
от чужой боевой базы уехал бы в файл резервной копии — а копии лежат рядом с
архивом и переживают всё.

**Только чтение, и это проверяется НЕ доверием.** Разрешён ровно один оператор
`SELECT` (или `WITH … SELECT`), без второго через точку с запятой, без DDL и DML.
Проверка структурная: разбирается сам текст запроса, а не намерение того, кто его
прислал. Плюс, где драйвер это умеет, соединение открывается в режиме только для
чтения.

**Потолки обязательны и называются вслух.** Строк — не больше `_MAX_ROWS`,
время — не дольше `_TIMEOUT_SEC`. Обрез не молчит: в ответе стоит признак
`truncated`, потому что «первые 200 строк» и «всего 200 строк» — разные факты, и
молчаливый обрез читается как второе.

**SQLite работает без зависимостей** (он и так в стандартной библиотеке), а
postgres и mysql — необязательный extra `friday[sql]`. Ядро запускается без них;
источник неустановленного вида честно говорит, чего не хватает, а не падает
непонятной ошибкой импорта.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

#: Сколько строк отдаём максимум. Не настройка: это защита контекста модели и
#: памяти процесса, а не предпочтение.
_MAX_ROWS = 200
#: Сколько ждём ответа внешней базы. Она чужая и может быть занята.
_TIMEOUT_SEC = 15

#: Виды источников, которые система умеет открывать.
SOURCE_KINDS: tuple[str, ...] = ("sqlite", "postgres", "mysql")

#: Что должно быть установлено для каждого вида. SQLite — стандартная библиотека.
_DRIVERS: dict[str, tuple[str, str]] = {
    "postgres": ("psycopg", "friday[sql]"),
    "mysql": ("pymysql", "friday[sql]"),
}

#: Запрос обязан быть ОДНИМ чтением. Комментарии срезаются до проверки: иначе
#: `SELECT 1 -- ; DROP TABLE` выглядит как один оператор, а на сервере им не
#: является.
_COMMENT = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)
_READ_ONLY_START = re.compile(r"^\s*(?:with\b|select\b)", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(?:insert|update|delete|drop|alter|create|truncate|grant|revoke|attach|"
    r"detach|pragma|vacuum|call|copy|merge|replace|set)\b",
    re.IGNORECASE,
)


class UnsafeQueryError(ValueError):
    """Запрос не является одним чтением."""


class SourceUnavailableError(RuntimeError):
    """Источник объявлен, но открыть его нечем или незачем."""


@dataclass(frozen=True)
class DataSource:
    """Объявленный источник. Строки подключения здесь НЕТ и быть не должно."""

    name: str
    kind: str
    dsn_env: str
    description: str = ""

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,40}", self.name or ""):
            raise ValueError("Имя источника: строчные буквы, цифры, дефис, подчёркивание")
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"Неизвестный вид источника {self.kind!r}; известны {list(SOURCE_KINDS)}")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,60}", self.dsn_env or ""):
            raise ValueError("Имя переменной окружения: заглавные буквы, цифры, подчёркивание")


def assert_read_only(query: str) -> str:
    """Вернуть запрос, если это ровно одно чтение. Иначе — отказ с причиной.

    Проверяется ТЕКСТ, а не намерение: инструмент зовёт модель, и «я же просил
    только посмотреть» здесь не аргумент. Комментарии срезаются первыми — иначе
    `SELECT 1 -- ; DROP TABLE users` проходит как один оператор.
    """

    text = _COMMENT.sub(" ", str(query or "")).strip().rstrip(";").strip()
    if not text:
        raise UnsafeQueryError("Пустой запрос")
    if ";" in text:
        raise UnsafeQueryError("Разрешён ровно один запрос: точка с запятой внутри запрещена")
    if not _READ_ONLY_START.match(text):
        raise UnsafeQueryError("Разрешено только чтение: запрос обязан начинаться с SELECT или WITH")
    found = _FORBIDDEN.search(text)
    if found:
        raise UnsafeQueryError(f"Разрешено только чтение: слово «{found.group(0)}» запрещено")
    return text


def _require_driver(kind: str) -> Any:
    module_name, extra = _DRIVERS.get(kind, ("", ""))
    if not module_name:
        return None
    try:
        return __import__(module_name)
    except ImportError as error:  # pragma: no cover - зависит от установки
        raise SourceUnavailableError(
            f"Источник вида «{kind}» требует пакета {module_name}: установите `pip install {extra}`"
        ) from error


def _rows_from(cursor: Any, columns: list[str]) -> tuple[list[dict[str, Any]], bool]:
    """Строки с потолком. Второй элемент — был ли обрез, и он не молчит."""

    rows = cursor.fetchmany(_MAX_ROWS + 1)
    truncated = len(rows) > _MAX_ROWS
    return [dict(zip(columns, row, strict=False)) for row in rows[:_MAX_ROWS]], truncated


def run_query(source: DataSource, dsn: str, query: str, *, parameters: Any = None) -> dict[str, Any]:
    """Выполнить ОДНО чтение во внешней базе и вернуть строки с провенансом."""

    safe = assert_read_only(query)
    if source.kind == "sqlite":
        # `mode=ro` — не вежливая просьба, а отказ движка на любую запись.
        connection = sqlite3.connect(f"file:{dsn}?mode=ro", uri=True, timeout=_TIMEOUT_SEC)
        try:
            cursor = connection.execute(safe, parameters or ())
            columns = [str(item[0]) for item in (cursor.description or [])]
            rows, truncated = _rows_from(cursor, columns)
        finally:
            connection.close()
    elif source.kind == "postgres":
        psycopg = _require_driver("postgres")
        with psycopg.connect(dsn, connect_timeout=_TIMEOUT_SEC) as connection:  # type: ignore[union-attr]
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute(safe, parameters or ())
                columns = [str(item[0]) for item in (cursor.description or [])]
                rows, truncated = _rows_from(cursor, columns)
    elif source.kind == "mysql":
        pymysql = _require_driver("mysql")
        connection = pymysql.connect(  # type: ignore[union-attr]
            **_mysql_kwargs(dsn), connect_timeout=_TIMEOUT_SEC, read_default_file=None
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(safe, parameters or ())
                columns = [str(item[0]) for item in (cursor.description or [])]
                rows, truncated = _rows_from(cursor, columns)
        finally:
            connection.close()
    else:  # pragma: no cover - закрыто validate()
        raise SourceUnavailableError(f"Неизвестный вид источника {source.kind!r}")
    return {
        "source": source.name,
        "kind": source.kind,
        "query": safe,
        "columns": columns,
        "rows": rows,
        # Обрез назван вслух: «первые 200 строк» и «всего 200 строк» — разные
        # факты, и молчаливый обрез читается как второй.
        "truncated": truncated,
        "row_limit": _MAX_ROWS,
    }


def _mysql_kwargs(dsn: str) -> dict[str, Any]:
    """Разобрать `mysql://user:pass@host:port/db` в аргументы драйвера."""

    from urllib.parse import unquote, urlsplit

    parts = urlsplit(dsn)
    return {
        "host": parts.hostname or "localhost",
        "port": int(parts.port or 3306),
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "database": (parts.path or "/").lstrip("/"),
    }


#: Запросы схемы: что вообще есть в этой базе. Без них модель угадывает имена
#: таблиц, а угаданное имя даёт не пустой ответ, а ОШИБКУ — и выглядит она как
#: «источник не работает».
_SCHEMA_QUERIES: dict[str, str] = {
    "sqlite": (
        "SELECT m.name AS table_name, p.name AS column_name, p.type AS data_type "
        "FROM sqlite_master m JOIN pragma_table_info(m.name) p "
        "WHERE m.type='table' AND m.name NOT LIKE 'sqlite_%' ORDER BY m.name, p.cid"
    ),
    "postgres": (
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
        "ORDER BY table_name, ordinal_position"
    ),
    "mysql": (
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = DATABASE() ORDER BY table_name, ordinal_position"
    ),
}


def describe_source(source: DataSource, dsn: str) -> dict[str, Any]:
    """Таблицы и столбцы источника — то, по чему можно составить запрос."""

    query = _SCHEMA_QUERIES.get(source.kind)
    if not query:  # pragma: no cover - закрыто validate()
        raise SourceUnavailableError(f"Неизвестный вид источника {source.kind!r}")
    # Схема бывает больше потолка строк, и это не обрез данных, а обрез описания:
    # ограничение то же самое, признак `truncated` тот же.
    result = run_query(source, dsn, query)
    tables: dict[str, list[dict[str, str]]] = {}
    for row in result["rows"]:
        table = str(row.get("table_name") or "")
        tables.setdefault(table, []).append(
            {"column": str(row.get("column_name") or ""), "type": str(row.get("data_type") or "")}
        )
    return {
        "source": source.name,
        "kind": source.kind,
        "tables": tables,
        "truncated": result["truncated"],
    }
