"""Which package owns each module and asset (#52): unassigned, doubly assigned, ring-spanning and undeclared-edge findings.

Pure: the caller supplies the facts. A package claims a file by listing a path that is the file or a folder above it
(``packages.toml`` ``paths``); a non-Python file can also be claimed by an ``[[assets]]`` glob, where the first matching
rule wins. A package is not a ring, so spanning rings is information, not a failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .domain import Rule, Violation
from .package_fields import glob_match, within
from .package_model import CatalogDocument

__all__ = ["RepositoryFacts", "assignment_violations", "owners_of"]


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    modules: Mapping[str, str]  # first-party module -> posix path relative to the repository root
    rings: Mapping[str, str | None]  # module -> its effective ring (None when unclassified)
    imports: Mapping[str, tuple[str, ...]]  # module -> first-party modules it imports
    assets: Sequence[str]  # non-Python files below the policy's sources, repository-relative
    externals: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # module -> third-party top-level names it imports


def owners_of(catalog: CatalogDocument, path: str, *, asset: bool = False) -> tuple[str, ...]:
    """Ids of the packages that claim ``path``, sorted: by an owned path and, for an asset, by the first matching glob."""
    target = PurePosixPath(path)
    found = {
        package.id
        for package in catalog.packages
        if any(within(target, PurePosixPath(item.path)) for item in package.paths)
    }
    if asset:
        rule = next((rule for rule in catalog.assets if glob_match(rule.pattern, path)), None)
        if rule is not None:
            found.add(rule.package)
    return tuple(sorted(found))


def assignment_violations(catalog: CatalogDocument, facts: RepositoryFacts) -> list[Violation]:
    out: list[Violation] = []
    sole: dict[str, str] = {}  # module -> its only owning package
    claimed: dict[str, tuple[str, ...]] = {}  # module -> every package that claims it
    for module, path in sorted(facts.modules.items()):
        owners = claimed[module] = owners_of(catalog, path)
        if not owners:
            out.append(Violation(Rule.PACKAGE_UNASSIGNED_MODULE, module, f"module ({path}) is claimed by no package"))
        elif len(owners) > 1:
            out.append(Violation(
                Rule.PACKAGE_MULTIPLY_ASSIGNED_MODULE, module,
                f"module ({path}) is claimed by packages {_names(owners)}; a module belongs to exactly one",
            ))
        else:
            sole[module] = owners[0]
    for path in sorted(facts.assets):
        owners = owners_of(catalog, path, asset=True)
        if not owners:
            out.append(Violation(Rule.PACKAGE_UNOWNED_ASSET, path, "asset is claimed by no package path and no [[assets]] glob"))
        elif len(owners) > 1:
            out.append(Violation(
                Rule.PACKAGE_MULTIPLY_ASSIGNED_ASSET, path,
                f"asset is claimed by packages {_names(owners)}; a path owner and an [[assets]] glob must agree",
            ))
    out += _ring_spans(catalog, facts, claimed)
    out += _undeclared_edges(catalog, facts, sole)
    return out


def _names(owners: Sequence[str]) -> str:
    return ", ".join(repr(owner) for owner in owners)


def _ring_spans(
    catalog: CatalogDocument, facts: RepositoryFacts, claimed: Mapping[str, tuple[str, ...]]
) -> list[Violation]:
    out: list[Violation] = []
    for package in catalog.packages:
        mine = [module for module, owners in claimed.items() if package.id in owners]
        rings = sorted({ring for module in mine if (ring := facts.rings.get(module)) is not None})
        if len(rings) > 1:
            out.append(Violation(
                Rule.PACKAGE_SPANS_RINGS, package.id,
                f"package {package.id!r} has {len(mine)} modules in rings {', '.join(rings)}; "
                "a package is not a ring, so a split follows module sets, not folders",
            ))
    return out


def _undeclared_edges(catalog: CatalogDocument, facts: RepositoryFacts, sole: Mapping[str, str]) -> list[Violation]:
    declared = {package.id: set(package.depends_on) for package in catalog.packages}
    out: list[Violation] = []
    for module, owner in sorted(sole.items()):
        seen: set[str] = set()
        for target in sorted(facts.imports.get(module, ())):
            other = sole.get(target)
            if other is None or other == owner or other in seen or other in declared.get(owner, set()):
                continue
            seen.add(other)
            out.append(Violation(
                Rule.PACKAGE_UNDECLARED_DEPENDENCY, module,
                f"imports {target} of package {other!r}, which package {owner!r} does not list in depends_on",
            ))
    return out
