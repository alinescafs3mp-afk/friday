"""Подставлять в разметку функцию вместо её результата — ошибка, которая не падает.

`${pager}` вместо `${pager(...)}` — валидный JavaScript. Шаблонная строка зовёт
`toString()`, и в страницу уходит ИСХОДНИК стрелочной функции: пейджер не работает,
но консоль молчит, `node --check` доволен, и на глаз в мешанине разметки это
неотличимо от подстановки переменной. Именно так экран «Активность» приехал в
v0.134.0 с нерабочей пагинацией — свой пейджер собирался в переменную, а строка
рядом подставляла хелпер.

Тест не про конкретный `pager`: он собирает ВСЕ функции, объявленные в модуле, и
требует, чтобы ни одна не подставлялась в шаблон голым именем.
"""

from __future__ import annotations

import pathlib
import re

APP_JS = pathlib.Path(__file__).resolve().parents[1] / "jericho" / "admin_ui" / "static" / "app.js"

# `const name = (a, b) => …`, `const name = x => …`, `const name = async () => …`
_ARROW = re.compile(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
# `function name(…)`
_DECLARED = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")


def _without_line_comments(source: str) -> str:
    """Убрать строки-комментарии целиком.

    Только те, что начинаются с `//` после отступа: обрезать по `//` где угодно
    нельзя, в разметке живут `https://`. Комментарий, объясняющий эту самую
    ошибку, иначе сам её и «находит».
    """
    return "\n".join("" if line.lstrip().startswith("//") else line for line in source.split("\n"))


def _function_names(source: str) -> set[str]:
    return {match.group(1) for match in _ARROW.finditer(source)} | {
        match.group(1) for match in _DECLARED.finditer(source)
    }


def test_no_helper_is_interpolated_without_being_called():
    source = _without_line_comments(APP_JS.read_text(encoding="utf-8"))
    names = _function_names(source)
    assert "pager" in names, "разбор объявлений сломался — хелпер pager не найден"
    assert "esc" in names, "разбор объявлений сломался — хелпер esc не найден"

    offenders: list[tuple[int, str, str]] = []
    for name in sorted(names):
        for match in re.finditer(r"\$\{" + re.escape(name) + r"\}", source):
            line = source.count("\n", 0, match.start()) + 1
            offenders.append((line, name, source[match.start() - 60 : match.end() + 20]))

    assert not offenders, (
        "в шаблон подставлено имя функции, а не её результат — в разметку уйдёт исходник:\n"
        + "\n".join(f"  app.js:{line}  ${{{name}}}  …{context}…" for line, name, context in offenders)
    )


def test_the_activity_screen_uses_the_shared_pager():
    """Своя копия пейджера — то, из чего выросла ошибка выше.

    Экран активности был единственным со своей реализацией; пока их две, они
    расходятся молча (у одной «Раньше/Позже», у другой «Назад/Вперёд», и только
    одна знает про честный total).
    """
    source = APP_JS.read_text(encoding="utf-8")
    assert "pager('activityPage'" in source, (
        "экран «Активность» снова собирает пейджер сам, вместо общего хелпера"
    )
