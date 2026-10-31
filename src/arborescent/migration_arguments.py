"""The migration arguments of each package (#54): tool-neutral value objects built from the catalog and repository facts.

Everything a migration tool needs to be told, and nothing about any tool: identity and ``home``, the owned modules and
assets with their target paths and module names, the public surface and the modules that bypass it, declared
dependencies, and the annotation gaps that are still open. Pure; no migration is planned or performed here, and no
format of any particular tool is produced (``migration_json`` serialises these objects, a recipe or an adapter converts).

Target paths are relative to the root of the repository that holds the package after the migration: below the member's
folder for a workspace member, below the new repository's root for a package with its own repository.
"""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .domain import INFORMATIONAL_RULES
from .package_assignment import RepositoryFacts, assignment_violations, owners_of
from .package_fields import extraction_target, within
from .package_model import (
    CatalogDocument,
    CatalogError,
    ModuleRename,
    PackageDescriptor,
    PackageHome,
)

__all__ = [
    "Bypass", "Edge", "Gap", "GapCode", "MigrationArguments", "OwnedAsset", "OwnedModule", "PackageArguments",
    "PathRename", "build_migration_arguments",
]

ERROR = "error"
INFO = "info"


class GapCode:
    """Gap codes that exist only in the export. The package-assignment rules (#52) keep their own rule ids."""

    UNDECLARED_HOME = "package-undeclared-home"
    UNDECLARED_PUBLIC_SURFACE = "package-undeclared-public-surface"
    UNDECLARED_EXTERNAL = "package-undeclared-external"
    UNPLACEABLE_PATH = "package-unplaceable-path"
    UNPLACED_ASSET = "package-unplaced-asset"
    MODULE_RENAME_COLLISION = "package-module-rename-collision"
    UNUSED_MODULE_RENAME = "package-unused-module-rename"

    ALL = (
        UNDECLARED_HOME, UNDECLARED_PUBLIC_SURFACE, UNDECLARED_EXTERNAL, UNPLACEABLE_PATH, UNPLACED_ASSET,
        MODULE_RENAME_COLLISION, UNUSED_MODULE_RENAME,
    )


@dataclass(frozen=True, slots=True)
class OwnedModule:
    module: str
    path: str  # repository-relative, in the monorepo
    target_path: str | None  # None when no owned path places it
    target_module: str  # equal to ``module`` unless a module rename applies


@dataclass(frozen=True, slots=True)
class OwnedAsset:
    path: str
    target_path: str | None  # None: owned by an [[assets]] glob but below no owned path, so it has no destination


@dataclass(frozen=True, slots=True)
class PathRename:
    source: str  # an owned path in the monorepo
    target: str  # where it lands; "." is the repository root


@dataclass(frozen=True, slots=True)
class Bypass:
    """A module of another package that imports a module of this package which is not on its public surface."""

    module: str
    package: str
    imports: str


@dataclass(frozen=True, slots=True)
class Edge:
    source: str
    target: str
    kind: str  # "depends_on" (declared) or "imports" (observed); both can exist for one pair


@dataclass(frozen=True, slots=True)
class Gap:
    """An annotation that is missing or inconsistent, named so a consumer can refuse to proceed."""

    code: str
    subject: str
    detail: str
    severity: str  # "error" blocks a migration, "info" does not
    package: str | None = None


@dataclass(frozen=True, slots=True)
class PackageArguments:
    id: str
    name: str
    kind: str
    summary: str
    domain: str | None
    capabilities: tuple[str, ...]
    home: PackageHome | None
    modules: tuple[OwnedModule, ...]
    assets: tuple[OwnedAsset, ...]
    path_renames: tuple[PathRename, ...]
    module_renames: tuple[ModuleRename, ...]
    public_surface: tuple[str, ...]  # empty: no declared surface, so nothing of the package counts as private
    bypasses: tuple[Bypass, ...]
    depends_on: tuple[str, ...]
    externals: tuple[str, ...] | None  # None: not annotated
    pyproject: Mapping[str, object] | None = None  # the derived pyproject.toml fields (#60); None: ``package_pyproject`` is off


