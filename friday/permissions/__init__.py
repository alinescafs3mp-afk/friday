"""Capability-based authorization and request actor context.

Friday uses default-deny authorization.  Built-in presets are code-defined,
while custom presets and per-user overrides are persisted in SQLite.
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from friday.storage import FridayStorage

LEGACY_OWNER_USER_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "jericho://owner"))
DEFAULT_PRESET_KEY = "guest"
BUILTIN_PRESET_KEYS = ("owner", "admin", "moderator", "user", "guest")
_SECURITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class AuthorizationError(PermissionError):
    """The authenticated actor lacks the requested capability."""


class AuthenticationError(PermissionError):
    """No valid authenticated actor was established."""


class ResourceIsolationError(AuthorizationError):
    """A requested object exists outside the actor's tenant boundary."""


@dataclass(frozen=True)
class ActorContext:
    user_id: str
    preset_key: str
    source: str
    identity_id: str | None = None
    session_id: str | None = None
    #: Работает ли этот человек в ОБЩЕМ архиве (`FRIDAY_SHARED_ARCHIVE`).
    #: Тогда `user_id` — общий арендатор, а человека называет `person_id`.
    shared_tenant: bool = False
    #: Учётка человека. В общем архиве `user_id` у всех один, и только это поле
    #: отвечает на вопрос «кто именно». Не `identity_id`: там лежит способ входа
    #: — идентификатор API-токена или связанной телеграм-личности, — и переписка
    #: человека разъезжалась бы по токенам, которыми он входил.
    person_id: str = ""

    @property
    def is_owner(self) -> bool:
        """Хозяин архива — это УЧЁТКА, а не «у него полные права».

        Пресет `owner` означает полноту прав, и по прямой просьбе владельца
        каждый написавший заводится именно с ним: на живой установке таких
        учёток шесть, из них владельцу принадлежат две. Пока это свойство
        читало пресет, хозяином архива система считала всех шестерых — и отдала
        каждому последний рубеж, за которым уже нет ничего.

        Воспроизведено на изолированном стенде; пропускали ЧЕТЫРЕ замка:
        `require_delegable_account` (выпустить API-токен на учётку владельца —
        то есть забрать его власть целиком), `_protect_owner_target` (изменить
        его учётку), `set_user_preset("owner")` (раздать хозяйское положение
        кому угодно) и `_only_mine` (управлять чужими миссиями).

        Различие видно только в ОБЩЕМ архиве: там арендатор один на всех, и
        учётка человека — `own_id`. В личном архиве человек один, `own_id`
        равен `user_id` у всех, и граница по учётке отобрала бы у единственного
        хозяина его же архив; там решает пресет, как решал всегда.

        Смотреть это правило не запрещает: владелец 2026-08-04 решил «все видят
        всех», и надзор оно не трогает. Оно про право МЕНЯТЬ чужую учётку,
        которое досталось участникам заодно — потому что одно слово `owner`
        называло два разных понятия.
        """
        if self.shared_tenant:
            return self.own_id == self.user_id
        return self.preset_key == "owner"

    @property
    def own_id(self) -> str:
        """Идентификатор ЧЕЛОВЕКА, а не арендатора, в котором он работает.

        При обычной настройке это одно и то же. В общем архиве `user_id` у всех
        один — иначе они не видели бы материал друг друга, — и различает людей
        только это поле. По нему остаются личными переписка и авторство: общими
        просьба делала документы и записи, а не чужие разговоры.

        Признак берётся отдельным полем, а не выводится из «identity_id не равен
        user_id»: связанная личность бывает и при обычной настройке (владелец,
        вошедший через бота), и вывод разносил бы его переписку по двум разным
        идентификаторам — поймано двадцатью пятью упавшими тестами. И человек
        здесь `person_id`, а не `identity_id`: во втором лежит СПОСОБ входа
        (идентификатор API-токена), и переписка разъезжалась бы по токенам.
        """
        if self.shared_tenant and self.person_id:
            return self.person_id
        return self.user_id


_ACTOR: ContextVar[ActorContext | None] = ContextVar("jericho_actor", default=None)


def legacy_owner_context(*, source: str = "api-token") -> ActorContext:
    return ActorContext(user_id=LEGACY_OWNER_USER_ID, preset_key="owner", source=source)


def current_actor() -> ActorContext:
    actor = _ACTOR.get()
    if actor is None:
        raise AuthenticationError("No authenticated actor is bound to this operation")
    return actor


def current_user_id() -> str:
    return current_actor().user_id


