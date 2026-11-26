"""Fan-in, entrypoint closure, startup blame and ambient-hub metrics over the project import graph (#148, #216).

Import edges use the same resolution as the cycle report, so ``from pkg import sub`` is one edge to ``pkg.sub`` as in every ring
check. Each edge has one kind: ``runtime`` (module level), ``lazy`` (only inside a function body) or ``type-only`` (only
under ``if TYPE_CHECKING``). A pair imported in several ways takes the strongest kind. Fan-in counts these written imports.

Startup numbers (closure, blame, lazy mounts, cost of a new module) follow ``ImportEdges.loading``: the ``runtime`` edges
plus each module's parent packages, because importing ``a.b.c`` runs ``a`` and ``a.b`` first. The modules only a closure's
function-level or type-only imports name are counted separately.

``parse_importtime``, ``parse_loaded_modules`` and ``package_costs`` read the output of the opt-in measured run (#216).
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from .facts import ModuleInfo
from .import_cycles import EdgeKind, resolved_internal_imports

Graph = Mapping[str, frozenset[str]]

_MOSTLY = 2  # "mostly": more than 1 in 2 public definitions use an effect directly (a parameter default is a seam, not a use)

# Written to stderr just before the measured import, so the interpreter's own startup imports are not counted.
IMPORTTIME_MARKER = "arborescent-importtime-start"
# Starts the line listing ``sys.modules`` after the measured import: ``-X importtime`` has no line for ``importlib.import_module``.
LOADED_MARKER = "arborescent-importtime-modules"
_IMPORTTIME_LINE = re.compile(r"import time:\s*\d+ \|\s*(\d+) \| ( *)(\S+)\s*")


@dataclass(frozen=True)
class ImportEdges:
    runtime: Graph
    lazy: Graph  # edges that exist only as function-level imports
    type_only: Graph  # edges that exist only under TYPE_CHECKING
    parents: Graph  # implicit: each module's parent packages, which no import statement can be cut to avoid
    loading: Graph  # ``runtime`` and ``parents``: what importing a module runs


@dataclass(frozen=True)
class Closure:
    entrypoint: str
    modules: frozenset[str]  # every project module that importing the entrypoint loads: itself and parent packages included
    lazy_only: frozenset[str]  # modules the closure imports only inside functions, not loaded with it
    type_only: frozenset[str]  # modules the closure imports only for type checking


@dataclass(frozen=True)
class FanIn:
    module: str
    direct: int  # distinct importing modules, any kind of import
    runtime: int  # distinct importing modules by module-level import
    transitive: int  # modules that reach this one through module-level imports


@dataclass(frozen=True)
class Hub:
    """A module many others import whose public surface is mostly state or I/O: pass it in through a port instead."""

    module: str
    direct: int
    accessors: tuple[str, ...]
    public: int


@dataclass(frozen=True)
class NewModule:
    """``count`` modules added behind ``importer`` (a port, a service and an adapter is 3); they import ``imports``."""

    importer: str
    imports: tuple[str, ...] = ()
    count: int = 3


def import_edges(modules: Mapping[str, ModuleInfo], known: Collection[str]) -> ImportEdges:
    found: dict[EdgeKind, dict[str, set[str]]] = {kind: {} for kind in EdgeKind}
    for name, kind, _, targets in resolved_internal_imports(modules, known):
        found[kind].setdefault(name, set()).update(target for target in targets if target and target != name)
    runtime = {name: frozenset(found[EdgeKind.EAGER].get(name, ())) for name in modules}
    lazy = _without(found[EdgeKind.LAZY], runtime)
    parents = with_parent_packages({}, set(known) | set(modules))
    type_only = _without(found[EdgeKind.TYPE_ONLY], runtime, lazy)
    return ImportEdges(runtime, lazy, type_only, parents, _merge(runtime, parents))


def _without(edges: Mapping[str, set[str]], *stronger: Graph) -> Graph:
    return {
        name: frozenset(targets.difference(*(layer.get(name, frozenset()) for layer in stronger)))
        for name, targets in edges.items()
    }


def reach(graph: Graph, starts: Iterable[str]) -> frozenset[str]:
    """Every node reachable from ``starts`` (themselves included) along ``graph``."""
    seen = set(starts)
    pending = deque(seen)
    while pending:
        for target in graph.get(pending.popleft(), frozenset()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return frozenset(seen)


def _reverse(graph: Graph) -> Graph:
    reverse: dict[str, set[str]] = {}
    for source, targets in graph.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    return {name: frozenset(sources) for name, sources in reverse.items()}


def fan_in(edges: ImportEdges, modules: Iterable[str]) -> list[FanIn]:
    """Per module, most imported first. Only importers that are project modules count; a module is never its own importer."""
    runtime_importers = _reverse(edges.runtime)
    importers_all = _reverse(_merge(edges.runtime, edges.lazy, edges.type_only))
    return sorted(
        (
            FanIn(
                name,
                len(importers_all.get(name, ())),
                len(runtime_importers.get(name, ())),
                len(reach(runtime_importers, [name])) - 1,
            )
            for name in modules
        ),
        key=lambda item: (-item.direct, item.module),
    )


def _merge(*graphs: Graph) -> Graph:
    merged: dict[str, frozenset[str]] = {}
    for graph in graphs:
        for name, targets in graph.items():
            merged[name] = merged.get(name, frozenset()) | targets
    return merged


def entrypoint_closure(edges: ImportEdges, entrypoint: str) -> Closure:
    loaded = reach(edges.loading, [entrypoint])
    return Closure(
        entrypoint,
        loaded,
        _beyond(edges.lazy, loaded),
        _beyond(edges.type_only, loaded) - _beyond(edges.lazy, loaded),
    )


@dataclass(frozen=True)
class Blame:
    """``modules`` are loaded only because of the imports of ``target``: cutting every ``importers`` -> ``target`` one removes them."""

    target: str
    importers: tuple[str, ...]  # one module: that single edge is responsible
    modules: int


def blame(graph: Graph, entrypoint: str, parents: Graph | None = None) -> list[Blame]:
    """What cutting the imports of each module of ``entrypoint``'s closure removes, heaviest first, with the importers.

    ``graph`` holds the written imports, which can be cut. ``parents`` (module -> its parent packages) is loaded with each
    module and cannot be: a package that stays loaded for a submodule removes nothing, so it is not listed."""
    implicit: Graph = dict[str, frozenset[str]]() if parents is None else parents
    loaded = reach(_merge(graph, implicit), [entrypoint])
    importers = _reverse({name: graph.get(name, frozenset()) for name in loaded})
    found: list[Blame] = []
    for target in importers.keys() - {entrypoint}:
        without = _reach_without(graph, implicit, entrypoint, target)
        if target not in without:
            direct = tuple(sorted(name for name in importers[target] if name in without))
            found.append(Blame(target, direct, len(loaded) - len(without)))
    return sorted(found, key=lambda item: (-item.modules, item.target))


def _reach_without(graph: Graph, parents: Graph, start: str, avoid: str) -> frozenset[str]:
    """``reach`` from ``start`` with every written import of ``avoid`` cut."""
    seen: set[str] = {start}
    pending: list[str] = [start]
    while pending:
        name = pending.pop()
        for target in (graph.get(name, frozenset()) - {avoid}) | parents.get(name, frozenset()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return frozenset(seen)


def import_chain(graph: Graph, start: str, target: str) -> tuple[str, ...] | None:
    """A shortest import path from ``start`` to ``target``, or None when it is not reached."""
    came_from = {start: start}
    pending = deque([start])
    while pending:
        name = pending.popleft()
        for following in sorted(graph.get(name, frozenset())):
            if following not in came_from:
                came_from[following] = name
                pending.append(following)
    if target not in came_from:
        return None
    path = [target]
    while path[-1] != start:
        path.append(came_from[path[-1]])
    return tuple(reversed(path))


def with_parent_packages(graph: Graph, known: Collection[str]) -> Graph:
    """``graph`` where each module also loads its parent packages, as importing it does."""
    names = set(known) | set(graph)
    parents = {
        name: frozenset(parent for end in range(1, name.count(".") + 1) if (parent := ".".join(name.split(".")[:end])) in names)
        for name in names
    }
    return _merge(graph, parents)


def mount_cost(graph: Graph, entrypoint: str, module: str) -> int:
    """Modules that running a lazily mounted command adds to what ``entrypoint`` already loaded.

    ``graph`` is ``ImportEdges.loading``, so the command's parent packages count."""
    return len(reach(graph, [module]) - reach(graph, [entrypoint]))


