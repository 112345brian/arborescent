"""Where a module's functions use a real injectable effect, and what parameters stand around each use (#233).

Which expressions are effects is ``effect_facts``'s business. This module records, for every use that runs inside a
function, the chain of functions and classes around it with their parameters, so ``effect-seam-bypassed`` can decide,
under the policy's own names, whether one of them was an injected stand-in for that effect. A use that is the fallback
for a missing parameter (``now or datetime.now``, ``if now is None:``) carries that parameter in ``guards``."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from .effect_categories import INJECTABLE_EFFECTS, effect_category_of
from .effect_facts import EffectSite, runs

__all__ = ["SeamFacts", "SeamFrame", "SeamParam", "SeamUse", "seam_facts"]

# `call_facts.dotted_name`, handed in by `facts`, which already imports it.
Dotted = Callable[[ast.expr, dict[str, str]], str | None]
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class SeamParam:
    name: str
    annotation: str = ""  # the dotted annotation, looking through Optional/`| None` and strings
    default: str = ""  # the effect category of a default that is an effect function (an injection default, #101)


@dataclass(frozen=True)
class SeamFrame:
    """A function, or a class (whose parameters are its ``__init__``'s, or its dataclass fields)."""

    name: str  # dotted from the module: "f", "Class.method", "outer.inner"
    parent: int  # index of the enclosing frame, -1 at module level
    params: tuple[SeamParam, ...] = ()
    is_class: bool = False


@dataclass(frozen=True)
class SeamUse:
    frame: int  # the function whose body runs it
    line: int
    category: str
    target: str
    guards: tuple[str, ...] = ()  # names whose absence this use is the fallback for


@dataclass(frozen=True)
class SeamFacts:
    frames: tuple[SeamFrame, ...] = ()
    uses: tuple[SeamUse, ...] = ()


def _name_of(node: ast.expr) -> str | None:
    """``now``, ``self.now`` and ``self._now`` all name the seam ``now``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr.lstrip("_")
    return None


def _mentioned(nodes: Sequence[ast.expr]) -> frozenset[str]:
    return frozenset(name for root in nodes for node in ast.walk(root) if isinstance(node, ast.expr) and (name := _name_of(node)) is not None)


def _tested(test: ast.expr) -> tuple[frozenset[str], frozenset[str]]:
    """``(names absent when the test holds, names absent when it does not)``: where a fallback for them may sit."""
    nothing: frozenset[str] = frozenset()
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        present_absent = _tested(test.operand)
        return present_absent[1], present_absent[0]
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None:
        name = _name_of(test.left)
        found = nothing if name is None else frozenset({name})
        if isinstance(test.ops[0], ast.Is | ast.Eq):
            return found, nothing
        return nothing, found
    name = _name_of(test)
    return nothing, nothing if name is None else frozenset({name})


def _leaves(body: Sequence[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], ast.Return | ast.Raise | ast.Continue | ast.Break)


Guarded = tuple[ast.AST, frozenset[str]]


def _statements(body: Sequence[ast.stmt], guards: frozenset[str]) -> Iterator[Guarded]:
    """A block in order: after ``if now is not None: return now``, the rest of it is the fallback for ``now``."""
    for statement in body:
        yield statement, guards
        if isinstance(statement, ast.If) and not statement.orelse and _leaves(statement.body):
            guards = guards | _tested(statement.test)[1]


def _guarded_children(node: ast.AST, guards: frozenset[str]) -> Iterator[Guarded]:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        for position, value in enumerate(node.values):
            yield value, guards | _mentioned(node.values[:position])
        return
    if isinstance(node, ast.If | ast.IfExp):
        holds, fails = _tested(node.test)
        yield node.test, guards
        for branch, absent in ((node.body, holds), (node.orelse, fails)):
            if isinstance(branch, list):
                yield from _statements(branch, guards | absent)
            else:
                yield branch, guards | absent
        return
    for _, value in ast.iter_fields(node):
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            yield from _statements(value, guards)
        else:
            yield from ((item, guards) for item in (value if isinstance(value, list) else [value]) if isinstance(item, ast.AST))


class _Context(NamedTuple):
    """Where a node sits: the function that runs it (-1: none), the guards on it, and the frame a nested def hangs from."""

    frame: int
    guards: frozenset[str]
    parent: int
    prefix: str
    bound: frozenset[str]  # parameter names of the enclosing functions, which shadow an imported name


def _annotation(node: ast.expr | None, aliases: dict[str, str], dotted: Dotted) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except (SyntaxError, ValueError):
            return ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        sides = [side for side in (node.left, node.right) if not (isinstance(side, ast.Constant) and side.value is None)]
        return next((found for side in sides if (found := _annotation(side, aliases, dotted))), "")
    if isinstance(node, ast.Subscript) and (dotted(node.value, aliases) or "").rsplit(".", 1)[-1] in {"Optional", "Annotated"}:
        inner = node.slice
        return _annotation(inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner, aliases, dotted)
    return "" if node is None else dotted(node, aliases) or ""


def _default_category(node: ast.expr | None, aliases: dict[str, str], dotted: Dotted) -> str:
    """``now=time.time`` and ``now=lambda: datetime.now()``: the category of the effect function the default stands for."""
    if isinstance(node, ast.Lambda) and isinstance(node.body, ast.Call):
        node = node.body.func
    if not isinstance(node, ast.Name | ast.Attribute):
        return ""
    category = effect_category_of(dotted(node, aliases) or "")
    return category if category in INJECTABLE_EFFECTS else ""


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str], dotted: Dotted) -> list[SeamParam]:
    """Named parameters only: ``*args`` and ``**kwargs`` inject nothing in particular."""
    spec = function.args
    positional = [*spec.posonlyargs, *spec.args]
    defaults: list[ast.expr | None] = [*[None] * (len(positional) - len(spec.defaults)), *spec.defaults, *spec.kw_defaults]
    return [
        SeamParam(arg.arg, _annotation(arg.annotation, aliases, dotted), _default_category(default, aliases, dotted))
        for arg, default in zip([*positional, *spec.kwonlyargs], defaults, strict=True)
    ]


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, aliases: dict[str, str], dotted: Dotted) -> set[str]:
    return {(dotted(d.func if isinstance(d, ast.Call) else d, aliases) or "").rsplit(".", 1)[-1] for d in node.decorator_list}


def _class_parameters(node: ast.ClassDef, aliases: dict[str, str], dotted: Dotted) -> list[SeamParam]:
    init = next((s for s in node.body if isinstance(s, _FUNCTIONS) and s.name == "__init__"), None)
    if init is not None:
        return _parameters(init, aliases, dotted)[1:]
    if "dataclass" not in _decorator_names(node, aliases, dotted):
        return []
    return [
        SeamParam(s.target.id, _annotation(s.annotation, aliases, dotted), _default_category(s.value, aliases, dotted))
        for s in node.body
        if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
    ]


def _root(node: ast.AST) -> str:
    while isinstance(node, ast.Attribute | ast.Call):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else ""


class _Walk:
    """An explicit stack, not recursion: a long ``a + b + ...`` chain is as deep as it is long."""

    def __init__(self, sites: dict[int, list[tuple[str, str]]], aliases: dict[str, str], dotted: Dotted) -> None:
        self.sites = sites
        self.aliases = aliases
        self.dotted = dotted
        self.frames: list[SeamFrame] = []
        self.uses: set[SeamUse] = set()
        self.stack: list[tuple[ast.AST, _Context]] = []

    def run(self, tree: ast.Module) -> None:
        top = _Context(-1, frozenset(), -1, "", frozenset())
        self.stack = [(statement, top) for statement in tree.body]
        while self.stack:
            node, at = self.stack.pop()
            if isinstance(node, _FUNCTIONS):
                self._function(node, at)
            elif isinstance(node, ast.ClassDef):
                self._class(node, at)
            else:
                self._record(node, at)
                self.stack.extend((child, at._replace(guards=guards)) for child, guards in _guarded_children(node, at.guards))

    def _record(self, node: ast.AST, at: _Context) -> None:
        if at.frame < 0 or id(node) not in self.sites or _root(node) in at.bound:
            return
        line = getattr(node, "lineno", 0)
        self.uses.update(SeamUse(at.frame, line, category, target, tuple(sorted(at.guards))) for category, target in self.sites[id(node)])

    def _class(self, node: ast.ClassDef, at: _Context) -> None:
        """A class body runs where the class statement is; its functions hang from the class's own frame."""
        self.frames.append(SeamFrame(at.prefix + node.name, at.parent, tuple(_class_parameters(node, self.aliases, self.dotted)), True))
        inside = at._replace(parent=len(self.frames) - 1, prefix=f"{at.prefix}{node.name}.")
        self.stack.extend((child, at) for child in (*node.decorator_list, *node.bases, *node.keywords))
        self.stack.extend((statement, inside) for statement in node.body)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, at: _Context) -> None:
        """Decorators and defaults run outside the function, so only its body belongs to its frame."""
        self.stack.extend((child, at) for child in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults) if child is not None)
        parent = at.parent
        in_class = parent >= 0 and self.frames[parent].is_class
        if in_class and _decorator_names(node, self.aliases, self.dotted) & {"staticmethod", "classmethod"}:
            parent = self.frames[parent].parent  # no instance, so nothing injected into one
        params = tuple(_parameters(node, self.aliases, self.dotted))
        self.frames.append(SeamFrame(at.prefix + node.name, parent, params))
        index = len(self.frames) - 1
        inside = _Context(index, at.guards, index, f"{at.prefix}{node.name}.", at.bound | {p.name for p in params})
        self.stack.extend((statement, inside._replace(guards=guards)) for statement, guards in _statements(node.body, at.guards))


