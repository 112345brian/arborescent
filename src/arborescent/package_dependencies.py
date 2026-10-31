"""The use case behind ``arborescent packages report``: the imports between declared packages, and who imports whom back.

Grouping the scan's imports by package is plain bookkeeping (``package_report``). Finding the packages that import each
other is a graph algorithm, so it is asked of a ``CycleFinder`` (an adapter over a graph library) and is optional: with
none available the report says so instead of guessing.
"""

from __future__ import annotations

from .package_assignment import RepositoryFacts
from .package_model import CatalogDocument
from .package_report import DependencyReport, package_edges
from .ports import CycleFinder

__all__ = ["dependency_report"]


def dependency_report(catalog: CatalogDocument, facts: RepositoryFacts, finder: CycleFinder | None) -> DependencyReport:
    edges, unused = package_edges(catalog, facts)
    cycles = None
    if finder is not None and finder.available():
        cycles = tuple(finder.cycles([(edge.source, edge.target) for edge in edges]))
    return DependencyReport(edges, unused, cycles)
