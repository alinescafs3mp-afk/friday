from __future__ import annotations
from friday.people import resolve_person, unambiguous


def test_documented_phrasings():
    rows = [
        {"id": "telegram:telegram:1", "display_name": "Иван", "username": "ivan"},
        {"id": "telegram:telegram:2", "display_name": "Хасанов Руслан", "username": ""},
    ]
    for q in ("Иван", "у Ивана", "Ивану", "у Иван", "иван",
              "Хасанов", "у Хасанова", "Хасанова Руслана",
              "что у Иванова", "по Хасанову"):
        m = resolve_person(rows, q)
        w = unambiguous(m)
        print(f"{q!r:22} -> {(w.display_name if w else 'НЕ НАЙДЕН')!r:18} "
              f"{[(x.display_name, round(x.confidence,2), x.method) for x in m]}")
