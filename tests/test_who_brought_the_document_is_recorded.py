"""Кто принёс материал — записывается, а неизвестное называется неизвестным.

Найдено ревью уязвимых участков 2026-08-04 и подтверждено замером на живой базе:
3295 документов из 3296 лежат под ОДНИМ идентификатором (архив общий), и признака
автора у них нет ни в столбцах, ни в метаданных. Значит надзор «что Иван присылал»
был неразрешим в принципе: поиск по человеку давал ноль всегда.

Сделано две вещи, и вторая важнее первой.

ПЕРВОЕ: все девять дорог приёма пишут `uploaded_by` — Telegram, HTTP, URL,
веб-исследование и оба импорта. Аутентифицированные дороги пишут `actor.own_id`;
неаутентифицированный CLI принимает явное значение либо сохраняет JSON `null`.
Ключ единый, чтобы надзор не гадал, где искать, а tenant не изображал человека.

ВТОРОЕ: у материалов, принятых РАНЬШЕ, автора нет, и приписать их кому-либо
нельзя — догадка здесь означала бы приписать человеку чужие документы. Такие
строки в ответ по автору не попадают и считаются отдельно, честной строкой «без
автора». Это решение принято сознательно: «ноль у Ивана» рядом с тремя тысячами
документов неизвестного происхождения читается как «Иван ничего не присылал», и
на этом строят кадровые выводы.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import RawObject, new_id
from friday.web_surfer import FetchResult

ROOT = Path(__file__).resolve().parents[1]


def _arrived(storage, user_id: str, *, uploaded_by: str | None, at: str) -> None:
    storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="telegram",
            source_ref=new_id("ref"),
            raw_content="текст материала",
            content_type="text",
            metadata_json={"uploaded_by": uploaded_by} if uploaded_by is not None else {},
            received_at=at,
        )
    )


def test_arrivals_are_counted_for_the_person_who_brought_them(storage) -> None:
    """Мутация: убрать условие по автору — в ответ попадут чужие материалы."""
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by="person-a", at="2026-08-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by="person-b", at="2026-08-01T11:00:00+00:00")

    where, params = storage._arrival_window(  # noqa: SLF001
        "tenant", None, None, uploaded_by="person-a"
    )
    count = storage.execute(f"SELECT COUNT(*) AS n FROM raw_objects WHERE {where}", tuple(params)).fetchone()[
        "n"
    ]

    assert count == 1, "надзор по человеку считает чужие материалы"


def test_material_without_an_author_is_counted_separately(storage) -> None:
    """Неизвестность называется вслух, а не превращается в ноль.

    Это и есть смысл правки: «у Ивана ноль» рядом с тремя тысячами документов
    неизвестного происхождения — не факт о человеке, а факт о том, что мы не
    знаем.
    """
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by="person-a", at="2026-08-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-07-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-07-02T10:00:00+00:00")

    assert storage.arrivals_without_an_author("tenant") == 2


def test_old_material_is_not_attributed_to_anybody(storage) -> None:
    """Догадка здесь означала бы приписать человеку чужие документы."""
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-07-01T10:00:00+00:00")

    where, params = storage._arrival_window(  # noqa: SLF001
        "tenant", None, None, uploaded_by="tenant"
    )
    count = storage.execute(f"SELECT COUNT(*) AS n FROM raw_objects WHERE {where}", tuple(params)).fetchone()[
        "n"
    ]

    assert count == 0, "материал без автора приписан владельцу архива"


def test_the_window_without_an_author_still_sees_everything(storage) -> None:
    """Обратная сторона: без указания автора надзор смотрит весь архив.

    Владельцу это и нужно; сузив ответ молча, мы отняли бы у него общий обзор.
    """
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by="person-a", at="2026-08-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-07-01T10:00:00+00:00")

    where, params = storage._arrival_window("tenant", None, None)  # noqa: SLF001
    count = storage.execute(f"SELECT COUNT(*) AS n FROM raw_objects WHERE {where}", tuple(params)).fetchone()[
        "n"
    ]

    assert count == 2


EXPECTED_INGEST_CALLS = [
    ("friday/api/files.py", "upload_file", "ingest_file", "actor.own_id"),
    ("friday/api/ingest.py", "ingest", "ingest_text", "actor.own_id"),
    ("friday/api/ingest.py", "ingest_url", "ingest_text", "actor.own_id"),
    ("friday/bulk_import.py", "_ingest_one", "ingest_file", "uploaded_by"),
    (
        "friday/execution_kernel/__init__.py",
        "ExecutionKernel._capture_web_sources",
        "ingest_text",
        "actor.own_id",
    ),
    ("friday/organs/importer/__init__.py", "_router.run_import", "ingest_text", "actor.own_id"),
    ("friday/server.py", "create_app.chat", "ingest_file", "actor.own_id"),
    ("friday/server.py", "create_app.chat", "ingest_file", "actor.own_id"),
    ("friday/server.py", "create_app.chat", "ingest_file", "actor.own_id"),
    ("friday/server.py", "create_app.chat", "ingest_text", "actor.own_id"),
]


class _IngestCallVisitor(ast.NodeVisitor):
    """Name every ingestion call and the expression that supplies its uploader."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.scope: list[str] = []
        self.dict_bindings: list[dict[str, ast.AST]] = []
        self.found: list[tuple[str, str, str, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API
        if self.dict_bindings:
            # A nested executable scope may retain and later mutate a local
            # mapping through a closure-like reference.  This inventory is not
            # a points-to analyser, so withdraw the outer proof conservatively.
            self._bindings.clear()
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:  # noqa: N802
        if self.dict_bindings:
            self._bindings.clear()
        self.scope.append(node.name)
        self.dict_bindings.append({})
        self.generic_visit(node)
        self.dict_bindings.pop()
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    @property
    def _bindings(self) -> dict[str, ast.AST]:
        return self.dict_bindings[-1] if self.dict_bindings else {}

    @staticmethod
    def _target_root_name(node: ast.AST) -> str:
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        return node.id if isinstance(node, ast.Name) else ""

    @classmethod
    def _target_names(cls, node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, ast.Starred):
            return cls._target_names(node.value)
        if isinstance(node, (ast.Tuple, ast.List)):
            names: set[str] = set()
            for item in node.elts:
                names.update(cls._target_names(item))
            return names
        root = cls._target_root_name(node)
        return {root} if root else set()

    @staticmethod
    def _same_expression(left: ast.AST, right: ast.AST) -> bool:
        return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)

    def _merge_bindings(self, *branches: dict[str, ast.AST]) -> dict[str, ast.AST]:
        if not branches:
            return {}
        common = dict(branches[0])
        for branch in branches[1:]:
            common = {
                name: value
                for name, value in common.items()
                if name in branch and self._same_expression(value, branch[name])
            }
        return common

    def _visit_branch(
        self,
        statements: list[ast.stmt],
        initial: dict[str, ast.AST],
    ) -> dict[str, ast.AST]:
        self.dict_bindings[-1] = dict(initial)
        for statement in statements:
            self.visit(statement)
        return dict(self._bindings)

    def _invalidate_references(self, node: ast.AST | None) -> None:
        if not self.dict_bindings or node is None:
            return
        referenced = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
        for name in referenced & self._bindings.keys():
            self._bindings.pop(name, None)

    def _assign_target(self, target: ast.AST, value: ast.AST | None) -> None:
        if not self.dict_bindings:
            return
        if isinstance(target, ast.Name):
            if isinstance(value, ast.Dict):
                self._bindings[target.id] = value
            else:
                self._bindings.pop(target.id, None)
            return
        if isinstance(target, ast.Starred):
            self._assign_target(target.value, None)
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._assign_target(item, None)
            return
        root = self._target_root_name(target)
        if root:
            self._bindings.pop(root, None)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802 - ast API
        # A literal binding is evidence only until that exact local is replaced,
        # aliased or mutated.  Keeping an earlier literal after ``meta = make()``
        # made this inventory green while the real uploader was unknowable.
        referenced = {
            item.id for item in ast.walk(node.value) if isinstance(item, ast.Name)
        } & self._bindings.keys()
        self.visit(node.value)
        for target in node.targets:
            self._assign_target(target, node.value)
        assigned_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if isinstance(node.value, ast.Dict) and len(node.targets) > 1:
            # ``left = right = {...}`` creates two mutable aliases.  A later
            # write through either name cannot be attributed to just that name
            # by this local syntactic proof, so neither remains trusted.
            for name in assigned_names:
                self._bindings.pop(name, None)
        for name in referenced - assigned_names:
            self._bindings.pop(name, None)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802 - ast API
        referenced = (
            {item.id for item in ast.walk(node.value) if isinstance(item, ast.Name)} & self._bindings.keys()
            if node.value is not None
            else set()
        )
        if node.value is not None:
            self.visit(node.value)
        self._assign_target(node.target, node.value)
        assigned_name = node.target.id if isinstance(node.target, ast.Name) else ""
        for name in referenced - {assigned_name}:
            self._bindings.pop(name, None)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802 - ast API
        self.visit(node.value)
        self._assign_target(node.target, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:  # noqa: N802 - ast API
        referenced = {
            item.id for item in ast.walk(node.value) if isinstance(item, ast.Name)
        } & self._bindings.keys()
        self.visit(node.value)
        self._assign_target(node.target, node.value)
        if referenced:
            # ``alias := metadata`` exposes the same mutable object through a
            # second name.  Without a full points-to analysis the only honest
            # static verdict is to withdraw both proofs.
            for name in referenced:
                self._bindings.pop(name, None)
            if isinstance(node.target, ast.Name):
                self._bindings.pop(node.target.id, None)

    def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802 - ast API
        for target in node.targets:
            self._assign_target(target, None)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - ast API
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
            self._bindings.pop(bound, None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 - ast API
        for alias in node.names:
            self._bindings.pop(alias.asname or alias.name, None)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._assign_target(item.optional_vars, None)
        for statement in node.body:
            self.visit(statement)
        # ``__exit__`` / ``__aexit__`` may suppress an exception raised before
        # a later syntactic restore.  No final body state is a safe proof for
        # code that follows the context manager.
        self._bindings.clear()

    def visit_With(self, node: ast.With) -> None:  # noqa: N802 - ast API
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802 - ast API
        self._visit_with(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802 - ast API
        self._invalidate_references(node.body)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802 - ast API
        self._invalidate_references(node.value)
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:  # noqa: N802 - ast API
        self._invalidate_references(node.value)
        self.generic_visit(node)

    visit_YieldFrom = visit_Yield

    def visit_If(self, node: ast.If) -> None:  # noqa: N802 - ast API
        self.visit(node.test)
        if not self.dict_bindings:
            return
        initial = dict(self._bindings)
        body = self._visit_branch(node.body, initial)
        otherwise = self._visit_branch(node.orelse, initial)
        self.dict_bindings[-1] = self._merge_bindings(body, otherwise)

    def visit_Match(self, node: ast.Match) -> None:  # noqa: N802 - ast API
        self.visit(node.subject)
        if not self.dict_bindings:
            return
        initial = dict(self._bindings)
        branches = [initial]
        for case in node.cases:
            self.dict_bindings[-1] = dict(initial)
            for pattern in ast.walk(case.pattern):
                if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name:
                    self._bindings.pop(pattern.name, None)
                elif isinstance(pattern, ast.MatchMapping) and pattern.rest:
                    self._bindings.pop(pattern.rest, None)
            if case.guard is not None:
                self.visit(case.guard)
            branches.append(self._visit_branch(case.body, self._bindings))
        self.dict_bindings[-1] = self._merge_bindings(*branches)

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self.visit(node.iter)
        else:
            self.visit(node.test)
        if not self.dict_bindings:
            return
        initial = dict(self._bindings)
        self.dict_bindings[-1] = dict(initial)
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._assign_target(node.target, None)
        body = self._visit_branch(node.body, self._bindings)
        otherwise = self._visit_branch(node.orelse, self._merge_bindings(initial, body))
        # A loop may execute zero times, break before ``else``, or finish it.
        self.dict_bindings[-1] = self._merge_bindings(initial, body, otherwise)
        if any(isinstance(item, (ast.Break, ast.Continue)) for item in ast.walk(ast.Module(body=node.body))):
            # A break/continue may bypass a later restore in the same body.  A
            # linear AST walk cannot identify the actual exit-state safely.
            self._bindings.clear()

    def visit_For(self, node: ast.For) -> None:  # noqa: N802 - ast API
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802 - ast API
        self._visit_loop(node)

    def visit_While(self, node: ast.While) -> None:  # noqa: N802 - ast API
        self._visit_loop(node)

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802 - ast API
        if not self.dict_bindings:
            return
        initial = dict(self._bindings)
        body = self._visit_branch(node.body, initial)
        completed = self._visit_branch(node.orelse, body)
        exits = [completed]
        for handler in node.handlers:
            if handler.type is not None:
                self.visit(handler.type)
            self.dict_bindings[-1] = {}
            if handler.name:
                self._bindings.pop(handler.name, None)
            exits.append(self._visit_branch(handler.body, self._bindings))
        merged = self._merge_bindings(*exits)
        self.dict_bindings[-1] = self._visit_branch(node.finalbody, merged)

    visit_TryStar = visit_Try

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        outputs: tuple[ast.AST, ...],
    ) -> None:
        if not self.dict_bindings:
            return
        initial = dict(self._bindings)
        preserved_shadowed: dict[str, ast.AST] = {}
        self.dict_bindings[-1] = dict(initial)
        for generator in generators:
            self.visit(generator.iter)
            for root in self._target_names(generator.target):
                if root in self._bindings:
                    preserved_shadowed[root] = self._bindings[root]
            self._assign_target(generator.target, None)
            for condition in generator.ifs:
                self.visit(condition)
        for output in outputs:
            self.visit(output)
        # Comprehension targets are inner-scope locals in Python 3.  Restore
        # only proofs that survived evaluation of the corresponding iterable;
        # mutations of other captured mappings remain withdrawn.
        self._bindings.update(preserved_shadowed)

    def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802 - ast API
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802 - ast API
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802 - ast API
        if not self.dict_bindings:
            return
        first, *deferred_generators = node.generators
        # Python evaluates only the outermost iterable at generator creation.
        # Preserve any invalidation caused there; everything else is deferred.
        self.visit(first.iter)
        surviving_outer = dict(self._bindings)
        # The generator body runs later, after arbitrary rebinding in the
        # enclosing scope.  Only literals written directly at the eventual
        # ingestion call remain self-proving; captured mutable locals do not.
        self.dict_bindings[-1] = {}
        self._assign_target(first.target, None)
        for condition in first.ifs:
            self.visit(condition)
        for generator in deferred_generators:
            self.visit(generator.iter)
            self._assign_target(generator.target, None)
            for condition in generator.ifs:
                self.visit(condition)
        self.visit(node.elt)
        self.dict_bindings[-1] = surviving_outer

    def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802 - ast API
        self._visit_comprehension(node.generators, (node.key, node.value))

    def _uploader_expression(self, metadata: ast.AST | None) -> str:
        if isinstance(metadata, ast.Name):
            for bindings in reversed(self.dict_bindings):
                if metadata.id in bindings:
                    return self._uploader_expression(bindings[metadata.id])
            return "<metadata is not a literal or locally bound dict>"
        if not isinstance(metadata, ast.Dict):
            return "<metadata is not a literal or locally bound dict>"

        def may_override_uploader(value: ast.AST) -> bool:
            if isinstance(value, ast.IfExp):
                return may_override_uploader(value.body) or may_override_uploader(value.orelse)
            if not isinstance(value, ast.Dict):
                return True
            for nested_key, nested_value in zip(value.keys, value.values, strict=True):
                if nested_key is None:
                    if may_override_uploader(nested_value):
                        return True
                    continue
                if not isinstance(nested_key, ast.Constant):
                    return True
                if nested_key.value == "uploaded_by":
                    return True
            return False

        uploader: ast.AST | None = None
        for key, value in zip(metadata.keys, metadata.values, strict=True):
            # An unpack or computed key before the explicit key is harmless:
            # Python's later literal wins.  After it, the runtime value could
            # be replaced and is no longer statically closed.
            if key is None:
                if uploader is not None and may_override_uploader(value):
                    return "<metadata is not a literal or locally bound dict>"
                continue
            if not isinstance(key, ast.Constant):
                if uploader is not None:
                    return "<metadata is not a literal or locally bound dict>"
                continue
            if isinstance(key, ast.Constant) and key.value == "uploaded_by":
                if uploader is not None:
                    return "<metadata is not a literal or locally bound dict>"
                uploader = value
        if uploader is not None:
            return ast.unparse(uploader)
        return "<uploaded_by is missing>"

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name in {"ingest_text", "ingest_file"}:
            metadata = next((item.value for item in node.keywords if item.arg == "metadata"), None)
            uploader = self._uploader_expression(metadata)
            self.found.append(
                (
                    str(self.path.relative_to(ROOT)),
                    ".".join(self.scope),
                    name,
                    uploader,
                )
            )
        # Any call may retain or mutate a dictionary passed to it.  The uploader
        # is recorded above at the ingestion boundary itself; after that point a
        # reused binding must be proved again instead of inheriting stale syntax.
        if isinstance(node.func, ast.Attribute):
            root = self._target_root_name(node.func.value)
            if root:
                self._bindings.pop(root, None)
        for argument in node.args:
            self._invalidate_references(argument)
        for keyword in node.keywords:
            self._invalidate_references(keyword.value)
        self.generic_visit(node)


def _ingest_calls_with_uploaders() -> list[tuple[str, str, str, str]]:
    found: list[tuple[str, str, str, str]] = []
    for path in (ROOT / "friday").rglob("*.py"):
        visitor = _IngestCallVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.extend(visitor.found)
    return sorted(found)


def test_every_intake_road_records_the_uploader() -> None:
    """Полный переучёт вызовов, а не поиск слова по нескольким модулям.

    Прежний тест был зелёным, когда четыре из восьми тогдашних дорог не писали автора:
    достаточно было встретить `uploaded_by` где-нибудь в том же модуле. Здесь
    новая дорога, перенос вызова или неверный `actor.user_id` меняют точную
    матрицу и требуют осознанного решения.

    Неаутентифицированный дисковый импорт — единственное исключение: значение
    приходит параметром и может быть `None`. Это явное «неизвестно», а не
    выдуманный из арендатора человек.
    """
    assert _ingest_calls_with_uploaders() == sorted(EXPECTED_INGEST_CALLS)


def _synthetic_ingest_uploader(source: str) -> str:
    visitor = _IngestCallVisitor(ROOT / "synthetic_ingest_inventory.py")
    visitor.visit(ast.parse(source))
    assert len(visitor.found) == 1
    return visitor.found[0][-1]


def test_ingest_inventory_accepts_an_unchanged_local_literal() -> None:
    assert (
        _synthetic_ingest_uploader(
            """
def road(actor, pipeline):
    metadata = {"uploaded_by": actor.own_id}
    pipeline.ingest_file(metadata=metadata)
"""
        )
        == "actor.own_id"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "metadata = build_metadata(actor.own_id)",
        "metadata['uploaded_by'] = actor.user_id",
        "alias = metadata",
        'metadata = alias = {"uploaded_by": actor.own_id}\n    alias["uploaded_by"] = actor.user_id',
        'metadata = holder.value = {"uploaded_by": actor.own_id}\n    holder.value["uploaded_by"] = actor.user_id',
        '(alias := metadata)\n    alias["uploaded_by"] = actor.user_id',
        "if actor.source:\n        metadata = build_metadata(actor.own_id)",
        'for item in actor.sources:\n        metadata = {"uploaded_by": actor.user_id}',
    ],
    ids=[
        "replacement",
        "subscript-mutation",
        "mutable-alias",
        "chained-mutable-alias",
        "chained-attribute-alias",
        "walrus-mutable-alias",
        "conditional-replacement",
        "zero-iteration-loop",
    ],
)
def test_ingest_inventory_rejects_a_stale_literal_after_mutation(mutation: str) -> None:
    source = f"""\
def road(actor, pipeline):
    metadata = {{"uploaded_by": actor.own_id}}
    {mutation}
    pipeline.ingest_file(metadata=metadata)
"""

    assert _synthetic_ingest_uploader(source) == "<metadata is not a literal or locally bound dict>"


@pytest.mark.parametrize(
    "literal",
    [
        '{"uploaded_by": actor.own_id, "uploaded_by": actor.user_id}',
        '{"uploaded_by": actor.own_id, **{"uploaded_by": actor.user_id}}',
    ],
    ids=["duplicate-key", "later-unpack"],
)
def test_ingest_inventory_rejects_a_literal_with_an_overridable_uploader(literal: str) -> None:
    source = f"""\
def road(actor, pipeline):
    metadata = {literal}
    pipeline.ingest_file(metadata=metadata)
"""

    assert _synthetic_ingest_uploader(source) == "<metadata is not a literal or locally bound dict>"


@pytest.mark.parametrize(
    "expression",
    [
        "[pipeline.ingest_file(metadata=metadata) for metadata in sources]",
        "{pipeline.ingest_file(metadata=metadata) for metadata in sources}",
        "(pipeline.ingest_file(metadata=metadata) for metadata in sources)",
        "{str(index): pipeline.ingest_file(metadata=metadata) for index, metadata in enumerate(sources)}",
    ],
    ids=["list", "set", "generator", "dict"],
)
def test_ingest_inventory_respects_comprehension_target_shadowing(expression: str) -> None:
    source = f"""\
def road(actor, pipeline, sources):
    metadata = {{"uploaded_by": actor.own_id}}
    {expression}
"""

    assert _synthetic_ingest_uploader(source) == "<metadata is not a literal or locally bound dict>"


def test_ingest_inventory_does_not_freeze_a_lazy_generator_capture() -> None:
    source = """\
def road(actor, pipeline, sources):
    metadata = {"uploaded_by": actor.own_id}
    work = (pipeline.ingest_file(metadata=metadata) for _ in sources)
    metadata = build_metadata(actor.user_id)
    list(work)
"""

    assert _synthetic_ingest_uploader(source) == "<metadata is not a literal or locally bound dict>"


@pytest.mark.parametrize(
    "iterable",
    ["retain_or_mutate(metadata)", 'metadata.pop("uploaded_by")'],
    ids=["argument-escape", "method-mutation"],
)
def test_ingest_inventory_keeps_eager_generator_iterable_side_effects(iterable: str) -> None:
    source = f"""\
def road(actor, pipeline):
    metadata = {{"uploaded_by": actor.own_id}}
    (item for item in {iterable})
    pipeline.ingest_file(metadata=metadata)
"""

    assert _synthetic_ingest_uploader(source) == "<metadata is not a literal or locally bound dict>"


@pytest.mark.parametrize(
    "body",
    [
        """try:
        raise RuntimeError
    except RuntimeError as metadata:
        pipeline.ingest_file(metadata=metadata)""",
        """with manager() as metadata:
        pipeline.ingest_file(metadata=metadata)""",
        """match source:
        case {"metadata": metadata}:
            pipeline.ingest_file(metadata=metadata)""",
        """import json as metadata
    pipeline.ingest_file(metadata=metadata)""",
    ],
    ids=["except-target", "with-target", "match-capture", "import-alias"],
)
def test_ingest_inventory_rejects_control_flow_rebinding(body: str) -> None:
    source = f"""\
def road(actor, pipeline, source, manager):
    metadata = {{"uploaded_by": actor.own_id}}
    {body}
"""

    assert _synthetic_ingest_uploader(source) == "<metadata is not a literal or locally bound dict>"


@pytest.mark.parametrize(
    "body",
    [
        """for item in sources:
        metadata = build_metadata(actor.user_id)
        break
        metadata = {"uploaded_by": actor.own_id}
    pipeline.ingest_file(metadata=metadata)""",
        """for item in sources:
        metadata = build_metadata(actor.user_id)
        continue
        metadata = {"uploaded_by": actor.own_id}
    pipeline.ingest_file(metadata=metadata)""",
        """try:
        metadata = build_metadata(actor.user_id)
        risky()
    except RuntimeError:
        pipeline.ingest_file(metadata=metadata)""",
        """with manager():
        metadata = build_metadata(actor.user_id)
        risky()
        metadata = {"uploaded_by": actor.own_id}
    pipeline.ingest_file(metadata=metadata)""",
    ],
    ids=["break-skips-restore", "continue-skips-restore", "try-prefix", "suppressed-with-prefix"],
)
def test_ingest_inventory_rejects_ambiguous_control_flow_exit_state(body: str) -> None:
    source = f"""\
def road(actor, pipeline, sources, manager, risky):
    metadata = {{"uploaded_by": actor.own_id}}
    {body}
"""

    assert _synthetic_ingest_uploader(source) == "<metadata is not a literal or locally bound dict>"


def _raw_metadata(storage, raw_id: str, user_id: str) -> dict:
    raw = storage.get_raw_object(raw_id, user_id)
    assert raw is not None, raw_id
    metadata = raw.get("metadata_json") or {}
    return json.loads(metadata) if isinstance(metadata, str) else dict(metadata)


def test_authenticated_intake_roads_record_the_person_not_the_shared_tenant(settings, tmp_path) -> None:
    """Все семь actor-aware дорог исполняются с различными tenant/person.

    Обычный owner-token этого не доказывает: у него оба идентификатора равны.
    Мутация `actor.own_id -> actor.user_id` на любой дороге должна дать общий
    tenant вместо человека и покрасить соответствующий элемент матрицы.
    """
    from friday.bulk_import import plan_import, run_import

    person_id = "person-forward-author"
    shared = dataclasses.replace(settings, shared_archive=True)
    app = create_app(shared)

    class Surfer:
        async def fetch(self, _url: str, **_kwargs: object) -> FetchResult:
            text = "Синтетическая страница для проверки автора загрузки. " * 8
            return FetchResult(
                url="https://example.test/direct",
                title="Прямая страница",
                text=text,
                text_length=len(text),
                status_code=200,
            )

        async def research(self, query: str, *, max_sources: int = 3) -> dict:
            del max_sources
            text = ("Синтетический результат веб-поиска с явным автором. " * 8).strip()
            return {
                "query": query,
                "outbound_attempted": True,
                "sources": [
                    {
                        "url": "https://example.org/research",
                        "title": "Найденная страница",
                        "text": text,
                        "text_length": len(text),
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    }
                ],
                "summary": "ok",
                "requested_sources": 1,
                "completed_sources": 1,
                "timed_out_sources": 0,
                "failed_sources": 0,
                "search_timed_out": False,
            }

    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {shared.api_token}"}
        created = client.post(
            "/api/admin/users",
            json={"id": person_id, "preset_key": "admin"},
            headers=owner,
        )
        assert created.status_code < 300, created.text
        issued = client.post("/api/admin/tokens", json={"user_id": person_id}, headers=owner)
        assert issued.status_code == 200, issued.text
        person = {"Authorization": f"Bearer {issued.json()['token']}"}

        surfer = Surfer()
        app.state.web_surfer = surfer
        app.state.kernel.web_surfer = surfer

        async def quiet_chat(*_args, **_kwargs):
            return {"conversation_id": "conv-author-matrix", "content": "ok"}

        app.state.agent.chat = quiet_chat

        raw_ids: dict[str, str] = {}
        pasted = client.post(
            "/api/ingest",
            json={"content": "Текстовая проверка полного маршрута авторства." * 4},
            headers=person,
        )
        assert pasted.status_code == 200, pasted.text
        raw_ids["api text"] = pasted.json()["raw_object_id"]

        uploaded = client.post(
            "/api/files",
            files={
                "file": (
                    "author-matrix.txt",
                    ("Содержимое файла для полной проверки автора. " * 5).encode(),
                    "text/plain",
                )
            },
            headers=person,
        )
        assert uploaded.status_code == 200, uploaded.text
        raw_ids["api file"] = uploaded.json()["raw_object_id"]

        by_url = client.post(
            "/api/ingest/url",
            json={"url": "https://example.test/direct"},
            headers=person,
        )
        assert by_url.status_code == 200, by_url.text
        raw_ids["api url"] = by_url.json()["raw_object_id"]

        chat_text = client.post(
            "/api/chat",
            json={
                "message": "Сообщение в чате для проверки автора поступления.",
                "source_ref": "author-matrix:chat-text",
                "enable_tools": False,
            },
            headers=person,
        )
        assert chat_text.status_code == 200, chat_text.text
        assert "raw_object_id" not in chat_text.json()["ingestion"]
        chat_text_raw = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE user_id=? AND source_ref=?",
            (LEGACY_OWNER_USER_ID, "author-matrix:chat-text"),
        ).fetchone()
        assert chat_text_raw is not None
        raw_ids["chat text"] = str(chat_text_raw["id"])

        chat_file = client.post(
            "/api/chat",
            json={
                "message": "",
                "source_ref": "author-matrix:chat-file",
                "enable_tools": False,
                "document": {
                    "filename": "chat-author.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(
                        ("Файл из чата для проверки автора. " * 5).encode()
                    ).decode(),
                },
            },
            headers=person,
        )
        assert chat_file.status_code == 200, chat_file.text
        assert "raw_object_id" not in chat_file.json()["file_ingestion"]
        chat_file_raw = app.state.storage.execute(
            """SELECT id FROM raw_objects
               WHERE user_id=? AND content_type='file'
                 AND json_extract(metadata_json,'$.uploaded_by')=?
                 AND json_extract(metadata_json,'$.filename')='chat-author.txt'
               ORDER BY rowid DESC LIMIT 1""",
            (LEGACY_OWNER_USER_ID, person_id),
        ).fetchone()
        assert chat_file_raw is not None
        raw_ids["chat file"] = str(chat_file_raw["id"])

        actor = app.state.auth_service.actor_for_user(person_id, source="test")
        researched = asyncio.run(
            app.state.kernel.execute("web_research", {"query": "синтетическая проверка"}, actor=actor)
        )
        assert researched.success, researched.error
        raw_ids["web research"] = researched.data["captured"][0]["raw_object_id"]

        calendar = "\r\n".join(
            (
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "UID:author-matrix@example.test",
                "DTSTART:20260806",
                "SUMMARY:Проверка автора импорта",
                "END:VEVENT",
                "END:VCALENDAR",
            )
        )
        imported = client.post(
            "/api/import",
            files={"file": ("author-matrix.ics", calendar.encode(), "text/calendar")},
            headers=person,
        )
        assert imported.status_code == 200, imported.text
        organ_raw = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE user_id=? AND source_ref=?",
            (LEGACY_OWNER_USER_ID, "ics:author-matrix@example.test"),
        ).fetchone()
        assert organ_raw is not None
        raw_ids["organ import"] = str(organ_raw["id"])

        disk_file = tmp_path / "known-author.txt"
        disk_file.write_text("Файл CLI с явно названным автором. " * 5, encoding="utf-8")
        disk = asyncio.run(
            run_import(
                app.state.ingestion,
                LEGACY_OWNER_USER_ID,
                plan_import(disk_file, max_bytes=shared.max_upload_bytes),
                uploaded_by=person_id,
            )
        )
        assert len(disk) == 1 and disk[0].status == "ingested"
        raw_ids["disk import"] = disk[0].raw_object_id

        assert set(raw_ids) == {
            "api text",
            "api file",
            "api url",
            "chat text",
            "chat file",
            "web research",
            "organ import",
            "disk import",
        }
        for road, raw_id in raw_ids.items():
            metadata = _raw_metadata(app.state.storage, raw_id, LEGACY_OWNER_USER_ID)
            assert metadata.get("uploaded_by") == person_id, (
                f"{road}: записан арендатор или неизвестность вместо человека: {metadata}"
            )


