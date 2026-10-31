"""Cycle finding over a graph library. Arborescent does not implement or vendor the algorithm; it asks a tool that does.

The adapter imports nothing itself: the entrypoint hands it a ``load`` function that returns the library, or ``None`` when it
is not installed. Loading is deferred until cycles are asked for, because importing networkx takes about 115 ms and every
other command would pay it for nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import ModuleType

__all__ = ["NetworkxCycles"]


@dataclass(frozen=True)
class NetworkxCycles:
    """Strongly connected components of the package import graph, from networkx."""

    load: Callable[[], ModuleType | None]  # the entrypoint's loader for ``networkx``
    name: str = "networkx"
    requirement: str = "networkx>=3"

    def available(self) -> bool:
        return self.load() is not None

    def cycles(self, edges: Sequence[tuple[str, str]]) -> list[tuple[str, ...]]:
        networkx = self.load()
        if networkx is None:
            raise RuntimeError("networkx is not installed")
        graph = networkx.DiGraph()
        graph.add_edges_from(edges)
        groups = (tuple(sorted(group)) for group in networkx.strongly_connected_components(graph))
        return sorted(group for group in groups if len(group) > 1)  # a lone node reaches only itself: not a cycle