@contextmanager
def bind_actor(actor: ActorContext):
    token = _ACTOR.set(actor)
    try:
        yield actor
    finally:
        _ACTOR.reset(token)


@dataclass(frozen=True)
class CapabilityDefinition:
    security_id: str
    description: str
    category: str
    risk_level: int = 0
    default_presets: tuple[str, ...] = ()
    default_requires_hitl: bool = False
    source: str = "core"

    def validate(self) -> None:
        if not _SECURITY_ID_RE.fullmatch(self.security_id):
            raise ValueError(f"Invalid security_id: {self.security_id!r}")
        if not 0 <= int(self.risk_level) <= 4:
            raise ValueError("risk_level must be between 0 and 4")
        invalid = set(self.default_presets) - set(BUILTIN_PRESET_KEYS)
        if invalid:
            raise ValueError(f"Unknown default presets: {sorted(invalid)}")
        # `default_requires_hitl` больше не роняет старт, и вот почему.
        #
        # Прежде поле было обещанием без механизма: авторизация знает только
        # «можно» и «нельзя», ядро после проверки права сразу звало обработчик, и
        # пометка не задерживала ничего. Падение на старте было честнее молчания —
        # тот, кто пометил бы способность, считал бы действие задержанным.
        #
        # Шлюз с тех пор построен: `action_approvals`, заявка человеку с кнопками,
        # отпечаток аргументов, повторная проверка прав перед эффектом. Ядро
        # спрашивает человека по этой пометке (`_capability_requires_person`),
        # поэтому обещание наконец обеспечено, и запрет снят.


