"""Per-definition facts for ``mixed-purity`` (#173), gathered in the one AST walk ``facts.extract`` already does.

``EffectUse`` records the line of every effectful use but not which function it sits in. This module records the
top-level definitions' line spans and what a definition does that ``EffectUse`` does not model: logging, memo caches
and mutation of module-level containers. They are deliberately *not* ``EffectUse`` categories: adding categories there
would change the ``effect-*`` rules, the classify audit and the coupling accessor counts for every user.

What is and is not seen, by design:

- Only module-level functions and classes are units. A class is one unit: an effect in any of its methods makes it effectful.
- A logger is a module-level name bound to ``logging.getLogger(...)`` (or ``structlog.get_logger``), or ``loguru.logger``.
- Module state is a module-level name bound to a ``dict``/``list``/``set`` literal or constructor, written from inside a
  function by item assignment, ``del`` or a mutating method call, unless the function binds that name locally. A name
  containing ``cache`` or ``memo`` is a memo, any other is plain state. ``functools.lru_cache``/``cache`` on a function is
  *not* a memo kind: a memoised pure function is still pure (#195); only a written module-level cache is.
- A method call on a parameter annotated with an imported or module-level class (``reader: Reader`` then ``reader.read()``)
  is recorded as ``port_calls`` so ``mixed_purity.ambient`` patterns can match ``pkg.ports.Reader.read``; a parameter the
  function reassigns is not recorded.
- A call by bare name to another unit of the same module is recorded so the rule can follow it: a function that calls an
  effectful sibling is not pure.
"""

from __future__ import annotations

import ast
import re
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

__all__ = ["LOGGING", "MEMO", "STATE", "Helpers", "PurityFacts", "PurityUnit", "local_names", "module_containers", "mutated_container", "purity_facts"]

LOGGING = "logging"
MEMO = "memo"
STATE = "state"

_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"})
_LOGGING_FUNCTIONS = frozenset(f"logging.{name}" for name in (*_LOG_METHODS, "basicConfig", "disable", "captureWarnings"))
_LOGGER_FACTORIES = frozenset({"logging.getLogger", "structlog.get_logger", "structlog.getLogger"})
_LOGGER_OBJECTS = frozenset({"loguru.logger"})
_CONTAINER_CALLS = frozenset({"dict", "list", "set", "defaultdict", "OrderedDict", "Counter", "deque"})
_CONTAINER_LITERALS = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
_MUTATORS = frozenset({
    "append", "extend", "insert", "add", "update", "pop", "popitem", "remove", "discard", "clear", "setdefault", "appendleft", "popleft",
})
_MEMO_NAME = re.compile(r"cache|memo", re.IGNORECASE)

Dotted = Callable[[ast.expr, dict[str, str]], str | None]


@dataclass(frozen=True)
class PurityUnit:
    """One module-level function or class, with the span an effect line is attributed by."""

    name: str
    line: int  # first line, decorators included
    end_line: int
    body_lines: int  # source lines from the first statement after the docstring to the last; 0 for a docstring-only body
    is_class: bool
    logic: bool  # a function that is not a stub, or a non-abstract class with at least one method that is not a stub
    kinds: tuple[tuple[int, str], ...] = ()  # (line, logging | memo | state) found inside the span
    calls: tuple[str, ...] = ()  # sorted names of other units of the module that this one calls by bare name


@dataclass(frozen=True)
class PurityFacts:
    units: tuple[PurityUnit, ...] = ()
    import_time: tuple[tuple[int, str], ...] = ()  # (line, logging) outside every unit: logging at import time
    port_calls: tuple[tuple[int, str, str, bool], ...] = ()  # (line, class, method, class is defined in this module) of `param.method()` on a typed parameter


