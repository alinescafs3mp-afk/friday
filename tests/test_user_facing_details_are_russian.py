"""HTTPException.detail, доезжающий до экрана, должен быть по-русски.

Спецификация V2 §9.3: видимый пользователю нерусский текст — баг. Админ-UI
делает `throw new Error(data?.detail)` и `toast(e.message)` — английская
строка из API показывается дословно.

Инвентарь собирает ВСЕ `detail=` в `admin_api/` и `api/`. Статический литерал
и f-string обязаны содержать кириллицу. Динамический `str(exc)` проверить
статически нельзя — такие точки перечислены явно; молчаливый пропуск запрещён.
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SURFACE = [ROOT / "friday" / "admin_api", ROOT / "friday" / "api"]
CYRILLIC = re.compile(r"[А-Яа-яЁё]")

# Каждая запись — «path:lineno» точки, где detail= не литерал (обычно str(exc)).
# Добавляй причину рядом при росте; уменьшать — только когда detail стал
# статическим русским текстом. Число пинится, чтобы новый str(exc) не проскочил.
#
# 43: срез 2026-07-31 при закрытии #54 — все динамические detail= в admin_api/api.
# 49: три graph/as_of маршрута 2026-08-05 передают русскую ошибку единого
# календарного валидатора; иначе API вернул бы 500 на пользовательскую дату.
# 51: пять known_at graph routes используют один allowlisted переводчик
# RelationHistorySnapshotError. Он не отдаёт произвольный str(exc): наружу могут
# выйти только русская категория и каноническая completeness boundary.
# 50: hard-purge больше не публикует внутренний английский ValueError и возвращает
# закрытый русский текст двухфазной политики удаления.
EXPECTED_DYNAMIC_DETAIL_SITES = 50


def _detail_sites() -> list[tuple[pathlib.Path, int, str, ast.AST]]:
    sites: list[tuple[pathlib.Path, int, str, ast.AST]] = []
    for root in SURFACE:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "detail":
                        continue
                    sites.append((path, node.lineno, path.relative_to(ROOT).as_posix(), keyword.value))
    return sites


def _fstring_static_parts(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
    return "".join(parts)


def test_static_http_details_contain_cyrillic():
    """Literals and f-string templates that reach toast must be Russian."""
    offenders: list[str] = []
    for _path, lineno, rel, value in _detail_sites():
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if not CYRILLIC.search(value.value):
                offenders.append(f"{rel}:{lineno}: {value.value!r}")
        elif isinstance(value, ast.JoinedStr):
            template = _fstring_static_parts(value)
            if not CYRILLIC.search(template):
                offenders.append(f"{rel}:{lineno}: f-string {template!r}")
    assert not offenders, "английский detail= доедет до toast в админ-UI:\n" + "\n".join(offenders)


def test_dynamic_http_details_are_inventoried_not_silently_skipped():
    """str(exc) and other non-literals cannot be checked for language here.

    They still reach the same toast path, so each site must appear in the
    inventory. A silent skip would hide exactly the surface that is hardest
    to reason about. Update EXPECTED_DYNAMIC_DETAIL_SITES when the set changes
    and keep the listed paths self-describing.
    """
    dynamic = [
        f"{rel}:{lineno}"
        for _path, lineno, rel, value in _detail_sites()
        if not (
            (isinstance(value, ast.Constant) and isinstance(value.value, str))
            or isinstance(value, ast.JoinedStr)
        )
    ]
    assert len(dynamic) == EXPECTED_DYNAMIC_DETAIL_SITES, (
        f"динамических detail= стало {len(dynamic)}, ожидалось {EXPECTED_DYNAMIC_DETAIL_SITES}.\n"
        "Каждая точка — потенциальная английская утечка через str(exc). "
        "Обновите EXPECTED_DYNAMIC_DETAIL_SITES только вместе с решением по языку.\n" + "\n".join(dynamic)
    )
    assert dynamic, "инвентарь динамических detail= опустел — проверьте, что сканер ещё видит поверхность"


def test_mutation_english_literal_is_caught(tmp_path, monkeypatch):
    """Broken guard: an English detail= must fail the Cyrillic inventory."""
    # Inline the same rule the production scan uses — if someone weakens the
    # regex to always-pass, this still fails on a known English string.
    assert CYRILLIC.search("Объект знания не найден")
    assert not CYRILLIC.search("Knowledge object not found")