def _pruned(frames: Sequence[SeamFrame], uses: set[SeamUse]) -> SeamFacts:
    """Only the frames that stand around a use, renumbered."""
    wanted: set[int] = set()
    for use in uses:
        index = use.frame
        while index >= 0 and index not in wanted:
            wanted.add(index)
            index = frames[index].parent
    renumbered = {old: new for new, old in enumerate(sorted(wanted))}
    kept = tuple(
        SeamFrame(frames[old].name, renumbered.get(frames[old].parent, -1), frames[old].params, frames[old].is_class) for old in sorted(wanted)
    )
    moved = {SeamUse(renumbered[u.frame], u.line, u.category, u.target, u.guards) for u in uses}
    return SeamFacts(kept, tuple(sorted(moved, key=lambda u: (u.line, u.category, u.target, u.frame, u.guards))))


def seam_facts(tree: ast.Module, sites: Sequence[EffectSite], running: set[int], aliases: dict[str, str], dotted: Dotted) -> SeamFacts:
    """``sites`` and ``running`` are what ``facts.extract`` already computed; a module with no injectable use is not walked."""
    found: dict[int, list[tuple[str, str]]] = {}
    for site in sites:
        if site[1].category in INJECTABLE_EFFECTS and runs(site, running):
            found.setdefault(id(site[0]), []).append((site[1].category, site[1].target))
    if not found:
        return SeamFacts()
    walk = _Walk(found, aliases, dotted)
    walk.run(tree)
    return _pruned(walk.frames, walk.uses)
