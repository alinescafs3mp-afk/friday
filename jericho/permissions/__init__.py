"""Capability-based authorization and request actor context.

Jericho uses default-deny authorization.  Built-in presets are code-defined,
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
    from jericho.storage import JerichoStorage

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
    policy_epoch: int = 1

    @property
    def is_owner(self) -> bool:
        return self.preset_key == "owner"


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
        if self.default_requires_hitl:
            # Поле объявлено, но НИЧЕГО его не читает: авторизация знает только
            # «можно» и «нельзя», а ядро после проверки права сразу зовёт обработчик.
            # Найдено Sol и подтверждено грепом по всему дереву — упоминаний ровно
            # два, объявление и это место.
            #
            # Обещание в коде, за которым нет механизма, опаснее отсутствия обещания:
            # тот, кто пометит способность как требующую человека, будет считать, что
            # действие задержано, а оно исполнится сразу. Поэтому не молчим, а падаем
            # на старте. Строка снимается вместе с появлением настоящего шлюза
            # подтверждения — см. `sol/PROPOSALS.md` №2.
            raise ValueError(
                f"{self.security_id}: default_requires_hitl объявлен, но шлюза подтверждения "
                "в системе нет — пометка не задержит действие. Сначала шлюз, потом пометка."
            )


# The same identifiers are used by HTTP routes and agent tools.
CORE_CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        "chat.use", "Use the conversational agent", "chat", 0, ("admin", "moderator", "user", "guest")
    ),
    CapabilityDefinition(
        "search.use", "Search personal knowledge", "retrieval", 0, ("admin", "moderator", "user", "guest")
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
)


@dataclass(frozen=True)
class AuthorizationDecision:
    decision_id: str
    effect: Literal["allow", "deny"]
    security_id: str
    user_id: str
    reason_code: str
    policy_epoch: int
    preset_key: str | None = None

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


class AuthorizationService:
    """Resolve built-in/custom presets plus explicit allow/deny overrides."""

    def __init__(self, storage: JerichoStorage | None = None) -> None:
        self.storage = storage
        self._capabilities: dict[str, CapabilityDefinition] = {}
        for capability in CORE_CAPABILITIES:
            self.register_capability(capability)

    def register_capability(self, capability: CapabilityDefinition) -> None:
        capability.validate()
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
        return ActorContext(
            user_id=user_id,
            preset_key=self.get_user_preset(user_id),
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
        capability = self._capabilities.get(security_id)
        if not capability:
            return AuthorizationDecision(
                decision_id,
                "deny",
                security_id,
                actor.user_id,
                "unknown_capability",
                actor.policy_epoch,
                actor.preset_key,
            )

        overrides = self.storage.get_permission_overrides(actor.user_id) if self.storage else {}
        if overrides.get(security_id) == "deny":
            return AuthorizationDecision(
                decision_id,
                "deny",
                security_id,
                actor.user_id,
                "explicit_deny",
                actor.policy_epoch,
                actor.preset_key,
            )
        if overrides.get(security_id) == "allow":
            return AuthorizationDecision(
                decision_id,
                "allow",
                security_id,
                actor.user_id,
                "explicit_allow",
                actor.policy_epoch,
                actor.preset_key,
            )
        if security_id in self._preset_grants(actor.preset_key):
            return AuthorizationDecision(
                decision_id,
                "allow",
                security_id,
                actor.user_id,
                "preset_grant",
                actor.policy_epoch,
                actor.preset_key,
            )
        return AuthorizationDecision(
            decision_id,
            "deny",
            security_id,
            actor.user_id,
            "default_deny",
            actor.policy_epoch,
            actor.preset_key,
        )

    def require(self, actor: ActorContext, security_id: str) -> AuthorizationDecision:
        decision = self.authorize(actor, security_id)
        if not decision.allowed:
            raise AuthorizationError(f"Access denied for {security_id} ({decision.reason_code})")
        return decision