# The same identifiers are used by HTTP routes and agent tools.
CORE_CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        "chat.use", "Use the conversational agent", "chat", 0, ("admin", "moderator", "user", "guest")
    ),
    CapabilityDefinition(
        "search.use", "Search personal knowledge", "retrieval", 0, ("admin", "moderator", "user", "guest")
    ),
    CapabilityDefinition(
        "tts.use",
        "Speak a reply aloud as a voice message",
        "chat",
        0,
        ("admin", "moderator", "user", "guest"),
    ),
    CapabilityDefinition(
        "knowledge.read", "Read knowledge objects", "knowledge", 0, ("admin", "moderator", "user", "guest")
    ),
    CapabilityDefinition(
        "knowledge.create", "Create knowledge objects", "knowledge", 1, ("admin", "moderator", "user")
    ),
    CapabilityDefinition(
        "knowledge.edit", "Edit knowledge objects", "knowledge", 1, ("admin", "moderator", "user")
    ),
    CapabilityDefinition(
        "knowledge.delete", "Soft-delete knowledge objects", "knowledge", 2, ("admin", "moderator", "user")
    ),
    CapabilityDefinition("inbox.read", "Read the personal inbox", "inbox", 0, ("admin", "moderator", "user")),
    CapabilityDefinition(
        "inbox.review", "Review and classify inbox entries", "inbox", 1, ("admin", "moderator", "user")
    ),
    CapabilityDefinition(
        "kg.read",
        "Inspect the knowledge graph",
        "knowledge_graph",
        0,
        ("admin", "moderator", "user", "guest"),
    ),
    CapabilityDefinition(
        "kg.write",
        "Create and edit graph entities and relations",
        "knowledge_graph",
        1,
        ("admin", "moderator", "user"),
    ),
    CapabilityDefinition(
        "kg.merge", "Merge duplicate graph entities", "knowledge_graph", 2, ("admin", "moderator")
    ),
    CapabilityDefinition(
        "files.upload", "Upload files for ingestion", "files", 1, ("admin", "moderator", "user")
    ),
    CapabilityDefinition("files.read", "Read owned files", "files", 0, ("admin", "moderator", "user")),
    CapabilityDefinition(
        "feedback.write",
        "Record retrieval and answer feedback",
        "feedback",
        0,
        ("admin", "moderator", "user", "guest"),
    ),
    CapabilityDefinition(
        "conversations.read", "Read owned conversations", "chat", 0, ("admin", "moderator", "user", "guest")
    ),
    CapabilityDefinition(
        "conversations.manage",
        "Archive and delete owned conversations",
        "chat",
        1,
        ("admin", "moderator", "user"),
    ),
    CapabilityDefinition("web.search", "Search the public web", "web", 1, ("admin", "moderator", "user")),
    CapabilityDefinition("web.fetch", "Fetch a public web page", "web", 1, ("admin", "moderator", "user")),
    CapabilityDefinition("web.research", "Run multi-source web research", "web", 2, ("admin", "moderator")),
    CapabilityDefinition("code.run", "Run code in the restricted subprocess executor", "execution", 4, ()),
    CapabilityDefinition(
        "missions.read", "Inspect owned missions", "executive", 0, ("admin", "moderator", "user")
    ),
    CapabilityDefinition(
        "missions.create", "Create and plan missions", "executive", 1, ("admin", "moderator", "user")
    ),
    CapabilityDefinition(
        "missions.control",
        "Start, stop, and cancel owned missions",
        "executive",
        2,
        ("admin", "moderator", "user"),
    ),
    CapabilityDefinition("admin.missions.read", "Inspect missions across users", "admin", 3, ("admin",)),
    CapabilityDefinition("admin.missions.manage", "Control missions across users", "admin", 3, ("admin",)),
    CapabilityDefinition("admin.users.read", "List and inspect users", "admin", 2, ("admin",)),
    CapabilityDefinition("admin.users.manage", "Change users and presets", "admin", 3, ("admin",)),
    CapabilityDefinition("admin.presets.manage", "Manage custom permission presets", "admin", 3, ("admin",)),
    CapabilityDefinition("admin.audit.read", "Read the administrative audit log", "admin", 2, ("admin",)),
    CapabilityDefinition("admin.backup.manage", "Create and verify backups", "admin", 3, ("admin",)),
    CapabilityDefinition("admin.export", "Export user data", "admin", 3, ("admin",)),
    CapabilityDefinition("admin.diagnostics", "Inspect system diagnostics", "admin", 2, ("admin",)),
    # Кто, когда, сколько, откуда и что из этого стало знанием — без единой строки
    # того, что человек написал. Уровень 2, а не 3: он намеренно НИЖЕ
    # `admin.all_data.read`, и держатель одного не получает второго автоматически —
    # проверка способностей сравнивает точный идентификатор, иерархии «старшая
    # включает младшую» здесь нет. Обратная сторона того же устройства: способность
    # раздаётся пресету `admin`, иначе полный администратор не смог бы делегировать
    # то, чем сам не владеет, и уровень оказался бы некому выдать, кроме владельца.
    CapabilityDefinition(
        "admin.activity.read",
        "See when and how much other accounts wrote, without reading it",
        "admin",
        2,
        ("admin",),
    ),
    CapabilityDefinition("admin.all_data.read", "Read data across users", "admin", 3, ("admin",)),
    CapabilityDefinition("admin.all_data.manage", "Modify data across users", "admin", 4, ("admin",)),
    CapabilityDefinition(
        "admin.data.purge",
        "Permanently purge (hard-delete) soft-deleted data across users",
        "admin",
        4,
        ("admin",),
    ),
    CapabilityDefinition(
        "admin.tokens.manage",
        "Issue and revoke scoped API tokens for other accounts",
        "admin",
        4,
        ("admin",),
    ),
    # Чтение ВНЕШНЕЙ базы, объявленной источником. Отдельное право, а не часть
    # `kg.read`: там свой архив, здесь чужая боевая система по сети, и цена
    # ошибки другая — от неё зависит не только выдача, но и нагрузка на чужой
    # сервер. Пресеты — только владелец и админ: участник общего архива не
    # должен ходить в кадровую базу через чат.
    CapabilityDefinition(
        "data.read",
        "Read declared external SQL sources (read-only, one SELECT at a time)",
        "data",
        2,
        ("owner", "admin"),
    ),
)

#: Имена, принадлежащие ядру. Считается ОДИН раз из самого списка, а не пишется
#: вторым списком рядом: два перечня одного и того же расходятся молча, и здесь
#: расхождение означало бы дыру, а не опечатку — новое системное право оказалось бы
#: перекрываемым снаружи ровно потому, что его забыли внести во второй список.
_CORE_CAPABILITY_IDS: frozenset[str] = frozenset(item.security_id for item in CORE_CAPABILITIES)


@dataclass(frozen=True)
class AuthorizationDecision:
    decision_id: str
    effect: Literal["allow", "deny"]
    security_id: str
    user_id: str
    reason_code: str
    preset_key: str | None = None

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


