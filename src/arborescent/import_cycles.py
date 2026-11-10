"""Import cycles with their edge kinds, and what to cut (#149).

A cycle hidden by a function-level import is still a cycle: the ``ImportError`` is gone and the coupling is not. This
module finds the strongly connected components of the project import graph, marks each edge as import-time,
function-level or ``TYPE_CHECKING``-only, approximates a minimum feedback arc set (the edges whose removal leaves the
component acyclic), and says which function-level imports exist only to close a cycle that import-time edges would
otherwise complete. It also names the lowest-ring module of each component as the place to own a ``Protocol`` that
inverts a cut edge. Pure computation over scanned facts: no I/O.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum

from .facts import ModuleInfo, RawImport, resolve_targets

__all__ = ["Cycle", "EdgeKind", "ImportEdge", "cyclic_components", "find_cycles", "import_edges"]


class EdgeKind(StrEnum):
    """How an edge is traversed; an edge several imports share takes the first kind listed here that any of them has."""

    EAGER = "import-time"
    LAZY = "function-level"
    TYPE_ONLY = "type-checking"


_STRENGTH = {EdgeKind.EAGER: 0, EdgeKind.LAZY: 1, EdgeKind.TYPE_ONLY: 2}


@dataclass(frozen=True)
class ImportEdge:
    """``source`` imports ``target``; ``lines`` are the import statements that make the edge."""

    source: str
    target: str
    kind: EdgeKind
    lines: tuple[int, ...]


@dataclass(frozen=True)
class Cycle:
    """One strongly connected component of the import graph."""

    modules: tuple[str, ...]
    edges: tuple[ImportEdge, ...]  # every edge inside the component
    cut: tuple[ImportEdge, ...]  # an approximate minimum set whose removal leaves the component acyclic
    cycle_breaking: tuple[ImportEdge, ...]  # function-level edges that would close an import-time cycle if they were module-level
    protocol_owner: str  # the lowest-ring module in the component: the place for a Protocol that inverts a cut edge


def _kind(imp: RawImport) -> EdgeKind:
    if imp.type_only:
        return EdgeKind.TYPE_ONLY
    return EdgeKind.LAZY if imp.lazy else EdgeKind.EAGER


def import_edges(
    modules: Mapping[str, ModuleInfo], known: Collection[str], scope_of: Callable[[str], str | None],
) -> list[ImportEdge]:
    """The internal import edges between ``modules``, within one architecture scope, with the kind of each."""
    tops = {name.split(".")[0] for name in known}
    found: dict[tuple[str, str], list[tuple[EdgeKind, int]]] = {}
    for name, info in sorted(modules.items()):
        for imp in info.facts.imports:
            internal = resolve_targets(name, info.is_package, imp, known, tops)[0]
            for target in internal:
                if target in modules and scope_of(target) == scope_of(name):
                    found.setdefault((name, target), []).append((_kind(imp), imp.line))
    return [
        ImportEdge(source, target, min((kind for kind, _ in uses), key=_STRENGTH.__getitem__), tuple(sorted({line for _, line in uses})))
        for (source, target), uses in sorted(found.items())
    ]


def find_cycles(edges: Collection[ImportEdge], ring_rank: Mapping[str, int]) -> list[Cycle]:
    """Every import cycle, largest first. ``ring_rank`` orders modules innermost ring first; a module without one sorts last."""
    graph: dict[str, set[str]] = {}
    for edge in edges:
        graph.setdefault(edge.source, set()).add(edge.target)
        graph.setdefault(edge.target, set())
    cycles: list[Cycle] = []
    for component in cyclic_components(graph):
        inside = tuple(sorted((e for e in edges if e.source in component and e.target in component), key=lambda e: (e.source, e.target)))
        cycles.append(Cycle(
            tuple(sorted(component)), inside, _feedback_edges(component, inside), _cycle_breaking(inside), _protocol_owner(component, inside, ring_rank),
        ))
    return sorted(cycles, key=lambda c: (-len(c.modules), c.modules))


def cyclic_components(graph: Mapping[str, Collection[str]]) -> list[set[str]]:
    """Strongly connected components that hold a cycle: two or more modules, or one that imports itself.

    Iterative Kosaraju traversal avoids recursion limits on large repositories. Targets outside ``graph`` are ignored.
    """
    nodes = set(graph)
    edges = {node: set(targets) & nodes for node, targets in graph.items()}
    reverse: dict[str, set[str]] = {node: set() for node in nodes}
    for source, targets in edges.items():
        for target in targets:
            reverse[target].add(source)
    components = _strong_components(_finish_order(edges), reverse)
    return [component for component in components if len(component) > 1 or next(iter(component)) in edges[next(iter(component))]]


def _finish_order(edges: Mapping[str, set[str]]) -> list[str]:
    """Return DFS finish order without relying on Python's recursion limit."""
    visited: set[str] = set()
    order: list[str] = []
    for start in sorted(edges):
        if start in visited:
            continue
        visited.add(start)
        pending: list[tuple[str, Iterator[str]]] = [(start, iter(sorted(edges[start])))]
        while pending:
            node, targets = pending[-1]
            try:
                target = next(targets)
            except StopIteration:
                pending.pop()
                order.append(node)
                continue
            if target not in visited:
                visited.add(target)
                pending.append((target, iter(sorted(edges[target]))))
    return order


