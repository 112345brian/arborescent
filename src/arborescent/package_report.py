"""What crosses the boundaries between declared packages: the imports, with their evidence.

A package is an extraction unit, and single responsibility implies it can leave the repository, so its imports of other
packages are exactly its dependencies. This module only groups what the scan already knows (which module imports which)
by the package that owns each side. Nothing is discovered or planned: the groups are the ones the catalog declares.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from .package_assignment import RepositoryFacts, owners_of
from .package_model import CatalogDocument

__all__ = ["DependencyReport", "ImportUse", "PackageEdge", "observed_imports", "package_edges", "sole_owners"]


@dataclass(frozen=True, slots=True)
class ImportUse:
    module: str  # the importing module (in the source package)
    imports: str  # the module it imports (in the target package)


@dataclass(frozen=True, slots=True)
class PackageEdge:
    source: str
    target: str
    declared: bool  # the source lists the target in ``depends_on``
    uses: tuple[ImportUse, ...]


@dataclass(frozen=True, slots=True)
class DependencyReport:
    edges: tuple[PackageEdge, ...]
    unused: tuple[tuple[str, str], ...]  # declared in ``depends_on`` but never imported
    cycles: tuple[tuple[str, ...], ...] | None  # packages that import each other; None: no graph library to ask

    @property
    def cut_list(self) -> tuple[PackageEdge, ...]:
        """The observed imports no ``depends_on`` allows: what has to go before the packages can be split."""
        return tuple(edge for edge in self.edges if not edge.declared)

    @property
    def clean(self) -> bool:
        return not self.cut_list


def sole_owners(catalog: CatalogDocument, facts: RepositoryFacts) -> dict[str, str]:
    """Module -> its package, for the modules exactly one package owns (unowned and doubly owned are gaps, not edges)."""
    return {
        module: owners[0]
        for module, path in facts.modules.items()
        if len(owners := owners_of(catalog, path)) == 1
    }


def observed_imports(facts: RepositoryFacts, sole: Mapping[str, str]) -> dict[tuple[str, str], list[ImportUse]]:
    """(importing package, imported package) -> the module pairs behind it, for imports that cross a package boundary."""
    found: dict[tuple[str, str], list[ImportUse]] = defaultdict(list)
    for module, owner in sole.items():
        for target in facts.imports.get(module, ()):
            other = sole.get(target)
            if other is not None and other != owner:
                found[owner, other].append(ImportUse(module, target))
    return found


def package_edges(catalog: CatalogDocument, facts: RepositoryFacts) -> tuple[tuple[PackageEdge, ...], tuple[tuple[str, str], ...]]:
    """Every import edge between declared packages, and the declared dependencies that are never imported."""
    observed = observed_imports(facts, sole_owners(catalog, facts))
    declared = {(package.id, dep) for package in catalog.packages for dep in package.depends_on}
    edges = tuple(
        PackageEdge(source, target, (source, target) in declared, tuple(sorted(uses, key=lambda u: (u.module, u.imports))))
        for (source, target), uses in sorted(observed.items())
    )
    return edges, tuple(sorted(declared - set(observed)))