def _module_level_defs(body: Sequence[ast.stmt]) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    """Functions and classes defined at module level, including inside ``if``/``try``/``with`` blocks (an
    ``except ImportError`` fallback is still a module-level function), but never inside another function or class."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
    for stmt in body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.append(stmt)
        elif isinstance(stmt, ast.If | ast.With | ast.AsyncWith):
            found += _module_level_defs([*stmt.body, *getattr(stmt, "orelse", [])])
        elif isinstance(stmt, ast.Try | ast.TryStar):
            found += _module_level_defs([*stmt.body, *stmt.orelse, *stmt.finalbody, *(s for h in stmt.handlers for s in h.body)])
    return found


def _body_lines(body: list[ast.stmt]) -> int:
    first = 1 if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str) else 0
    rest = body[first:]
    if not rest:
        return 0
    return (rest[-1].end_lineno or rest[-1].lineno) - rest[0].lineno + 1


def _has_logic(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, is_stub: Callable[[list[ast.stmt]], bool], skipped: bool) -> bool:
    """``skipped``: the function is a stub, or the class is abstract (a Protocol or ABC): neither is logic."""
    if isinstance(node, ast.ClassDef):
        return not skipped and any(
            isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and not is_stub(member.body) for member in node.body
        )
    return not skipped


def _module_names(tree: ast.Module, aliases: dict[str, str], dotted: Dotted) -> tuple[set[str], set[str]]:
    """Module-level names bound to a logger, and to a mutable container."""
    loggers: set[str] = set()
    containers: set[str] = set()
    for stmt in tree.body:
        targets: Sequence[ast.expr] = ()
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = (stmt.target,), stmt.value
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names or value is None:
            continue
        if isinstance(value, ast.Call) and dotted(value.func, aliases) in _LOGGER_FACTORIES:
            loggers.update(names)
        elif isinstance(value, _CONTAINER_LITERALS) or (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Name | ast.Attribute)
            and (value.func.id if isinstance(value.func, ast.Name) else value.func.attr) in _CONTAINER_CALLS
        ):
            containers.update(names)
    return loggers, containers


def _local_names(unit: ast.AST) -> set[str]:
    """Names a unit binds locally (parameters and plain assignments), minus those it declares ``global``."""
    bound: set[str] = set()
    declared: set[str] = set()
    for node in ast.walk(unit):
        if isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.Global):
            declared.update(node.names)
    return bound - declared


def _class_name(node: ast.Name | ast.Attribute, aliases: dict[str, str], local: set[str], dotted: Dotted) -> list[tuple[str, bool]]:
    """The class a plain name or ``m.X`` stands for when it is imported or defined in this module; nothing for ``str`` and the like."""
    head: ast.expr = node
    while isinstance(head, ast.Attribute):
        head = head.value
    name = dotted(node, aliases)
    if not isinstance(head, ast.Name) or name is None:
        return []
    if head.id in aliases:
        return [(name, False)]
    return [(name, True)] if head.id in local and isinstance(node, ast.Name) else []


def _annotation_types(node: ast.expr | None, aliases: dict[str, str], local: set[str], dotted: Dotted) -> list[tuple[str, bool]]:
    """(dotted class name, defined in this module) for the classes an annotation names: ``X``, ``m.X``, ``"X"``, ``X | None``, ``Optional[X]``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _annotation_types(ast.parse(node.value.strip(), mode="eval").body, aliases, local, dotted)
        except SyntaxError:
            return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_types(node.left, aliases, local, dotted) + _annotation_types(node.right, aliases, local, dotted)
    if isinstance(node, ast.Subscript):
        base = dotted(node.value, aliases) or ""
        return _annotation_types(node.slice, aliases, local, dotted) if base.rsplit(".", 1)[-1] == "Optional" else []
    return _class_name(node, aliases, local, dotted) if isinstance(node, ast.Name | ast.Attribute) else []


def _port_calls(func: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str], local: set[str], dotted: Dotted) -> list[tuple[int, str, str, bool]]:
    """(line, class, method, local) for ``param.method(...)`` where the parameter is annotated with an imported or module-level class."""
    typed: dict[str, list[tuple[str, bool]]] = {}
    for arg in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs):
        if arg.annotation is not None and (types := _annotation_types(arg.annotation, aliases, local, dotted)):
            typed[arg.arg] = types
    if not typed:
        return []
    inner = list(ast.walk(func))
    rebound = {n.id for n in inner if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    return [
        (node.lineno, name, node.func.attr, is_local)
        for node in inner
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
        and node.func.value.id in typed and node.func.value.id not in rebound
        for name, is_local in typed[node.func.value.id]
    ]


def _is_logging_call(node: ast.Call, loggers: set[str], aliases: dict[str, str], dotted: Dotted) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return aliases.get(func.id) in _LOGGING_FUNCTIONS
    if not isinstance(func, ast.Attribute):
        return False
    if dotted(func, aliases) in _LOGGING_FUNCTIONS:
        return True
    if func.attr not in _LOG_METHODS:
        return False
    return (isinstance(func.value, ast.Name) and func.value.id in loggers) or dotted(func.value, aliases) in _LOGGER_OBJECTS


def _mutated_container(node: ast.AST, containers: set[str]) -> str | None:
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store | ast.Del) and isinstance(node.value, ast.Name):
        return node.value.id if node.value.id in containers else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATORS and isinstance(node.func.value, ast.Name):
        return node.func.value.id if node.func.value.id in containers else None
    return None


class Helpers(NamedTuple):
    """The three syntactic predicates ``facts`` already owns (this module cannot import it: ``facts`` imports this one)."""

    dotted: Dotted
    is_stub: Callable[[list[ast.stmt]], bool]
    is_abstract: Callable[[ast.ClassDef], bool]


