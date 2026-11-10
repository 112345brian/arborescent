"""Fan-in, entrypoint closure and ambient-hub metrics over the project import graph (#148).

Import edges come from ``facts.resolve_targets``, so ``from pkg import sub`` is one edge to ``pkg.sub`` as in every ring
check. Each edge has one kind: ``runtime`` (module level), ``lazy`` (only inside a function body) or ``type-only`` (only
under ``if TYPE_CHECKING``). A pair imported in several ways takes the strongest kind. A closure follows ``runtime`` edges
alone, because that is what loading the entrypoint runs; the modules only its function-level or type-only imports reach
are counted separately.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from .facts import ModuleInfo, RawImport, resolve_targets

Graph = Mapping[str, frozenset[str]]

_MOSTLY = 2  # "mostly": more than 1 in 2 public definitions use an effect directly (a parameter default is a seam, not a use)


@dataclass(frozen=True)
class ImportEdges:
    runtime: Graph
    lazy: Graph  # edges that exist only as function-level imports
    type_only: Graph  # edges that exist only under TYPE_CHECKING


@dataclass(frozen=True)
class Closure:
    entrypoint: str
    modules: frozenset[str]  # every project module loaded by module-level imports, the entrypoint included
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


def _kind(imp: RawImport) -> str:
    if imp.type_only:
        return "type-only"
    return "lazy" if imp.lazy else "runtime"


def import_edges(modules: Mapping[str, ModuleInfo], known: Collection[str]) -> ImportEdges:
    tops = {name.split(".")[0] for name in known}
    found: dict[str, dict[str, set[str]]] = {"runtime": {}, "lazy": {}, "type-only": {}}
    for name, info in modules.items():
        for imp in info.facts.imports:
            internal, _, _ = resolve_targets(name, info.is_package, imp, known, tops)
            targets = {target for target in internal if target and target != name}
            found[_kind(imp)].setdefault(name, set()).update(targets)
    runtime = {name: frozenset(found["runtime"].get(name, ())) for name in modules}
    lazy = _without(found["lazy"], runtime)
    return ImportEdges(runtime, lazy, _without(found["type-only"], runtime, lazy))


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
    loaded = reach(edges.runtime, [entrypoint])
    return Closure(
        entrypoint,
        loaded,
        _beyond(edges.lazy, loaded),
        _beyond(edges.type_only, loaded) - _beyond(edges.lazy, loaded),
    )


def _beyond(graph: Graph, loaded: frozenset[str]) -> frozenset[str]:
    return frozenset(target for name in loaded for target in graph.get(name, ())) - loaded


def accessor_surface(info: ModuleInfo) -> tuple[tuple[str, ...], int]:
    """(public definitions that use an effect directly, public definition count).

    A definition owns the lines from its first line to the next definition's; a use before the first definition is
    module-level state and belongs to none, so it never makes a surface look like accessors.
    """
    defs = sorted(info.facts.top_level_defs, key=lambda item: item.line)
    uses = [use.line for use in info.facts.effect_uses if use.seam == 0]
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
    """Per entrypoint whose closure holds ``new.importer``: modules the closure gains (the new ones plus any closure they pull in)."""
    return {
        closure.entrypoint: new.count + len(reach(edges.runtime, new.imports) - closure.modules)
        for closure in closures
        if new.importer in closure.modules
    }
