"""Личный запрет действует и в общем архиве.

Разбор Codex (`sol/HARDENING_FOR_OPUS.md`, §12.1). `actor_for_user` берёт ЛИЧНЫЙ
пресет человека и кладёт в `actor.user_id` общего арендатора — так и задумано:
искать надо в том архиве, который человеку открыт. Но `authorize()` читает
переопределения прав по `actor.user_id`, то есть по арендатору.

Следствие ровно обратно тому, что обещает комментарий рядом («права остаются
личными»): для пресета это правда, для явных allow/deny — нет. Запрет, выданный
человеку, не находится: поиск идёт по общей строке, она пуста, и решение
возвращается разрешающим от пресета.

Третий случай одного семейства за сутки, и все три об одном — `user_id` перестал
быть человеком, а код местами всё ещё считает его человеком:

    заявка на подтверждение была видна и решаема любым участником (§12.2);
    указание «отвечай мне кратко» ложилось в общую учётку;
    личный запрет не действовал — здесь.

Опасен именно ЗАПРЕТ. Забытый allow означает «человек не получил лишнего» —
неудобно; забытый deny означает «человек сохранил то, что у него отобрали», и
узнать об этом можно только по сделанному.
"""

from __future__ import annotations

import pytest

from friday.permissions import AuthorizationService


@pytest.fixture
def shared(storage):
    """Служба прав в режиме общего архива и два человека в нём."""
    for name in ("tenant", "person-a", "person-b"):
        storage.ensure_user(name)
    return AuthorizationService(storage, shared_tenant="tenant")


def test_a_personal_deny_is_honoured(shared, storage) -> None:
    """Мутация: читать переопределения по `actor.user_id` — запрет снова не действует."""
    shared.deny_permission("person-a", "knowledge.create")
    actor = shared.actor_for_user("person-a", source="test")

    decision = shared.authorize(actor, "knowledge.create")

    assert decision.allowed is False, "личный запрет не подействовал в общем архиве"
    assert decision.reason_code == "explicit_deny"


def test_the_deny_belongs_to_one_person_only(shared, storage) -> None:
    """Обратная сторона: запрет одному не запрещает другому.

    Без неё правку можно «пройти», запретив всем сразу, — и это было бы хуже
    исходной дыры.
    """
    shared.deny_permission("person-a", "knowledge.create")
    other = shared.actor_for_user("person-b", source="test")

    assert shared.authorize(other, "knowledge.create").allowed is True


def test_a_personal_allow_is_honoured(shared, storage) -> None:
    """Выданное сверх пресета тоже личное — иначе оно досталось бы всем."""
    shared.grant_permission("person-a", "admin.diagnostics", acting_actor=None)
    a = shared.actor_for_user("person-a", source="test")
    b = shared.actor_for_user("person-b", source="test")

    assert shared.authorize(a, "admin.diagnostics").allowed is True
    assert shared.authorize(b, "admin.diagnostics").allowed is False, "право утекло соседу"


def test_without_a_shared_archive_nothing_changes(storage) -> None:
    """Установка с одним пользователем не может пострадать: там оба идентификатора совпадают."""
    storage.ensure_user("solo")
    service = AuthorizationService(storage)
    service.deny_permission("solo", "knowledge.create")
    actor = service.actor_for_user("solo", source="test")

    assert service.authorize(actor, "knowledge.create").allowed is False


def test_the_decision_names_the_person_not_the_tenant(shared, storage) -> None:
    """След решения о правах должен называть, КОМУ отказали.

    Запись «отказано арендатору» бесполезна в общем архиве: арендатор один, а
    людей несколько, и разобраться потом будет невозможно.
    """
    shared.deny_permission("person-a", "knowledge.create")
    actor = shared.actor_for_user("person-a", source="test")

    decision = shared.authorize(actor, "knowledge.create")

    assert decision.user_id == "person-a", f"в следе записан не человек: {decision.user_id}"


def test_the_comment_no_longer_promises_more_than_it_does() -> None:
    """Комментарий обещал «права остаются личными», а личным был только пресет.

    Обещание, которое код не держит, опаснее отсутствия обещания: на него
    ссылаются и считают, что защита есть.
    """
    import inspect

    source = inspect.getsource(AuthorizationService.actor_for_user)
    assert "переопределения" in source.casefold(), (
        "комментарий не говорит, что личными остаются И переопределения"
    )