@dataclass(frozen=True, slots=True)
class _Inputs:
    catalog: CatalogDocument
    facts: RepositoryFacts
    sole: Mapping[str, str]  # module -> its only owning package


@dataclass(frozen=True, slots=True)
class MigrationArguments:
    packages: tuple[PackageArguments, ...]
    edges: tuple[Edge, ...]
    gaps: tuple[Gap, ...]

    @property
    def ready(self) -> bool:
        return not any(gap.severity == ERROR for gap in self.gaps)


def build_migration_arguments(catalog: CatalogDocument, facts: RepositoryFacts) -> MigrationArguments:
    gaps: list[Gap] = [
        Gap(str(v.rule), v.subject, v.detail, INFO if v.rule in INFORMATIONAL_RULES else ERROR)
        for v in assignment_violations(catalog, facts)
    ]
    inputs = _Inputs(catalog, facts, _sole_owners(catalog, facts))
    observed, importers = _observed_edges(facts, inputs.sole)
    packages = tuple(
        _package_arguments(package, inputs, importers.get(package.id, {}), gaps)
        for package in sorted(catalog.packages, key=lambda item: item.id)
    )
    edges = {Edge(package.id, dep, "depends_on") for package in catalog.packages for dep in package.depends_on}
    edges |= {Edge(source, target, "imports") for source, target in observed}
    return MigrationArguments(
        packages,
        tuple(sorted(edges, key=lambda e: (e.source, e.target, e.kind))),
        tuple(sorted(gaps, key=lambda g: (g.code, g.subject, g.detail))),
    )


def _sole_owners(catalog: CatalogDocument, facts: RepositoryFacts) -> dict[str, str]:
    return {
        module: owners[0]
        for module, path in facts.modules.items()
        if len(owners := owners_of(catalog, path)) == 1
    }


def _observed_edges(
    facts: RepositoryFacts, sole: Mapping[str, str]
) -> tuple[set[tuple[str, str]], dict[str, dict[str, list[tuple[str, str]]]]]:
    """Package-level imports, and for each imported package the (importing module, imported module) pairs by package."""
    edges: set[tuple[str, str]] = set()
    importers: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for module, owner in sole.items():
        for target in facts.imports.get(module, ()):
            other = sole.get(target)
            if other is not None and other != owner:
                edges.add((owner, other))
                importers[other][owner].append((module, target))
    return edges, importers


def _package_arguments(
    package: PackageDescriptor,
    inputs: _Inputs,
    importers: Mapping[str, Sequence[tuple[str, str]]],
    gaps: list[Gap],
) -> PackageArguments:
    catalog, facts = inputs.catalog, inputs.facts
    placements = _placements(package, gaps)
    mine = sorted(module for module, owner in inputs.sole.items() if owner == package.id)
    modules = tuple(
        OwnedModule(
            module, facts.modules[module], _target_of(facts.modules[module], placements),
            _target_module(module, package.module_renames),
        )
        for module in mine
    )
    assets = tuple(
        OwnedAsset(path, _asset_target(package, path, placements, gaps))
        for path in sorted(facts.assets)
        if owners_of(catalog, path, asset=True) == (package.id,)
    )
    if package.home is None:
        gaps.append(Gap(GapCode.UNDECLARED_HOME, package.id, "no [packages.home]: own repository or workspace member is not annotated", ERROR, package.id))
    _rename_gaps(package, modules, gaps)
    externals = _external_gaps(package, facts, mine, gaps)
    surface, bypasses = _public_surface(package, modules, importers, gaps)
    return PackageArguments(
        package.id, package.name, package.kind, package.summary, package.domain, package.capabilities, package.home,
        modules, assets, tuple(PathRename(source.as_posix(), _posix(target)) for source, target in placements),
        package.module_renames, surface, bypasses, package.depends_on, externals,
    )


def _posix(path: PurePosixPath) -> str:
    return path.as_posix() if path.parts else "."


