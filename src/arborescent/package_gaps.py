"""Gather the repository facts for the package-assignment rules (#52) and run them. The rules are in ``package_assignment``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .domain import Policy, Violation
from .facts import resolve_targets
from .package_assignment import RepositoryFacts, assignment_violations
from .package_model import CatalogDocument
from .package_policy import policy_violations
from .package_pyproject_gaps import (
    derived_projects,
    existing_derived_paths,
    pyproject_violations,
)
from .package_tree import is_generated
from .packages import read_catalog
from .path_exclusion import excluded_by_policy
from .ports import FileSystem, ScanResult
from .rules import RuleContext

__all__ = ["PackageContext", "asset_paths", "load_package_catalog", "load_package_context", "package_gap_violations", "repository_facts"]


def load_package_catalog(fs: FileSystem, root: Path, policy: Policy) -> CatalogDocument:
    """The configured catalog, parsed only: gaps (including double claims that validation refuses) are findings here."""
    return read_catalog(fs, fs.resolve(root), policy.package_catalog or "packages.toml")


@dataclass(frozen=True, slots=True)
class PackageContext:
    """The catalog and the repository facts it is checked against (also what the selftest plans its probes from)."""

    catalog: CatalogDocument
    facts: RepositoryFacts
    derived: tuple[str, ...] = ()  # derived package pyproject files that exist (the drift probe edits one)


def load_package_context(policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> PackageContext | None:
    """``None`` when the policy does not set ``package_catalog``."""
    if policy.package_catalog is None:
        return None
    catalog, facts = load_package_catalog(fs, root, policy), repository_facts(policy, root, fs, scan)
    derived = existing_derived_paths(fs, root, derived_projects(policy, scan, catalog, facts)) if policy.package_pyproject else ()
    return PackageContext(catalog, facts, derived)


def package_gap_violations(policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> list[Violation]:
    """Findings for the configured catalog; empty when the policy does not set ``package_catalog``."""
    context = load_package_context(policy, root, fs, scan)
    if context is None:
        return []
    found = [*assignment_violations(context.catalog, context.facts), *policy_violations(context.catalog)]
    if policy.package_pyproject:
        found += pyproject_violations(fs, root, derived_projects(policy, scan, context.catalog, context.facts))
    return found


def repository_facts(policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> RepositoryFacts:
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    tops = {name.split(".")[0] for name in scan.known}
    imports: dict[str, tuple[str, ...]] = {}
    externals: dict[str, tuple[str, ...]] = {}
    for name, info in scan.modules.items():
        resolved = [resolve_targets(name, info.is_package, imp, scan.known, tops) for imp in info.facts.imports]
        imports[name] = tuple(sorted({t for internal, _, _ in resolved for t in internal if t in scan.known and t != name}))
        externals[name] = tuple(sorted({t for _, outside, _ in resolved for t in outside if t not in policy.stdlib}))
    return RepositoryFacts(
        dict(scan.paths),
        {name: context.resolve_ring(name) for name in scan.paths},
        imports,
        asset_paths(policy, root, fs),
        externals,
    )


def asset_paths(policy: Policy, root: Path, fs: FileSystem) -> tuple[str, ...]:
    """Non-Python files below the policy's sources, minus the policy's excludes and generated output."""
    sources = {source for local in (policy, *(scope.policy for scope in policy.scopes)) for source in local.sources}
    found: set[str] = set()
    for source in sorted(sources):
        base = root if source in ("", ".") else root.joinpath(*Path(source).parts)
        if not fs.is_dir(base):
            continue
        for path in fs.rglob(base, "*"):
            rel = path.relative_to(root).as_posix()
            if path.suffix == ".py" or not fs.is_file(path):
                continue
            if is_generated(fs, root, path) or excluded_by_policy(rel, policy.exclude):
                continue
            found.add(rel)
    return tuple(sorted(found))