class AuthorizationService:
    """Resolve built-in/custom presets plus explicit allow/deny overrides."""

    def __init__(self, storage: FridayStorage | None = None, *, shared_tenant: str = "") -> None:
        self.storage = storage
        # Общий архив: все работают в ОДНОМ арендаторе, поэтому любой находит,
        # правит и подтверждает материал любого. Подмена делается здесь, а не в
        # двухстах сорока трёх местах, где спрашивают `actor.user_id`: иначе одно
        # забытое место означало бы, что часть системы видит общий корпус, а
        # часть — свой, и расхождение вылезло бы молча.
        #
        # Личность не теряется: настоящий идентификатор человека едет в
        # `identity_id`, и по нему пишется след.
        self._shared_tenant = str(shared_tenant or "").strip()
        self._capabilities: dict[str, CapabilityDefinition] = {}
        for capability in CORE_CAPABILITIES:
            self.register_capability(capability)

    def capability_requires_person(self, security_id: str) -> bool:
        """Помечена ли способность как «не молча»: задержать любое её действие.

        Пометка живёт на СПОСОБНОСТИ, а не на инструменте, и это разные вопросы.
        Класс риска инструмента говорит, что делает вызов; пометка говорит, что
        владелец не хочет отдавать этот класс действий без своего слова, каким бы
        безобидным ни выглядел конкретный вызов.
        """

        definition = self._capabilities.get(str(security_id or "").strip())
        return bool(definition and definition.default_requires_hitl)

    def register_capability(self, capability: CapabilityDefinition) -> None:
        """Объявить способность. Занятое имя не переопределяется — падаем на старте.

        Раньше здесь была обычная запись в словарь по ключу, и этого хватало, чтобы
        орган ОДНОЙ строкой переписал системное право. Воспроизведено на стенде:
        `CapabilityDefinition("kg.merge", "мой безобидный обход", "kg", 0, ("guest",
        "user"))` — и `_builtin_grants("guest")` начинает возвращать `kg.merge`, то
        есть гость получает слияние сущностей. Ни исключения, ни строки в журнале:
        со стороны это выглядит как «так и было задумано».

        Дыра тут не в органах — свои пять ведут себя честно, — а в том, что честность
        держалась на соглашении. Органы собираются `build_registry` из каталога, и
        один чужой файл рядом с ними не отличим от своего.

        Вторая половина той же дыры — умолчание `source="core"`: объявитель, забывший
        поле, представляется ядром, и по журналу потом не отличить системное право от
        принесённого. Поэтому имя из ядрового пространства (`CORE_CAPABILITIES`) не
        может быть объявлено с чужим `source`, а чужое имя — с `source="core"`.

        Повтор ОДНОГО И ТОГО ЖЕ определения проходит: сборка приложения не одна
        (сервер, CLI, тесты), и требовать от них ровно одного вызова — значит менять
        безопасность на хрупкость. Отличается хоть одно поле — это уже подмена.
        """
        capability.validate()
        existing = self._capabilities.get(capability.security_id)
        if existing is not None and existing != capability:
            raise ValueError(
                f"{capability.security_id}: способность с таким именем уже объявлена "
                f"({existing.source}, пресеты {list(existing.default_presets)}), а новое "
                f"объявление другое ({capability.source}, пресеты "
                f"{list(capability.default_presets)}). Переопределять чужие права нельзя: "
                "выберите своё имя."
            )
        core_name = capability.security_id in _CORE_CAPABILITY_IDS
        if core_name and capability.source != "core":
            raise ValueError(
                f"{capability.security_id}: это системная способность, её нельзя объявить "
                f"из {capability.source!r}."
            )
        if not core_name and capability.source == "core":
            raise ValueError(
                f"{capability.security_id}: объявлена как системная (`source=\"core\"`), но "
                "в ядровом списке её нет. Укажите, откуда она пришла — иначе в журнале "
                "принесённое право не отличить от системного."
            )
        self._capabilities[capability.security_id] = capability

    def get_capability(self, security_id: str) -> CapabilityDefinition | None:
        return self._capabilities.get(security_id)

    def list_capabilities(self) -> list[CapabilityDefinition]:
        return sorted(self._capabilities.values(), key=lambda item: item.security_id)

    def preset_exists(self, preset_key: str) -> bool:
        if preset_key in BUILTIN_PRESET_KEYS:
            return True
        return bool(self.storage and self.storage.get_custom_preset(preset_key))

    def _require_delegable(self, acting_actor: ActorContext | None, security_id: str) -> None:
        """Service-level delegation invariant: nobody grants beyond their own authority.

        ``acting_actor=None`` means a trusted internal caller (server bootstrap,
        offline CLI on the same filesystem) — those paths are documented
        exceptions, every actor-attributed caller must pass itself in.
        """
        if acting_actor is None or acting_actor.is_owner:
            return
        if not self.authorize(acting_actor, security_id).allowed:
            raise AuthorizationError(f"Cannot delegate capability not held by the actor: {security_id}")

    def effective_capabilities(self, user_id: str) -> set[str]:
        """Everything this account can do: its preset, plus and minus overrides."""
        preset_key = self.get_user_preset(user_id)
        granted = set(self._preset_grants(preset_key))
        overrides = self.storage.get_permission_overrides(user_id) if self.storage else {}
        for security_id, effect in overrides.items():
            if effect == "allow":
                granted.add(security_id)
            elif effect == "deny":
                granted.discard(security_id)
        return granted

    def require_delegable_account(self, acting_actor: ActorContext | None, target_user_id: str) -> None:
        """The delegation invariant applied to an ACCOUNT rather than one capability.

        `_require_delegable` guards `grant_permission`, `set_user_preset` and
        custom presets, so an administrator cannot hand out authority they do not
        hold. Minting an API token for another account hands out that account's
        ENTIRE authority in one step and went through none of it: the only barrier
        was `_protect_owner_target`, which recognises the legacy owner id and the
        `owner` preset — and an ordinary account carrying one capability above the
        administrator's own (say `code.run`, which no preset but `owner` grants)
        is neither. Print a token, authenticate as them, keep the difference.
        """
        if acting_actor is None or acting_actor.is_owner:
            return
        for security_id in sorted(self.effective_capabilities(target_user_id)):
            if not self.authorize(acting_actor, security_id).allowed:
                raise AuthorizationError(
                    "Cannot issue a token for an account holding a capability the actor "
                    f"does not have: {security_id}"
                )

    def set_user_preset(
        self,
        user_id: str,
        preset_key: str,
        *,
        acting_actor: ActorContext | None = None,
    ) -> None:
        if not self.preset_exists(preset_key):
            raise ValueError(f"Unknown preset: {preset_key}")
        if not self.storage:
            raise RuntimeError("Persistent storage is required to assign presets")
        if acting_actor is not None and not acting_actor.is_owner:
            if preset_key == "owner":
                raise AuthorizationError("Only an owner may assign the owner preset")
            for security_id in self._preset_grants(preset_key):
                self._require_delegable(acting_actor, security_id)
        self.storage.ensure_user(user_id)
        self.storage.update_user(user_id, preset_key=preset_key)

    def get_user_preset(self, user_id: str) -> str:
        if not self.storage:
            return DEFAULT_PRESET_KEY
        user = self.storage.get_user(user_id)
        return str(user.get("preset_key", DEFAULT_PRESET_KEY)) if user else DEFAULT_PRESET_KEY

    def create_custom_preset(
        self,
        preset_key: str,
        name: str,
        security_ids: set[str],
        *,
        description: str = "",
        created_by: str,
        acting_actor: ActorContext | None = None,
    ) -> dict:
        if not self.storage:
            raise RuntimeError("Persistent storage is required for custom presets")
        unknown = security_ids - self._capabilities.keys()
        if unknown:
            raise ValueError(f"Unknown capabilities: {sorted(unknown)}")
        if preset_key in BUILTIN_PRESET_KEYS:
            raise ValueError("Built-in presets cannot be overwritten")
        for security_id in security_ids:
            self._require_delegable(acting_actor, security_id)
        return self.storage.upsert_custom_preset(
            preset_key,
            name,
            security_ids,
            description=description,
            created_by=created_by,
        )

    def list_presets(self) -> list[dict]:
        presets: list[dict] = []
        for key in BUILTIN_PRESET_KEYS:
            presets.append(
                {
                    "preset_key": key,
                    "name": key.title(),
                    "built_in": True,
                    "capabilities": sorted(self._builtin_grants(key)),
                }
            )
        if self.storage:
            presets.extend({**item, "built_in": False} for item in self.storage.list_custom_presets())
        return presets

    def grant_permission(
        self,
        user_id: str,
        security_id: str,
        *,
        acting_actor: ActorContext | None = None,
    ) -> None:
        self._validate_capability(security_id)
        if not self.storage:
            raise RuntimeError("Persistent storage is required for overrides")
        self._require_delegable(acting_actor, security_id)
        self.storage.set_permission_override(user_id, security_id, "allow")

    # deny/revoke REMOVE authority — deliberately no delegation check: an
    # admin may always narrow access, including to capabilities it lacks.
    def deny_permission(self, user_id: str, security_id: str) -> None:
        self._validate_capability(security_id)
        if not self.storage:
            raise RuntimeError("Persistent storage is required for overrides")
        self.storage.set_permission_override(user_id, security_id, "deny")

    def revoke_permission(self, user_id: str, security_id: str) -> None:
        self._validate_capability(security_id)
        if not self.storage:
            raise RuntimeError("Persistent storage is required for overrides")
        self.storage.set_permission_override(user_id, security_id, None)

    def actor_for_user(self, user_id: str, *, source: str, identity_id: str | None = None) -> ActorContext:
        preset = self.get_user_preset(user_id)
        if self._shared_tenant:
            # Права остаются личными — и пресет, и ПЕРЕОПРЕДЕЛЕНИЯ. Общим
            # становится только хранилище, в котором человек работает.
            #
            # Уточнено 2026-08-04: прежняя редакция обещала то же самое, а
            # выполняла наполовину — пресет брался личный, а явные allow/deny
            # читались в `authorize()` по арендатору и не находились. Обещание,
            # которое код не держит, опаснее отсутствия обещания: на него
            # ссылаются и считают, что защита есть.
            return ActorContext(
                user_id=self._shared_tenant,
                preset_key=preset,
                source=source,
                identity_id=identity_id,
                shared_tenant=True,
                person_id=user_id,
            )
        return ActorContext(
            user_id=user_id,
            preset_key=preset,
            source=source,
            identity_id=identity_id,
        )

    def _validate_capability(self, security_id: str) -> None:
        if security_id not in self._capabilities:
            raise ValueError(f"Unknown capability: {security_id}")

    def _builtin_grants(self, preset_key: str) -> set[str]:
        if preset_key == "owner":
            return set(self._capabilities)
        return {
            capability.security_id
            for capability in self._capabilities.values()
            if preset_key in capability.default_presets
        }

    def _preset_grants(self, preset_key: str) -> set[str]:
        if preset_key in BUILTIN_PRESET_KEYS:
            return self._builtin_grants(preset_key)
        if not self.storage:
            return set()
        preset = self.storage.get_custom_preset(preset_key)
        return set(preset.get("capabilities", [])) if preset else set()

    def authorize(self, actor: ActorContext, security_id: str) -> AuthorizationDecision:
        decision_id = uuid.uuid4().hex[:16]
        # Права принадлежат ЧЕЛОВЕКУ, а не архиву, в котором он работает.
        #
        # Разбор Codex §12.1, воспроизведено: `actor_for_user` берёт личный пресет
        # и кладёт в `actor.user_id` общего арендатора — так и задумано, искать
        # надо в открытом человеку архиве. А переопределения читались по этому же
        # `user_id`, то есть по арендатору: явный запрет, выданный человеку, не
        # находился (общая строка пуста), и решение возвращалось разрешающим от
        # пресета.
        #
        # Опасен именно ЗАПРЕТ. Забытый allow значит «не получил лишнего» —
        # неудобно; забытый deny значит «сохранил то, что у него отобрали», и
        # узнать об этом можно только по сделанному.
        #
        # В след тоже пишется человек: запись «отказано арендатору» в общем архиве
        # бесполезна — арендатор один, людей несколько.
        principal = actor.own_id
        capability = self._capabilities.get(security_id)
        if not capability:
            return AuthorizationDecision(
                decision_id,
                "deny",
                security_id,
                principal,
                "unknown_capability",
                actor.preset_key,
            )

        overrides = self.storage.get_permission_overrides(principal) if self.storage else {}
        if overrides.get(security_id) == "deny":
            return AuthorizationDecision(
                decision_id,
                "deny",
                security_id,
                principal,
                "explicit_deny",
                actor.preset_key,
            )
        if overrides.get(security_id) == "allow":
            return AuthorizationDecision(
                decision_id,
                "allow",
                security_id,
                principal,
                "explicit_allow",
                actor.preset_key,
            )
        if security_id in self._preset_grants(actor.preset_key):
            return AuthorizationDecision(
                decision_id,
                "allow",
                security_id,
                principal,
                "preset_grant",
                actor.preset_key,
            )
        return AuthorizationDecision(
            decision_id,
            "deny",
            security_id,
            principal,
            "default_deny",
            actor.preset_key,
        )

    def require(self, actor: ActorContext, security_id: str) -> AuthorizationDecision:
        decision = self.authorize(actor, security_id)
        if not decision.allowed:
            raise AuthorizationError(f"Access denied for {security_id} ({decision.reason_code})")
        return decision
