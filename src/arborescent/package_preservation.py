"""Annotation preservation across a split (#55): do the parts still carry what the catalog says? Pure; no graph analysis.

The origin is the catalog (#52) with the repository facts it is checked against; a part is an extraction (#27) with the
facts of its own scan. What must survive is annotation, not behaviour: the package's semantic metadata, which modules
belong to it, each module's effective ring (``classify`` and ``__arbor__`` alike), and the public surface. Coordinates
(current paths, destinations) are free to change. Import graphs are never compared and no test is run: that is for
grimp, import-linter, tach and each part's own suite.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields

from .domain import Rule, Violation
from .package_assignment import RepositoryFacts, owners_of
from .package_model import (
    CatalogDocument,
    ExtractedPath,
    PackageDescriptor,
    PackageSemantics,
    semantics_of,
)

__all__ = ["PartFacts", "is_public", "preservation_violations", "public_patterns", "sole_owners"]


@dataclass(frozen=True, slots=True)
class PartFacts:
    """One extracted part: its manifest (#27) and the facts of scanning it under its own shipped policy."""

    location: str  # the configured path, repository-relative and posix
    included: tuple[str, ...]  # package ids the manifest says the part contains
    semantics: tuple[PackageSemantics, ...]
    paths: tuple[ExtractedPath, ...]
    module_paths: Mapping[str, str]  # module -> posix path relative to the part root
    rings: Mapping[str, str | None]  # module -> effective ring under the part's own policy
    ring_names: tuple[str, ...]  # the part policy's rings
    symbols: Mapping[str, frozenset[str]]  # module -> public top-level class and function names
    imports: Mapping[str, tuple[str, ...]]  # module -> modules it imports; those of other parts resolve by origin identity


def sole_owners(catalog: CatalogDocument, facts: RepositoryFacts) -> dict[str, str]:
    """Module -> its only owning package (modules owned by none or by several are #52's findings, not ours)."""
    return {
        module: owners[0]
        for module, path in sorted(facts.modules.items())
        if len(owners := owners_of(catalog, path)) == 1
    }


def public_patterns(catalog: CatalogDocument) -> dict[str, tuple[str, ...]]:
    return {package.id: package.public for package in catalog.packages if package.public}


def is_public(module: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(module, pattern) for pattern in patterns)


def preservation_violations(
    catalog: CatalogDocument, facts: RepositoryFacts, parts: Sequence[PartFacts]
) -> list[Violation]:
    """Private-import bypasses in the origin, then the drift of each configured part against the catalog."""
    sole = sole_owners(catalog, facts)
    public = public_patterns(catalog)
    out = private_imports(sole, public, facts.imports, "")
    for part in parts:
        expected = {module for module, owner in sole.items() if owner in part.included}
        out += _semantics(catalog, part)
        out += _membership(sole, facts, part, expected)
        out += _rings(facts, part, expected)
        out += _symbols(sole, public, facts, part, expected)
        out += private_imports(sole, public, part.imports, f"part {part.location!r}: ")
    return out


def private_imports(
    sole: Mapping[str, str], public: Mapping[str, tuple[str, ...]], imports: Mapping[str, tuple[str, ...]], label: str
) -> list[Violation]:
    """An import of another package's module outside the surface that package declares (a package without one is open)."""
    out: list[Violation] = []
    for module, targets in sorted(imports.items()):
        owner = sole.get(module)
        for target in sorted(targets):
            other = sole.get(target)
            if owner is None or other is None or other == owner or other not in public or is_public(target, public[other]):
                continue
            out.append(Violation(
                Rule.PACKAGE_PRIVATE_IMPORT, module,
                f"{label}imports {target} of package {other!r}, outside its public surface ({', '.join(public[other])})",
            ))
    return out


def _semantics(catalog: CatalogDocument, part: PartFacts) -> list[Violation]:
    by_id = {package.id: package for package in catalog.packages}
    out: list[Violation] = []
    for declared in part.semantics:
        package = by_id.get(declared.id)
        if package is None:
            out.append(Violation(
                Rule.PACKAGE_SEMANTICS_DRIFT, declared.id,
                f"part {part.location!r} declares package {declared.id!r}, which the catalog does not have",
            ))
            continue
        out += _changed_fields(part, declared, semantics_of(package))
        out += _changed_paths(part, package)
    return out


def _changed_fields(part: PartFacts, declared: PackageSemantics, expected: PackageSemantics) -> list[Violation]:
    return [
        Violation(
            Rule.PACKAGE_SEMANTICS_DRIFT, declared.id,
            f"part {part.location!r}: {field.name} is {getattr(declared, field.name)!r}, the catalog says {getattr(expected, field.name)!r}",
        )
        for field in fields(PackageSemantics)
        if getattr(declared, field.name) != getattr(expected, field.name)
    ]


def _changed_paths(part: PartFacts, package: PackageDescriptor) -> list[Violation]:
    """Per-path role and metadata, keyed by the origin coordinate; where a path now lives is not compared."""
    def key(role: str, metadata: Mapping[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        pairs: dict[str, str] = dict(metadata) if metadata is not None else {}
        return role, tuple(sorted(pairs.items()))

    expected = {item.path: key(item.role, item.metadata) for item in package.paths}
    actual = {item.origin_path: key(item.role, item.metadata) for item in part.paths if item.package_id == package.id}
    out: list[Violation] = []
    for path in sorted(expected.keys() | actual.keys()):
        if expected.get(path) != actual.get(path):
            was, now = expected.get(path), actual.get(path)
            out.append(Violation(
                Rule.PACKAGE_SEMANTICS_DRIFT, package.id,
                f"part {part.location!r}: path {path!r} is {'absent' if now is None else now!r} in the part, "
                f"the catalog says {'nothing' if was is None else was!r}",
            ))
    return out


def _membership(
    sole: Mapping[str, str], facts: RepositoryFacts, part: PartFacts, expected: set[str]
) -> list[Violation]:
    out = [
        Violation(Rule.PACKAGE_MEMBERSHIP_DRIFT, module, f"part {part.location!r}: module of package {sole[module]!r} is missing")
        for module in sorted(expected - part.module_paths.keys())
    ]
    for module in sorted(part.module_paths.keys() - expected):
        if module in sole:
            why = f"belongs to package {sole[module]!r}, which this part does not include"
        elif module in facts.modules:
            why = "is claimed by no single package in the catalog"
        else:
            why = "does not exist in the origin"
        out.append(Violation(Rule.PACKAGE_MEMBERSHIP_DRIFT, module, f"part {part.location!r}: module {why}"))
    return out


def _rings(facts: RepositoryFacts, part: PartFacts, expected: set[str]) -> list[Violation]:
    return [
        Violation(
            Rule.PACKAGE_RING_DRIFT, module,
            f"part {part.location!r}: ring is {part.rings.get(module)!r}, the origin has {facts.rings.get(module)!r}",
        )
        for module in sorted(expected & part.module_paths.keys())
        if facts.rings.get(module) != part.rings.get(module)
    ]


def _symbols(
    sole: Mapping[str, str], public: Mapping[str, tuple[str, ...]], facts: RepositoryFacts, part: PartFacts, expected: set[str]
) -> list[Violation]:
    out: list[Violation] = []
    for module in sorted(expected & part.module_paths.keys()):
        owner = sole[module]
        if owner not in public or not is_public(module, public[owner]):
            continue
        dropped = sorted(facts.symbols.get(module, frozenset()) - part.symbols.get(module, frozenset()))
        if dropped:
            out.append(Violation(
                Rule.PACKAGE_PUBLIC_SYMBOL_DROPPED, module,
                f"part {part.location!r}: public symbols of package {owner!r} are gone: {', '.join(dropped)}",
            ))
    return out

