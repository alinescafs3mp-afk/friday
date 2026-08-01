"""Резидентные матрицы векторов: весь корпус в памяти вместо скана BLOB-ов.

Прежний путь на КАЖДЫЙ запрос читал до 20 000 BLOB-ов (~80 МБ) из SQLite,
склеивал их в матрицу и считал нормы заново — и при этом был ограничен окном
«новейшие N»: на корпусе владельца окно пассажей исчерпано (доктор поднимает
dense_scan_window_near_cap), и следующий импорт молча вытеснил бы из смыслового
поиска старейшие документы.

Кэш держит матрицу и нормы на (арендатор, модель, размерность) и пересобирается
по дешёвой подписи таблиц (счётчики + MAX(updated_at)). Между пересборками —
нулевой I/O на запрос. Свежесть ограничена ``min_reload_interval_sec``: во время
массовой индексации подпись меняется каждый тик, и без этой выдержки каждый
поиск платил бы полную пересборку; вектор, записанный секунды назад, поиску не
обязателен. Устаревшая матрица НЕ воскрешает удалённое: продвижение кандидатов
в retrieval перечитывает объект и отбрасывает deleted_at — кэш лишь источник
скоров, не состава выдачи.

Без numpy кэш не строится (матрица из списков съела бы память без выигрыша) —
путь со сканом BLOB-ов остаётся как был.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

try:  # тот же необязательный numpy, что и в retrieval
    import numpy as _np
except ImportError:  # pragma: no cover - ветка без numpy проверяется отдельно
    _np = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from friday.storage import FridayStorage

LOGGER = logging.getLogger(__name__)


@dataclass
class DenseMatrices:
    """Однажды собранные вектора арендатора: единицы против BLOB-ов на запрос."""

    doc_ids: list[str]
    doc_matrix: Any
    doc_norms: Any
    chunk_ids: list[str]
    chunk_matrix: Any
    chunk_norms: Any
    signature: tuple[Any, ...]
    loaded_monotonic: float
    state: str = "reloaded"  # hit | reloaded | stale — для explain-трейса


def _build(rows: list[tuple[str, bytes]], dim: int) -> tuple[list[str], Any, Any]:
    expected = dim * 4  # float32
    ids = [row_id for row_id, blob in rows if len(blob) == expected]
    buffers = [blob for _, blob in rows if len(blob) == expected]
    if not ids:
        empty = _np.zeros((0, dim), dtype=_np.float32)
        return [], empty, _np.zeros(0, dtype=_np.float32)
    matrix = _np.frombuffer(b"".join(buffers), dtype=_np.float32).reshape(len(ids), dim)
    norms = _np.sqrt(_np.einsum("ij,ij->i", matrix, matrix))
    return ids, matrix, norms


class DenseVectorCache:
    def __init__(self, *, max_entries: int = 4, min_reload_interval_sec: float = 30.0) -> None:
        self._entries: dict[tuple[str, str, int], DenseMatrices] = {}
        self._max_entries = max(1, int(max_entries))
        self._min_reload_interval = float(min_reload_interval_sec)

    def get(self, storage: FridayStorage, user_id: str, model: str, dim: int) -> DenseMatrices | None:
        """Матрицы для (арендатор, модель, размерность); None — numpy недоступен."""
        if _np is None:
            return None
        key = (user_id, model, int(dim))
        signature = storage.dense_vector_signature(user_id, model, dim)
        cached = self._entries.get(key)
        now = time.monotonic()
        if cached is not None:
            if cached.signature == signature:
                cached.state = "hit"
                return cached
            if now - cached.loaded_monotonic < self._min_reload_interval:
                # Подпись меняется каждый тик массовой индексации; пересборка на
                # каждый поиск в этот период стоила бы дороже самого поиска.
                cached.state = "stale"
                return cached
        started = time.perf_counter()
        doc_rows = storage.get_user_embeddings(user_id, model, dim, limit=None)
        chunk_rows = storage.get_user_chunk_embeddings(user_id, model, dim)
        doc_ids, doc_matrix, doc_norms = _build(doc_rows, dim)
        chunk_ids, chunk_matrix, chunk_norms = _build(chunk_rows, dim)
        entry = DenseMatrices(
            doc_ids=doc_ids,
            doc_matrix=doc_matrix,
            doc_norms=doc_norms,
            chunk_ids=chunk_ids,
            chunk_matrix=chunk_matrix,
            chunk_norms=chunk_norms,
            # Подпись снята ДО чтения строк: изменение, вклинившееся между чтением
            # подписи и строк, сделает подпись «устаревшей» и вызовет пересборку на
            # следующем обращении — ошибка в сторону лишней работы, не потери данных.
            signature=signature,
            loaded_monotonic=now,
        )
        if key not in self._entries and len(self._entries) >= self._max_entries:
            # Простейший LRU: выкидывается самая старая загрузка. Арендаторов в
            # этой системе единицы, предел — страховка, а не рабочий режим.
            oldest = min(self._entries, key=lambda item: self._entries[item].loaded_monotonic)
            self._entries.pop(oldest, None)
        self._entries[key] = entry
        LOGGER.info(
            "Резидентные вектора пересобраны: %d объектов, %d пассажей за %.2f с",
            len(doc_ids),
            len(chunk_ids),
            time.perf_counter() - started,
        )
        return entry

    def invalidate(self, user_id: str | None = None) -> None:
        if user_id is None:
            self._entries.clear()
            return
        for key in [key for key in self._entries if key[0] == user_id]:
            self._entries.pop(key, None)


def matrix_scores(
    query_vector: list[float], entry_ids: list[str], matrix: Any, norms: Any
) -> list[tuple[float, str]]:
    """Косинусы по готовой матрице: та же математика, что `_dense_scores_numpy`,
    минус склейка BLOB-ов и пересчёт норм на каждый запрос."""
    if _np is None or not entry_ids:
        return []
    query = _np.asarray(query_vector, dtype=_np.float32)
    query_norm = math.sqrt(float(query @ query))
    if query_norm == 0.0:
        return []
    dots = matrix @ query
    valid = norms > 0.0
    scores = dots[valid] / (norms[valid] * query_norm)
    valid_ids = [entry_ids[index] for index in range(len(entry_ids)) if valid[index]]
    return list(zip((float(score) for score in scores), valid_ids, strict=True))