class _Collector:
    """Attributes each logging call, container write, local call and memo decorator to the top-level definition around it."""

    def __init__(self, tree: ast.Module, aliases: dict[str, str], helpers: Helpers) -> None:
        self.aliases = aliases
        self.helpers = helpers
        self.defs = _module_level_defs(tree.body)
        self.loggers, self.containers = _module_names(tree, aliases, helpers.dotted)
        self.first_line = [min([s.lineno, *(d.lineno for d in s.decorator_list)]) for s in self.defs]
        self.spans = sorted((self.first_line[index], s.end_lineno or s.lineno, index) for index, s in enumerate(self.defs))
        self.starts = [span[0] for span in self.spans]
        self.unit_names = {d.name for d in self.defs}
        self.kinds: dict[int, set[tuple[int, str]]] = {i: set() for i in range(len(self.defs))}
        self.calls: dict[int, set[str]] = {i: set() for i in range(len(self.defs))}
        self.candidates: list[tuple[int, int, str]] = []  # (unit index, line, container name)
        self.import_time: set[tuple[int, str]] = set()
        self.port_calls: set[tuple[int, str, str, bool]] = set()

    def owner(self, line: int) -> int | None:
        slot = bisect_right(self.starts, line) - 1
        return self.spans[slot][2] if slot >= 0 and line <= self.spans[slot][1] else None

    def visit(self, node: ast.AST) -> None:
        line = getattr(node, "lineno", 0)
        if not line or not isinstance(node, ast.Call | ast.Subscript):
            return
        owner = self.owner(line)
        if isinstance(node, ast.Call) and _is_logging_call(node, self.loggers, self.aliases, self.helpers.dotted):
            (self.import_time if owner is None else self.kinds[owner]).add((line, LOGGING))
        if owner is None:
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.unit_names:
            self.calls[owner].add(node.func.id)
        if self.containers and (name := _mutated_container(node, self.containers)) is not None:
            self.candidates.append((owner, line, name))

    def resolve_writes(self) -> None:
        """Keep a container write unless its function binds the name locally (then it is not the module's container)."""
        shadows: dict[int, set[str]] = {}
        for owner, line, name in self.candidates:
            if owner not in shadows:
                shadows[owner] = _local_names(self.defs[owner])
            if name not in shadows[owner]:
                self.kinds[owner].add((line, MEMO if _MEMO_NAME.search(name) else STATE))

    def collect_port_calls(self, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> None:
        """Calls on typed parameters, for every function or method with an annotated parameter; ``aliases`` keep relative imports' dots."""
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(a.annotation for a in (*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs)):
                self.port_calls.update(_port_calls(node, aliases, self.unit_names, self.helpers.dotted))

    def unit(self, index: int) -> PurityUnit:
        node = self.defs[index]
        helpers = self.helpers
        skipped = helpers.is_abstract(node) if isinstance(node, ast.ClassDef) else helpers.is_stub(node.body)
        return PurityUnit(
            node.name, self.first_line[index], node.end_lineno or node.lineno, _body_lines(node.body), isinstance(node, ast.ClassDef),
            _has_logic(node, helpers.is_stub, skipped), tuple(sorted(self.kinds[index])), tuple(sorted(self.calls[index] - {node.name})),
        )


def purity_facts(tree: ast.Module, nodes: Sequence[ast.AST], aliases: dict[str, str], helpers: Helpers, call_aliases: dict[str, str] | None = None) -> PurityFacts:
    """``nodes`` is the walk of ``tree`` that ``extract`` already made; nothing here walks the module again except to
    check one definition for a shadowing local, and only when it mutates a module-level container."""
    collector = _Collector(tree, aliases, helpers)
    for node in nodes:
        collector.visit(node)
    collector.resolve_writes()
    collector.collect_port_calls(nodes, call_aliases if call_aliases is not None else aliases)
    return PurityFacts(
        tuple(collector.unit(i) for i in range(len(collector.defs))), tuple(sorted(collector.import_time)), tuple(sorted(collector.port_calls)),
    )


def module_containers(tree: ast.Module, aliases: dict[str, str], dotted: Dotted) -> set[str]:
    """Module-level names bound to a ``dict``/``list``/``set`` (#194 reads them as ``registry`` effect uses)."""
    return _module_names(tree, aliases, dotted)[1]


def local_names(unit: ast.AST) -> set[str]:
    """Names a function binds locally (parameters and assignments), minus those it declares ``global``."""
    return _local_names(unit)


def mutated_container(node: ast.AST, containers: set[str]) -> str | None:
    """The module container ``node`` writes by item assignment, ``del`` or a mutating method call, else ``None``."""
    return _mutated_container(node, containers)