def _placements(package: PackageDescriptor, gaps: list[Gap]) -> list[tuple[PurePosixPath, PurePosixPath]]:
    """(owned path, final path) pairs, ordered by source; unplaceable paths become gaps."""
    base = PurePosixPath(package.home.path) if package.home is not None and package.home.path is not None else PurePosixPath()
    out: list[tuple[PurePosixPath, PurePosixPath]] = []
    for item in sorted(package.paths, key=lambda p: p.path):
        try:
            out.append((PurePosixPath(item.path), base / extraction_target(package, item)))
        except CatalogError as exc:
            gaps.append(Gap(GapCode.UNPLACEABLE_PATH, item.path, str(exc), ERROR, package.id))
    return out


def _target_of(path: str, placements: Sequence[tuple[PurePosixPath, PurePosixPath]]) -> str | None:
    source = PurePosixPath(path)
    covering = [(s, t) for s, t in placements if within(source, s)]
    if not covering:
        return None
    origin, target = max(covering, key=lambda pair: len(pair[0].parts))
    return _posix(target / source.relative_to(origin))


def _asset_target(
    package: PackageDescriptor, path: str, placements: Sequence[tuple[PurePosixPath, PurePosixPath]], gaps: list[Gap]
) -> str | None:
    target = _target_of(path, placements)
    if target is None:
        gaps.append(Gap(GapCode.UNPLACED_ASSET, path, f"owned by package {package.id!r} through a glob, but below none of its paths: no destination", ERROR, package.id))
    return target


def _target_module(module: str, renames: Sequence[ModuleRename]) -> str:
    matching = [r for r in renames if module == r.source or module.startswith(r.source + ".")]
    if not matching:
        return module
    best = max(matching, key=lambda r: len(r.source))
    return best.target + module[len(best.source):]


def _rename_gaps(package: PackageDescriptor, modules: Sequence[OwnedModule], gaps: list[Gap]) -> None:
    by_target: dict[str, list[str]] = defaultdict(list)
    for item in modules:
        by_target[item.target_module].append(item.module)
    for target, sources in sorted(by_target.items()):
        if len(sources) > 1:
            gaps.append(Gap(GapCode.MODULE_RENAME_COLLISION, target, f"modules {', '.join(sources)} of {package.id!r} all become {target}", ERROR, package.id))
    gaps.extend(
        Gap(GapCode.UNUSED_MODULE_RENAME, rename.source, f"module rename {rename.source} -> {rename.target} of {package.id!r} matches no owned module", ERROR, package.id)
        for rename in package.module_renames
        if not any(m.module == rename.source or m.module.startswith(rename.source + ".") for m in modules)
    )


def _external_gaps(
    package: PackageDescriptor, facts: RepositoryFacts, mine: Sequence[str], gaps: list[Gap]
) -> tuple[str, ...] | None:
    observed = sorted({name for module in mine for name in facts.externals.get(module, ())})
    declared = set(package.externals or ())
    how = "externals is not annotated" if package.externals is None else "it is not in externals"
    gaps.extend(
        Gap(GapCode.UNDECLARED_EXTERNAL, package.id, f"imports external {name!r} and {how}", ERROR, package.id)
        for name in observed
        if name not in declared
    )
    return package.externals


def _public_surface(
    package: PackageDescriptor, modules: Sequence[OwnedModule], importers: Mapping[str, Sequence[tuple[str, str]]],
    gaps: list[Gap],
) -> tuple[tuple[str, ...], tuple[Bypass, ...]]:
    if not package.public:
        if importers:
            gaps.append(Gap(GapCode.UNDECLARED_PUBLIC_SURFACE, package.id, f"imported by packages {', '.join(sorted(importers))} but declares no public surface", ERROR, package.id))
        return (), ()
    surface = tuple(m.module for m in modules if _is_public(m.module, package.public))
    bypasses = tuple(sorted(
        (Bypass(module, importer, target) for importer, pairs in importers.items() for module, target in pairs if not _is_public(target, package.public)),
        key=lambda b: (b.module, b.imports),
    ))
    return surface, bypasses


def _is_public(module: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(module, pattern) for pattern in patterns)