def _beyond(graph: Graph, loaded: frozenset[str]) -> frozenset[str]:
    return frozenset(target for name in loaded for target in graph.get(name, ())) - loaded


def accessor_surface(info: ModuleInfo) -> tuple[tuple[str, ...], int]:
    """(public definitions that use an effect directly, public definition count).

    A definition owns the lines from its first line to the next definition's; a use before the first definition is
    module-level state and belongs to none, so it never makes a surface look like accessors.
    """
    defs = sorted(info.facts.top_level_defs, key=lambda item: item.line)
    uses = [use.line for use in info.facts.effect_uses if use.seam == 0 and not use.is_module_state]
    accessors: list[str] = []
    public = 0
    for index, item in enumerate(defs):
        if item.name.startswith("_"):
            continue
        public += 1
        end = defs[index + 1].line if index + 1 < len(defs) else float("inf")
        if any(item.line <= line < end for line in uses):
            accessors.append(item.name)
    return tuple(accessors), public


def ambient_hubs(rows: Iterable[FanIn], modules: Mapping[str, ModuleInfo], minimum: int) -> list[Hub]:
    hubs: list[Hub] = []
    for row in rows:
        info = modules.get(row.module)
        if info is None or row.direct < minimum:
            continue
        accessors, public = accessor_surface(info)
        if public and len(accessors) * _MOSTLY > public:
            hubs.append(Hub(row.module, row.direct, accessors, public))
    return hubs