def test_disk_import_without_an_authenticated_actor_records_explicit_unknown(
    settings, storage, tmp_path
) -> None:
    """CLI не приписывает материал целевому tenant по догадке.

    `run_import` остаётся обратно совместимым: существующие вызовы без нового
    аргумента работают, но кладут JSON `null`. Явный `--uploaded-by` доступен
    тому, кто действительно знает человека; это не исторический backfill.
    """
    from friday.bulk_import import plan_import, run_import
    from friday.cli import build_parser
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    path = tmp_path / "unknown-author.txt"
    path.write_text("Материал, чей автор при запуске CLI неизвестен. " * 5, encoding="utf-8")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    outcome = asyncio.run(
        run_import(pipeline, "shared-tenant", plan_import(path, max_bytes=settings.max_upload_bytes))
    )[0]

    metadata = _raw_metadata(storage, outcome.raw_object_id, "shared-tenant")
    assert "uploaded_by" in metadata, "неизвестность снова представлена отсутствующим контрактом"
    assert metadata["uploaded_by"] is None, "целевой tenant выдуман как автор"

    attributed = asyncio.run(
        run_import(
            pipeline,
            "shared-tenant",
            plan_import(path, max_bytes=settings.max_upload_bytes),
            uploaded_by="person-named-too-late",
        )
    )[0]
    # Exact uploader provenance is also the ownership boundary for later
    # conversation pointers.  A previously explicit-unknown Raw Object cannot
    # be borrowed by a now-named person merely because its bytes match.  Keep
    # the unknown row unchanged and create a distinct attributed provenance
    # row over the same content-addressed bytes.
    assert attributed.status == "ingested"
    assert attributed.raw_object_id != outcome.raw_object_id
    assert _raw_metadata(storage, outcome.raw_object_id, "shared-tenant")["uploaded_by"] is None, (
        "повторный импорт незаметно превратился в исторический backfill"
    )
    assert (
        _raw_metadata(storage, attributed.raw_object_id, "shared-tenant")["uploaded_by"]
        == "person-named-too-late"
    )

    parsed = build_parser().parse_args(
        ["import", str(path), "--uploaded-by", "person-who-really-ran-the-import"]
    )
    assert parsed.uploaded_by == "person-who-really-ran-the-import"