def _strong_components(order: list[str], reverse: Mapping[str, set[str]]) -> list[set[str]]:
    """Collect the strongly connected components in reverse-graph finish order."""
    assigned: set[str] = set()
    components: list[set[str]] = []
    for start in reversed(order):
        if start in assigned:
            continue
        component: set[str] = set()
        pending = [start]
        assigned.add(start)
        while pending:
            node = pending.pop()
            component.add(node)
            for source in reverse[node]:
                if source not in assigned:
                    assigned.add(source)
                    pending.append(source)
        components.append(component)
    return components


def _feedback_edges(component: Collection[str], inside: tuple[ImportEdge, ...]) -> tuple[ImportEdge, ...]:
    """Edges against an Eades-Lin-Smyth ordering, then thinned until none can be put back without closing a cycle."""
    position = {module: index for index, module in enumerate(_ordering(component, inside))}
    cut: list[ImportEdge] = [edge for edge in inside if position[edge.target] <= position[edge.source]]
    for edge in tuple(cut):
        cut.remove(edge)  # put it back; keep it out of the cut when the component stays acyclic
        if not _acyclic(component, [other for other in inside if other not in cut]):
            cut.append(edge)
    return tuple(sorted(cut, key=lambda e: (e.source, e.target)))


def _ordering(component: Collection[str], inside: tuple[ImportEdge, ...]) -> list[str]:
    """Eades-Lin-Smyth: sinks go last, sources first, otherwise the module with the largest out-minus-in degree goes next."""
    out = {module: {e.target for e in inside if e.source == module and e.target != module} for module in component}
    into = {module: {e.source for e in inside if e.target == module and e.source != module} for module in component}
    remaining = set(component)
    head: list[str] = []
    tail: list[str] = []
    while remaining:
        ordered = sorted(remaining)
        sink = next((m for m in ordered if not out[m] & remaining), None)
        source = next((m for m in ordered if not into[m] & remaining), None)
        if sink is not None:
            tail.append(sink)
            remaining.discard(sink)
        elif source is not None:
            head.append(source)
            remaining.discard(source)
        else:
            best = max(ordered, key=lambda m: len(out[m] & remaining) - len(into[m] & remaining))
            head.append(best)
            remaining.discard(best)
    return head + tail[::-1]


def _acyclic(component: Collection[str], edges: Collection[ImportEdge]) -> bool:
    """Kahn's algorithm; a self-import is a cycle."""
    if any(edge.source == edge.target for edge in edges):
        return False
    incoming = dict.fromkeys(component, 0)
    for edge in edges:
        incoming[edge.target] += 1
    ready = deque(module for module, count in incoming.items() if count == 0)
    seen = 0
    while ready:
        module = ready.popleft()
        seen += 1
        for edge in edges:
            if edge.source == module:
                incoming[edge.target] -= 1
                if incoming[edge.target] == 0:
                    ready.append(edge.target)
    return seen == len(incoming)


def _cycle_breaking(inside: tuple[ImportEdge, ...]) -> tuple[ImportEdge, ...]:
    """Function-level edges ``a -> b`` where ``b`` already reaches ``a`` through import-time edges alone.

    Moving such an import to module level would close an import-time cycle, so it exists to break one. A function-level
    edge in the component without that property is on a cycle only through other deferred or type-only edges.
    """
    eager: dict[str, set[str]] = {}
    for edge in inside:
        if edge.kind is EdgeKind.EAGER:
            eager.setdefault(edge.source, set()).add(edge.target)
    return tuple(edge for edge in inside if edge.kind is EdgeKind.LAZY and _reaches(eager, edge.target, edge.source))


def _reaches(graph: Mapping[str, Collection[str]], start: str, goal: str) -> bool:
    seen = {start}
    pending = [start]
    while pending:
        module = pending.pop()
        if module == goal:
            return True
        for target in graph.get(module, ()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return False


def _protocol_owner(component: Collection[str], inside: tuple[ImportEdge, ...], ring_rank: Mapping[str, int]) -> str:
    """The innermost-ring module of the component, then the one most of the others import, then by name."""
    outermost = max(ring_rank.values(), default=0) + 1
    importers = {module: len({e.source for e in inside if e.target == module and e.source != module}) for module in component}
    return min(component, key=lambda module: (ring_rank.get(module, outermost), -importers[module], module))