def cost_of_new_modules(closures: Iterable[Closure], edges: ImportEdges, new: NewModule) -> dict[str, int]:
    """Per entrypoint whose closure holds ``new.importer``: modules the closure gains (the new ones plus any closure they pull in).

    The new modules' own packages are taken to be loaded already: where they will live is not known."""
    return {
        closure.entrypoint: new.count + len(reach(edges.loading, new.imports) - closure.modules)
        for closure in closures
        if new.importer in closure.modules
    }


@dataclass(frozen=True)
class ImportRecord:
    """One module ``-X importtime`` says was imported."""

    name: str
    cumulative_us: int  # its own time plus that of everything it was first to import
    ancestors: tuple[str, ...]  # the modules that were being imported when it was, outermost first


def parse_importtime(text: str) -> list[ImportRecord]:
    """The records after ``IMPORTTIME_MARKER``, in output order; other lines (headers, warnings, cut-off lines) are skipped.

    A module is printed after what it imports, indented two spaces per level, so the tree is rebuilt from the end."""
    _, _, measured = text.partition(IMPORTTIME_MARKER)
    found: list[ImportRecord] = []
    stack: list[tuple[int, str]] = []
    for line in reversed(measured.splitlines()):
        match = _IMPORTTIME_LINE.fullmatch(line)
        if match is None:
            continue
        cumulative, indent, name = match.groups()
        while stack and stack[-1][0] >= len(indent):
            _ = stack.pop()
        found.append(ImportRecord(name, int(cumulative), tuple(outer for _, outer in stack)))
        stack.append((len(indent), name))
    return found[::-1]


def parse_loaded_modules(text: str) -> frozenset[str] | None:
    """The module names of the last ``LOADED_MARKER`` line after ``IMPORTTIME_MARKER``, or None when there is none."""
    _, _, measured = text.partition(IMPORTTIME_MARKER)
    for line in reversed(measured.splitlines()):
        if line.startswith(LOADED_MARKER):
            return frozenset(line.split()[1:])
    return None


def package_costs(records: Iterable[ImportRecord]) -> dict[str, int]:
    """Top-level package -> microseconds spent importing it: its outermost records, so no module is counted twice."""
    costs: dict[str, int] = {}
    for record in records:
        top = record.name.partition(".")[0]
        if not any(outer.partition(".")[0] == top for outer in record.ancestors):
            costs[top] = costs.get(top, 0) + record.cumulative_us
    return costs
