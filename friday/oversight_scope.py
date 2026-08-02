"""Кого именно позволено смотреть — «моих людей», а не всех подряд.

Единственный незакрытый пункт многопользовательской части. Изоляция сделана,
обращение по имени сделано, смена прав через админку сделана — а надзор до сих пор
видел ВСЁ: у кого есть право `admin.all_data.read`, тот читал активность любого.

Пока участник один, это незаметно. В сценарии с несколькими начальниками это
означает, что каждый из них читает чужих подчинённых, и владелец узнает об этом
только из журнала. Тем более что новые учётки по заказу владельца заводятся с
ПОЛНЫМИ правами — то есть право надзора получает каждый написавший.

Связь «кто чей руководитель» хранится в метаданных учётки (`supervisor_id`), а не
отдельной таблицей: это одно поле на человека, миграции схемы оно не стоит, и
читается там же, где `chat_id`.

Правило намеренно простое, в три строки:

  - хозяин архива видит всех — он и так владелец всех данных;
  - руководитель видит своих подчинённых (и себя);
  - остальные — только себя.

Цепочка начальников поддерживается: начальник начальника видит подчинённых через
уровень. Глубина ограничена, потому что кольцо в данных («A подчинён B, B подчинён
A») не должно вешать проверку прав.
"""

from __future__ import annotations

import json
from typing import Any

#: Насколько глубоко разрешено идти вверх по цепочке руководителей. Кольцо в
#: данных при этом не зациклит проверку: она просто упрётся в предел.
_MAX_CHAIN = 8


def supervisor_of(storage: Any, user_id: str) -> str:
    """Идентификатор руководителя этого человека, если он назначен."""
    user = storage.get_user(user_id)
    if not user:
        return ""
    try:
        metadata = json.loads(str(user.get("metadata_json") or "{}"))
    except (TypeError, ValueError):
        return ""
    if not isinstance(metadata, dict):
        return ""
    supervisor = str(metadata.get("supervisor_id") or "").strip()
    return "" if supervisor == user_id else supervisor


def hierarchy_is_configured(storage: Any) -> bool:
    """Назначен ли хоть кому-нибудь руководитель.

    Пока не назначен никому, правило «вижу своих» не применяется вовсе — иначе
    введение этого поля молча выключило бы работающий надзор: у всех учёток
    подчинения нет, значит каждый видел бы только себя, и владелец узнал бы об
    этом, когда кто-то не смог сделать привычное.

    Разметка — действие человека. До него поведение остаётся прежним: право
    надзора решает само, как решало всегда.
    """
    for row in storage.list_users(limit=5000):
        raw = row.get("metadata_json")
        try:
            metadata = json.loads(str(raw or "{}")) if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError):
            continue
        if isinstance(metadata, dict) and str(metadata.get("supervisor_id") or "").strip():
            return True
    return False


def may_oversee(storage: Any, viewer_id: str, target_id: str, *, owner_id: str = "") -> bool:
    """Вправе ли `viewer_id` смотреть деятельность `target_id`.

    `owner_id` — хозяин архива; ему видно всё. Пустое значение означает «хозяин
    не задан», и тогда правило работает только по цепочке руководителей.
    """
    viewer = str(viewer_id or "")
    target = str(target_id or "")
    if not viewer or not target:
        return False
    if viewer == target:
        return True
    if owner_id and viewer == str(owner_id):
        return True
    # Идём от подчинённого ВВЕРХ: у человека один руководитель, а подчинённых
    # может быть сколько угодно, поэтому так дешевле и не нужен обход в ширину.
    seen = {target}
    current = target
    for _ in range(_MAX_CHAIN):
        current = supervisor_of(storage, current)
        if not current or current in seen:
            return False
        if current == viewer:
            return True
        seen.add(current)
    return False


def subordinates_of(storage: Any, viewer_id: str, *, owner_id: str = "") -> list[str]:
    """Кого этот человек вправе смотреть, включая себя.

    Владельцу возвращаются все учётки: он и так видит всё, и отдельный список
    для него был бы только поводом разойтись с `may_oversee`.
    """
    viewer = str(viewer_id or "")
    if not viewer:
        return []
    everyone = [str(row.get("id") or "") for row in storage.list_users(limit=5000)]
    if owner_id and viewer == str(owner_id):
        return [user_id for user_id in everyone if user_id]
    return [
        user_id
        for user_id in everyone
        if user_id and may_oversee(storage, viewer, user_id, owner_id=owner_id)
    ]