def test_the_marker_is_json_readable(storage) -> None:
    """Признак читается из метаданных ровно тем выражением, что стоит в запросе."""
    storage.ensure_user("tenant")
    _arrived(storage, "tenant", uploaded_by="person-a", at="2026-08-01T10:00:00+00:00")

    row = storage.execute(
        "SELECT json_extract(metadata_json,'$.uploaded_by') AS who FROM raw_objects LIMIT 1"
    ).fetchone()

    assert row["who"] == "person-a"
    assert (
        json.loads(storage.execute("SELECT metadata_json AS m FROM raw_objects LIMIT 1").fetchone()["m"])[
            "uploaded_by"
        ]
        == "person-a"
    )


@pytest.mark.asyncio
async def test_the_tool_itself_carries_the_count_to_the_model(settings, storage):
    """Счётчик безымянных загрузок доезжает до МОДЕЛИ, а не просто существует.

    Найдено мутацией: удаление строки, которая кладёт число в ответ, оставляло все
    тесты зелёными — один проверял хранилище, другой формулировку, и между ними
    зияла дыра ровно в том месте, где эти двое соединяются. Это на проекте
    отдельный класс: «проверять не механизм, а что он подключён».
    """
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("tenant", preset_key="owner")
    storage.ensure_user("bob", preset_key="user")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-08-01T10:00:00+00:00")
    _arrived(storage, "tenant", uploaded_by=None, at="2026-08-01T11:00:00+00:00")

    auth = AuthorizationService(storage, shared_tenant="tenant")
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    owner = auth.actor_for_user("tenant", source="test")

    result = await kernel.execute("user_activity", {"person": "bob"}, actor=owner)
    rendered = str(result.data or "") + str(result.to_llm_message() or "")
    assert "без отметки о том, кто их загрузил" in rendered, (
        "модель не узнала, что у части загрузок автор неизвестен — и объявит, "
        f"что человек ничего не присылал: {rendered[:300]}"
    )
    # Проверяется ЧИСЛО, а не наличие фразы. Осторожное умолчание (ключа нет —
    # считаем, что безымянные есть) выдаёт ту же фразу, и первая редакция этого
    # теста мутацию не поймала: отключённый счётчик выглядел как подключённый.
    # Двойка приходит только из настоящего запроса к хранилищу.
    assert "2 материалов без отметки" in rendered, (
        f"в ответе не настоящее число безымянных загрузок: {rendered[:300]}"
    )
