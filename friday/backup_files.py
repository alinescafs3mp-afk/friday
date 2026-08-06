"""Инкрементальный бэкап оригиналов файлов рядом с бэкапами базы.

Бэкап базы уносит извлечённый текст, но PDF, сканы, фото и голосовые существуют
в ОДНОМ экземпляре на том же диске, что и база, — а файлы через Telegram и есть
главный канал роста архива. Смерть диска means: базу вернёт зеркало, оригиналы —
ничто, и каждая ссылка на файл превратится в 404, неотличимый от «файла нет».

Файлы content-addressed и после приёма неизменяемы, поэтому инкремент — это
«докопируй то, чего в дереве бэкапа ещё нет». Ничего не перезаписывается, и
удаления НЕ распространяются: бэкап, повторяющий удаление, — не бэкап.
Зеркалирование этого дерева offsite — отдельная работа (`backup_mirror` сегодня
ходит только по манифестам БД); пока честный статус лежит в
``workers:last_files_backup`` и поднимается доктором.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from friday.private_fs import (
    copy_private_file,
    ensure_private_directory,
    restrict_private_file,
    restrict_private_tree,
)

LOGGER = logging.getLogger(__name__)

#: Имя файла в хранилище — это sha256 его содержимого (плюс расширение).
_SHA256_NAME = re.compile(r"^([0-9a-f]{64})(\.[a-z0-9]{1,16})?$")


def _digest_from_name(name: str) -> str | None:
    """sha256 из имени файла, если оно так устроено. Иначе — None (проверять нечем)."""
    match = _SHA256_NAME.match(name)
    return match.group(1) if match else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Потолок одного прогона. Первая синхронизация корпуса владельца (~1.3 ГБ на
# SSD) укладывается с большим запасом; потолок существует, чтобы аномалия
# (сетевой диск, деградировавший носитель) не съела бюджет воркера целиком.
_BUDGET_SEC = 300.0


def backup_files_incremental(
    files_dir: Path,
    target_dir: Path,
    *,
    budget_sec: float = _BUDGET_SEC,
) -> dict[str, Any]:
    """Докопировать новые файлы из ``files_dir`` в ``target_dir``. Идемпотентно.

    Возвращает счётчики и признак ``complete``: False означает «бюджет вышел
    раньше файлов» — остаток докопирует следующий суточный прогон.
    """
    if not files_dir.is_dir():
        return {"enabled": False, "reason": "files_dir_missing", "complete": True}
    started = time.monotonic()
    total = copied = pending = failed = repaired = corrupt_sources = 0
    copied_bytes = 0
    restrict_private_tree(target_dir)
    for source in sorted(files_dir.rglob("*")):
        if source.is_symlink() or not source.is_file():
            continue
        total += 1
        relative = source.relative_to(files_dir)
        destination = target_dir / relative
        expected = _digest_from_name(source.name)
        try:
            ensure_private_directory(destination.parent)
            if destination.is_symlink():
                raise ValueError("backup destination cannot be a symlink")
            restrict_private_file(destination)
            source_size = source.stat().st_size
            if destination.is_file() and destination.stat().st_size == source_size:
                # Совпадение размера — не совпадение содержимого. Замерено: один
                # перевёрнутый байт в копии не менял длину, и следующий прогон
                # отчитывался «copied: 0, failed: 0, complete: True», оставляя
                # документ испорченным навсегда. Оригиналы живут в одном
                # экземпляре, и это дерево — единственная вторая копия.
                #
                # Проверка бесплатна: имя файла И ЕСТЬ sha256 его содержимого.
                if expected is None or _sha256_file(destination) == expected:
                    continue
                LOGGER.warning("Копия файла испорчена — перезаписываю из оригинала")
                repaired += 1
            # Испорченный ОРИГИНАЛ поверх годной копии не кладём: иначе зеркало
            # аккуратно увозит порчу и подтверждает её целостность.
            if expected is not None and _sha256_file(source) != expected:
                corrupt_sources += 1
                LOGGER.error("Оригинал файла не сходится со своим sha256 — копия не тронута")
                continue
        except (OSError, ValueError):
            failed += 1
            continue
        if time.monotonic() - started > budget_sec:
            pending += 1
            continue
        try:
            staged = destination.with_name(destination.name + ".part")
            copy_private_file(source, staged)
            if staged.stat().st_size != source_size:
                staged.unlink(missing_ok=True)
                failed += 1
                continue
            # Копию проверяем сразу, пока она в кеше: тогда «скопировано» значит
            # «скопировано верно», а не «столько байт прошло мимо».
            if expected is not None and _sha256_file(staged) != expected:
                staged.unlink(missing_ok=True)
                failed += 1
                LOGGER.error("Копия файла не сошлась по sha256 сразу после записи")
                continue
            os.replace(staged, destination)
            restrict_private_file(destination)
            copied += 1
            copied_bytes += source_size
        except (OSError, ValueError) as exc:
            staged.unlink(missing_ok=True)
            LOGGER.warning("Не удалось скопировать файл в бэкап (%s)", type(exc).__name__)
            failed += 1
    result = {
        "enabled": True,
        "total": total,
        "copied": copied,
        "copied_bytes": copied_bytes,
        "pending": pending,
        "failed": failed,
        # Порча, найденная и починенная, и порча в самих оригиналах — разные вещи,
        # и обе должны быть видны доктору отдельно от «скопировано».
        "repaired": repaired,
        "corrupt_sources": corrupt_sources,
        "complete": pending == 0 and failed == 0 and corrupt_sources == 0,
        "target_dir": str(target_dir),
    }
    if copied or pending or failed:
        LOGGER.info(
            "Бэкап файлов: скопировано %d (%.1f МБ), отложено %d, ошибок %d из %d",
            copied,
            copied_bytes / 1_048_576,
            pending,
            failed,
            total,
        )
    return result
