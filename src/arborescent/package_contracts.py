"""The package assignment as the data the engine configs are compiled from (#53). Pure.

Nothing here walks an import graph: a layout answers one question about one edge (``allows``), and the contracts below
are lists of modules the engines (import-linter, tach) are told to refuse. Their own violation output is the cut list.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .domain import ALL
from .package_assignment import owners_of
from .package_model import CatalogDocument
from .package_preservation import is_public

__all__ = ["Allowance", "PackageLayout", "boundary_contracts", "external_contracts", "layout_of"]

# (names that are not third-party-free for the module, names its ring lists); ``None`` for a module with no ring.
Allowance = tuple[frozenset[str], frozenset[str]]
Contract = tuple[str, list[str], list[str]]  # (name, source modules, forbidden modules)


@dataclass(frozen=True, slots=True)
class PackageLayout:
    """Who owns each module, what each package declares, and which of its modules are its interface."""

    owner: Mapping[str, str]  # module -> its only package; a module claimed by none or by several is absent
    depends_on: Mapping[str, frozenset[str]]
    public: Mapping[str, tuple[str, ...]]  # empty: the package declares no surface, so nothing of it is private

    def is_public(self, module: str) -> bool:
        patterns = self.public.get(self.owner.get(module, ""), ())
        return not patterns or is_public(module, patterns)

    def allows(self, importer: str, imported: str) -> bool:
        """A module may import its own package, a module nobody owns (a gap reported elsewhere), or a public module of a
        package its package declares."""
        mine, theirs = self.owner.get(importer), self.owner.get(imported)
        if mine is None or theirs is None or mine == theirs:
            return True
        return theirs in self.depends_on.get(mine, frozenset()) and self.is_public(imported)

    def members(self, modules: Sequence[str]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for module in modules:
            owner = self.owner.get(module)
            if owner is not None:
                found.setdefault(owner, []).append(module)
        return found


def layout_of(catalog: CatalogDocument, modules: Mapping[str, str]) -> PackageLayout:
    """``modules`` maps a module to its repository-relative path."""
    owner: dict[str, str] = {}
    for module, path in sorted(modules.items()):
        owners = owners_of(catalog, path)
        if len(owners) == 1:
            owner[module] = owners[0]
    return PackageLayout(
        owner,
        {package.id: frozenset(package.depends_on) for package in catalog.packages},
        {package.id: package.public for package in catalog.packages},
    )


def boundary_contracts(layout: PackageLayout, module_leaves: Sequence[str]) -> list[Contract]:
    """Per package: every module of another package it may not import. Direct imports only, like the cut list."""
    out: list[Contract] = []
    for package, mine in sorted(layout.members(module_leaves).items()):
        forbidden = [other for other in module_leaves if other in layout.owner and not layout.allows(mine[0], other)]
        if forbidden:
            out.append((f"package {package} imports only declared packages' public modules", mine, forbidden))
    return out


def external_contracts(
    layout: PackageLayout, module_leaves: Sequence[str], allowance: Callable[[str], Allowance | None],
) -> list[Contract]:
    """Per package: the I/O and third-party names none of its modules' rings list.

    Implied by the ring contracts for any classified module; it names the package, and also covers a module with no ring.
    """
    out: list[Contract] = []
    for package, mine in sorted(layout.members(module_leaves).items()):
        universe: set[str] = set()
        allowed: set[str] = set()
        for module in mine:
            known = allowance(module)
            if known is not None:
                universe |= known[0]
                allowed |= known[1]
        if ALL in allowed:
            continue
        forbidden = sorted(universe - allowed)
        if forbidden:
            out.append((f"package {package} imports no I/O or third-party package its rings do not list", mine, forbidden))
    return out
